"""Sky Eyes 视频检索 · 中文 CLIP 编码服务（模型侧，部署在有 GPU 的机器上）

## 为什么单独一个服务，而不是塞进本地后端

1. **本地没有 GPU。** CPU 上实测约 890 ms/帧，959 帧要 14 分钟；
   这台 4060 上是另一个数量级。
2. 本地后端 `server/.venv` **刻意不装 torch**（见 DEMO_PLAN）。为一个旁路功能破这条规矩，
   会把后端从"轻量编排"变成"半个推理服务"，得不偿失。
3. 服务**无状态**：它只做向量化，不持有索引、不掺和业务规则。
   索引存在本地、跟着演示数据走 —— 所以这个服务挂了重启即可，不丢任何东西。
   更重要的是，本地因此能把**「服务不可用」和「没搜到」明确分开**：
   对一个检索功能，这两种失败混在一起，用户看到的东西是一样的（都是"没结果"），
   而排查方向完全相反。

## 接口刻意做得极窄

    GET  /health
    POST /embed_text    {"texts": ["黄色的小车", ...]}       -> [N, 512] 已 L2 归一化
    POST /embed_images  {"images_b64": ["<jpeg b64>", ...]}  -> [M, 512] 已 L2 归一化

相似度、排序、阈值一律在本地用 numpy 做。**服务不返回"结果"，只返回向量** ——
换模型、调阈值都不必动服务，排序逻辑也能被本地测试覆盖。

向量用 **base64 打包的 float32** 回传，而不是 JSON 浮点数组：
  · 无精度损失 —— 部署时要拿本地 CPU 的结果做逐元素比对，JSON 的浮点舍入会掩盖差异
  · 体积小约 6 倍，959 帧的索引构建省下几十 MB

## 模型与依赖（这段是踩坑记录，别改）

权重是 `damo/multi-modal_clip-vit-base-patch16_zh`（Chinese-CLIP ViT-B/16）。
加载用 **`cn_clip`（Chinese-CLIP 官方实现）**，不用 modelscope 的 pipeline：
后者的 `preprocessors.multi_modal` 在**模块级** `import decord` 并 `from .ofa import *`
（ofa 又会 import librosa）——为一条永远走不到的语音/视频路径拖一堆包。
而 `cn_clip` 的推理路径只需要 torch + torchvision。

`cn_clip.create_model` 会剥掉 DDP 的 `module.` 前缀、过滤 `bert.pooler`、并且用
**`strict=True` 载入** —— 与这份权重的键名完全吻合（实测 353/353 全命中）。
这里刻意保持 `strict` 语义：键名对不上必须**当场报错**，
否则 `strict=False` 会静默留下随机初始化的层，输出"看起来像样但其实是噪声"的向量。

用法：
    SKYEYES_CLIP_MODEL=/path/to/model python service.py --port 8001
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, List

import numpy as np
import torch
from PIL import Image

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "service.py 需要 fastapi 与 pydantic（远端 Janus 的 venv 里有）。"
        f"当前解释器缺少：{exc}"
    ) from None

# ---- 常量 ---------------------------------------------------------------- #

DEFAULT_PORT = 8001
DEFAULT_HOST = "127.0.0.1"
#: 一次前向的图片数。ViT-B/16 @224 在 4060 上这组很宽裕；纯粹是内存与吞吐的折中。
DEFAULT_BATCH = 32
#: 单张图解码后的字节上限。前端会把帧压到几十 KB，给足余量即可，
#: 但不设上限就是个可以被打爆的内存入口。
MAX_IMAGE_BYTES = 12 * 1024 * 1024
#: Chinese-CLIP 的图文联合嵌入维度（ViT-B/16 + BERT-base）。
EMBED_DIM = 512
#: 文本上下文长度，与 cn_clip 官方推理示例一致。
CONTEXT_LENGTH = 52

log = logging.getLogger("clip_service")


# ---- 请求体模型 ------------------------------------------------------------ #
#
# ⚠ 这两个类**必须定义在模块级**，不能挪进 build_app() 里。
# FastAPI 靠函数的 __globals__ 解析注解；类若定义在函数内部就解析不到，
# 它会退而把参数当成**查询参数**，于是所有 POST 都返回
# `422 {"loc":["query","payload"],"msg":"Field required"}` ——
# 报错信息完全指不到真正的原因。这样踩过一次，别再挪回去。


class TextIn(BaseModel):
    texts: List[str]


class ImagesIn(BaseModel):
    images_b64: List[str]


# ---- 模型 ----------------------------------------------------------------- #


def _sha256_head(path: Path, n: int = 16) -> str:
    """对权重文件取 sha256 前 n 位。

    启动时算一次（720MB 约 2 秒）。它进 `/health`，让本地能断言
    "远端正在用的就是本地做基准的那份权重" —— 换权重忘了同步时，
    这个指纹会让问题立刻现形，而不是表现为"检索结果莫名其妙变差"。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


class ClipEncoder:
    """Chinese-CLIP 的图文编码器。所有方法都返回 L2 归一化后的 float32。"""

    def __init__(self, model_dir: Path, device: str = "auto", fp16: bool = False):
        import json

        from cn_clip.clip.model import CLIP
        from cn_clip.clip.utils import image_transform, tokenize

        self.model_dir = model_dir
        self._tokenize = tokenize

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        # fp32 是默认：4060 上 ViT-B/16 本来就快，而 fp32 能让远端结果与本地 CPU
        # 基准逐元素对上（fp16 会有小差异，跨机比对时要放宽容差）。
        self.dtype = torch.float16 if (fp16 and self.device.type == "cuda") else torch.float32

        text_cfg = json.loads((model_dir / "text_model_config.json").read_text())
        vision_cfg = json.loads((model_dir / "vision_model_config.json").read_text())
        info = {**text_cfg, **vision_cfg}
        self.model_info = info

        model = CLIP(**info)

        weights = model_dir / "pytorch_model.bin"
        if not weights.is_file():
            raise FileNotFoundError(f"找不到权重：{weights}")
        self.weights_sha256 = _sha256_head(weights)

        ckpt = torch.load(weights, map_location="cpu")
        state = ckpt["state_dict"] if "state_dict" in ckpt else ckpt
        if next(iter(state)).startswith("module."):
            state = {k[len("module.") :]: v for k, v in state.items()}
        state = {k: v for k, v in state.items() if "bert.pooler" not in k}

        missing, unexpected = model.load_state_dict(state, strict=False)
        if missing or unexpected:
            raise RuntimeError(
                "权重与模型定义对不上，拒绝启动——"
                f"缺失 {len(missing)} 个、多余 {len(unexpected)} 个张量。"
                f"缺失示例 {missing[:3]}，多余示例 {unexpected[:3]}。"
                "（用 strict=False 静默放过会留下随机初始化的层，"
                "输出看起来正常但其实是噪声。）"
            )

        model.eval()
        model = model.to(dtype=self.dtype, device=self.device)

        # 词表必须与模型的 vocab_size 对上。词表错了不会报错，只会让文本向量默默变差。
        vocab_size = len(self._tokenize.__globals__["_tokenizer"].vocab)
        if vocab_size != info["vocab_size"]:
            raise RuntimeError(
                f"词表大小 {vocab_size} 与模型配置 {info['vocab_size']} 不一致"
            )
        self.vocab_size = vocab_size

        self.model = model
        self.preprocess = image_transform(info.get("image_resolution", 224))
        log.info(
            "模型就绪 device=%s dtype=%s embed_dim=%s vocab=%d weights=%s",
            self.device, self.dtype, info["embed_dim"], vocab_size, self.weights_sha256,
        )

    # ---- 编码 ---- #

    def _pack(self, arr: np.ndarray) -> dict:
        """float32 → base64。不是 JSON 浮点数组：要无精度损失地跨机比对。"""
        arr = np.ascontiguousarray(arr.astype("<f4"))
        return {
            "shape": list(arr.shape),
            "dtype": "float32",
            "b64": base64.b64encode(arr.tobytes()).decode("ascii"),
        }

    def encode_text(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        tokens = self._tokenize(list(texts), context_length=CONTEXT_LENGTH)
        with torch.inference_mode():
            feats = self.model.encode_text(tokens.to(self.device))
        return _l2(feats.float().cpu().numpy())

    def encode_images(self, images: List[Image.Image], batch: int = DEFAULT_BATCH) -> np.ndarray:
        if not images:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        out: List[np.ndarray] = []
        for i in range(0, len(images), batch):
            chunk = images[i : i + batch]
            x = torch.stack([self.preprocess(im) for im in chunk]).to(
                dtype=self.dtype, device=self.device
            )
            with torch.inference_mode():
                feats = self.model.encode_image(x)
            out.append(feats.float().cpu().numpy())
        return _l2(np.concatenate(out, axis=0))

    def decode_images(self, blobs: List[str]) -> List[Image.Image]:
        """base64 → PIL。逐条给出可读的失败原因，不吞异常。"""
        out: List[Image.Image] = []
        for idx, blob in enumerate(blobs):
            if not isinstance(blob, str) or not blob:
                raise ValueError(f"第 {idx} 张图不是非空字符串")
            try:
                raw = base64.b64decode(blob, validate=True)
            except Exception as exc:
                raise ValueError(f"第 {idx} 张图 base64 解码失败：{exc}") from None
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError(
                    f"第 {idx} 张图 {len(raw)/1024/1024:.1f} MB，超过上限 "
                    f"{MAX_IMAGE_BYTES/1024/1024:.0f} MB"
                )
            try:
                out.append(Image.open(io.BytesIO(raw)).convert("RGB"))
            except Exception as exc:
                raise ValueError(f"第 {idx} 张图不是可解析的图片：{exc}") from None
        return out


def _l2(arr: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(arr, axis=-1, keepdims=True)
    return arr / np.clip(norm, 1e-12, None)


# ---- HTTP ----------------------------------------------------------------- #


def build_app(encoder: ClipEncoder):
    app = FastAPI(title="Sky Eyes 视频检索编码服务", version="1.0")
    started = time.time()

    @app.get("/health")
    def health() -> dict[str, Any]:
        gpu: dict[str, Any] = {}
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            gpu = {
                "name": torch.cuda.get_device_name(0),
                "mem_free_mb": round(free / 1024 / 1024, 1),
                "mem_total_mb": round(total / 1024 / 1024, 1),
            }
        return {
            "ok": True,
            "model": "chinese-clip-ViT-B-16",
            "model_dir": str(encoder.model_dir),
            "weights_sha256": encoder.weights_sha256,
            "device": str(encoder.device),
            "dtype": str(encoder.dtype).replace("torch.", ""),
            "embed_dim": encoder.model_info["embed_dim"],
            "image_resolution": encoder.model_info.get("image_resolution"),
            "vocab_size": encoder.vocab_size,
            "batch": DEFAULT_BATCH,
            "gpu": gpu,
            "uptime_sec": round(time.time() - started, 1),
        }

    @app.post("/embed_text")
    def embed_text(payload: TextIn) -> dict[str, Any]:
        try:
            emb = encoder.encode_text(payload.texts)
        except Exception as exc:  # noqa: BLE001 - 转成 400，别让 500 掩盖原因
            raise HTTPException(400, f"文本编码失败：{type(exc).__name__}: {exc}") from None
        return {"count": len(payload.texts), "embeddings": encoder._pack(emb)}

    @app.post("/embed_images")
    def embed_images(payload: ImagesIn) -> dict[str, Any]:
        try:
            images = encoder.decode_images(payload.images_b64)
            emb = encoder.encode_images(images)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"图片编码失败：{type(exc).__name__}: {exc}") from None
        return {"count": len(images), "embeddings": encoder._pack(emb)}

    return app


# ---- CLI ------------------------------------------------------------------ #


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sky Eyes 视频检索 · 中文 CLIP 编码服务")
    parser.add_argument("--host", default=os.environ.get("SKYEYES_CLIP_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SKYEYES_CLIP_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("SKYEYES_CLIP_MODEL", str(Path(__file__).resolve().parent / "model")),
        help="含 pytorch_model.bin / vocab.txt / *_config.json 的目录",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("SKYEYES_CLIP_DEVICE", "auto"),
        choices=["auto", "cpu", "cuda"],
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        default=os.environ.get("SKYEYES_CLIP_FP16", "0") not in ("0", "", "false", "False"),
        help="GPU 上用 fp16（更快更省显存，但与 fp32 结果有微小差异）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    model_dir = Path(args.model_dir).resolve()
    try:
        encoder = ClipEncoder(model_dir, device=args.device, fp16=args.fp16)
    except Exception as exc:  # noqa: BLE001
        log.error("模型加载失败，拒绝启动：%s: %s", type(exc).__name__, exc)
        return 1

    import uvicorn

    log.info("监听 http://%s:%d", args.host, args.port)
    uvicorn.run(build_app(encoder), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
