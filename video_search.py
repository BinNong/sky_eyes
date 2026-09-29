"""视频检索：用一句自然语言，在录像里定位到对应的帧。

典型用法：输入「黄色引擎盖的小车」，返回该视频里最像的若干帧（帧号 + 时间点 + 缩略图），
点一下就能把播放器跳到那一刻。用于查资料、找证据。

## 架构：编码在远端，索引与排序在本地

    [本地] 抽帧 + 建索引(一次) ──┐
                                ├─► HTTP ──► [远端 GPU] 中文 CLIP 编码服务
    [本地] 查询：文字 → 向量 ───┘                 （只做向量化，无状态）
                ↓
            本地算余弦 + 排序

这个切分有三个刻意的选择：

1. **模型不在本地跑。** 本地无 GPU，实测 890 ms/帧，38 秒的视频要 14 分钟；
   放到 4060 上是一个数量级。本地后端 `server/.venv` 也刻意不装 torch。
2. **索引存本地**（跟着演示数据走），不在服务里。所以编码服务是**无状态**的：
   它挂了重启即可，不丢任何东西。
3. **排序在本地**。服务只回向量、不回"结果"。这样换模型、调阈值、改排序规则
   都不用动远端，而且这些逻辑能被本地测试覆盖。

## 为什么这个模块是纯标准库

它要被两处 import：
  · `pipeline.py` / 建索引 CLI —— 跑在 conda `visual_search` 环境
  · `server/app.py` —— 跑在 `server/.venv`，**那里没有 numpy 也没有 requests**

引任何第三方库都会把两边同时绑死（`alerting.py` 当初也是因为这个原因写成纯 stdlib）。
余弦因此用 `array` + `map`/`operator.mul` 在 C 层循环里算：959 帧 × 512 维约 20 ms，
对 38 秒的视频完全够用。真要支撑几万帧的长视频，再考虑引入 numpy。

## ⚠ 分数怎么读（这是本模块最容易被误读的地方）

实测：所有帧的余弦都挤在 0.34~0.41 这个**窄带**里，同一查询前四名之间常常只差 0.002。
**窄不代表没用**——对着帧逐一核对过，排序是准的（「黄色的小车」前四名每一帧里都有黄色小车）。
所以：

  · **不要用绝对阈值判定"没搜到"**。0.35 在这个模型上可能是最好的结果，也可能是最差的。
  · 界面上要显示**相对信息**（第 1 名与中位的差值、名次），让使用者自己判断，而不是给一个
    "相似度 87%" 这种看起来精确、实际无法解释的数字。
  · 真正稳健的用法是**看前几张**，而不是信一个分数。
"""

from __future__ import annotations

import base64
import json
import math
import operator
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import dataclass, field
from pathlib import Path

# ---- 常量 ------------------------------------------------------------------ #

#: 索引格式版本。改结构时 +1，load_index 会拒绝不认识的版本而不是猜。
INDEX_VERSION = 1

#: 编码服务默认地址。本地经 SSH 隧道映射到远端的 8001（见 scripts/search_tunnel.sh）。
DEFAULT_SERVICE_URL = "http://127.0.0.1:8001"

#: 索引目录名（相对于 output_dir 或 run 目录）
INDEX_DIRNAME = "search_index"

#: 缩略图宽度。检索结果只需要"看得出是什么"；要看清楚就点进去跳播放器。
THUMB_WIDTH = 320

#: 演示用的预置查询。它们会在建索引时一起编码进索引 ——
#: 这样即使现场网络断了、编码服务不可达，这些示例查询仍然能用。
DEFAULT_CHIPS = [
    "黄色的小汽车",
    "公交车",
    "摩托车",
    "行人",
    "卡车",
    "十字路口",
    "斑马线",
    "树",
]


def _needs_byteswap() -> bool:
    """本机是否用大端。

    ⚠ 这里踩过一次坑，别再"优化"：
        struct.pack(">H", 1)[0] == 0
    看着像在测字节序，其实小端机上 `struct.pack(">H",1)` 得到 b'\\x00\\x01'，
    `[0] == 0` 恒为真 —— 于是**每一台常见机器都会被判成大端**，索引向量被
    整段 byteswap 成 NaN/天文数字。而它不抛异常，只是让检索返回乱七八糟的名次。
    直接问解释器要答案，不要自己推。
    """
    return sys.byteorder == "big"


def _as_f32(values) -> array:
    """把一维向量转成小端 float32 的 array。"""
    a = array("f", values)
    if _needs_byteswap():
        a.byteswap()
    return a


class EncoderUnavailable(RuntimeError):
    """编码服务不可达或返回错误。

    单独一个异常类型是刻意的：调用方必须能把
    **「服务不可用」**和**「搜到 0 条」**分开。
    这两者在界面上都表现为"没结果"，但处置方式完全相反 ——
    前者去查隧道/服务，后者去改查询词。混在一起就会浪费大量排查时间。
    """


# ---- 服务客户端 ------------------------------------------------------------ #


class EncoderClient:
    """中文 CLIP 编码服务的客户端（纯 stdlib）。

    刻意**不使用环境变量里的代理**：目标始终是环回地址（本地隧道），
    而本机 `HTTP_PROXY` 指向一个本地代理，环回请求走代理会失败。
    """

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (
            base_url or os.environ.get("SKYEYES_CLIP_URL") or DEFAULT_SERVICE_URL
        ).rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})  # 显式不走代理
        )

    # -- 底层 -- #

    def _request(self, path: str, payload: dict | None, timeout: float | None = None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise EncoderUnavailable(f"{path} 返回 HTTP {exc.code}：{body}") from None
        except urllib.error.URLError as exc:
            raise EncoderUnavailable(
                f"连不上编码服务 {self.base_url}（{exc.reason}）。"
                "检查隧道 scripts/search_tunnel.sh 与远端 start.sh"
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise EncoderUnavailable(f"{path} 请求失败：{type(exc).__name__}: {exc}") from None

    # -- 对外 -- #

    def health(self) -> dict:
        return self._request("/health", None, timeout=6.0)

    def available(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except EncoderUnavailable:
            return False

    def embed_text(self, texts: list[str]) -> list[array]:
        if not texts:
            return []
        got = self._request("/embed_text", {"texts": list(texts)})
        return _unpack(got["embeddings"])

    def embed_images(self, jpegs: list[bytes]) -> list[array]:
        """一次别送太多：服务端按 32 张一批前向，但请求体是 base64，体积会膨胀 1/3。"""
        if not jpegs:
            return []
        blobs = [base64.b64encode(b).decode("ascii") for b in jpegs]
        got = self._request("/embed_images", {"images_b64": blobs})
        return _unpack(got["embeddings"])


def _unpack(packed: dict) -> list[array]:
    """解 base64 的 float32 → 每行一个 array('f')。

    用 base64 而不是 JSON 浮点数组，是为了**无精度损失**：
    部署时要拿远端 GPU 的结果与本地 CPU 基准逐元素比对，
    JSON 的浮点舍入会把真正的差异掩盖掉。
    """
    raw = base64.b64decode(packed["b64"])
    n, dim = packed["shape"]
    # <f4：显式小端，避免依赖宿主机字节序
    flat = array("f")
    flat.frombytes(raw)
    if len(flat) != n * dim:
        raise EncoderUnavailable(f"向量长度不符：期望 {n}×{dim}，实得 {len(flat)}")
    return [flat[i * dim : (i + 1) * dim] for i in range(n)]


# ---- 索引 ------------------------------------------------------------------ #


@dataclass
class Hit:
    """一条检索结果。"""

    frame_index: int
    time_sec: float
    score: float
    rank: int
    thumb: str | None = None

    def to_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "time_sec": round(self.time_sec, 3),
            "score": round(self.score, 4),
            "rank": self.rank,
            "thumb": self.thumb,
        }


@dataclass
class SearchIndex:
    """一段视频的可检索索引：帧向量 + 元数据。

    向量以**原始 float32** 存在 `vectors.f32`，不塞进 JSON ——
    959 帧 × 512 维写成 JSON 浮点文本约 6 MB，而且是二进制体积的好几倍。
    """

    dir: Path
    meta: dict
    _flat: memoryview = field(repr=False, default=None)  # type: ignore[assignment]
    _chips: dict[str, array] = field(repr=False, default_factory=dict)

    # -- 读取 -- #

    @property
    def dim(self) -> int:
        return int(self.meta["dim"])

    @property
    def count(self) -> int:
        return int(self.meta["frame_count"])

    @property
    def fps(self) -> float:
        return float(self.meta["fps"])

    @property
    def stride(self) -> int:
        return int(self.meta.get("stride", 1))

    @property
    def frames(self) -> list[dict]:
        return self.meta["frames"]

    def vector(self, i: int) -> memoryview:
        d = self.dim
        return self._flat[i * d : (i + 1) * d]

    def thumbs_dir(self) -> Path:
        return self.dir / "thumbs"

    # -- 检索 -- #

    def search_vector(self, query_vec, top_k: int = 12) -> list[Hit]:
        """给一个（已归一化的）查询向量，返回按余弦降序的前 top_k 条。

        向量都已 L2 归一化，所以内积就是余弦。用 `map`+`operator.mul` 让内层循环
        跑在 C 层（`sum(x*y for ...)` 是逐元素 Python 字节码，慢 5~10 倍）。
        """
        d = self.dim
        q = array("f", query_vec) if not isinstance(query_vec, array) else query_vec
        if len(q) != d:
            raise ValueError(f"查询向量维度 {len(q)} 与索引 {d} 不符")

        scored: list[tuple[float, int]] = []
        for i in range(self.count):
            row = self._flat[i * d : (i + 1) * d]
            scored.append((sum(map(operator.mul, row, q)), i))
        scored.sort(key=lambda t: -t[0])

        hits: list[Hit] = []
        for rank, (score, i) in enumerate(scored[:top_k]):
            f = self.frames[i]
            hits.append(
                Hit(
                    frame_index=int(f["frame_index"]),
                    time_sec=float(f["time_sec"]),
                    score=float(score),
                    rank=rank,
                    thumb=f.get("thumb"),
                )
            )
        return hits

    def cached_query(self, query: str) -> bool:
        """这条查询是否有本地缓存向量（建索引时编码进去的预置查询）。

        有的话查询**完全不依赖编码服务** —— 现场断网时这几条还能用。
        """
        return query.strip() in self._chips

    def search(self, query: str, top_k: int = 12, client: EncoderClient | None = None) -> list[Hit]:
        """按自然语言检索。优先用预置查询缓存（服务不可达时仍可用）。"""
        chip = self._chips.get(query.strip())
        if chip is not None:
            return self.search_vector(chip, top_k)
        client = client or EncoderClient()
        vecs = client.embed_text([query])
        if not vecs:
            raise EncoderUnavailable("编码服务没有返回向量")
        return self.search_vector(vecs[0], top_k)

    def chips(self) -> list[str]:
        return list(self.meta.get("chips") or [])

    def describe(self) -> dict:
        """给后端/前端用的摘要（不含向量）。

        ⚠ 这里的键就是**前端直接读的键**。漏一个不会报错——界面上只会渲染成空白，
        看起来像"样式问题"而不是"数据没给"。`stride_sec` 就这么漏过一次
        （界面上显示成「每 秒」）。所以 tests/test_video_search.py 里有一条
        测试专门断言这份键集合，别再靠眼睛发现。
        """
        m = self.meta
        return {
            "available": True,
            "video": m.get("video"),
            "fps": m.get("fps"),
            "stride": m.get("stride"),
            "stride_sec": m.get("stride_sec"),
            "frame_count": m.get("frame_count"),
            "source_frames": m.get("source_frames"),
            "duration_sec": m.get("duration_sec"),
            "dim": m.get("dim"),
            "chips": self.chips(),
            "chips_cached": len(self._chips),
            "encoder": m.get("encoder"),
            "built_at": m.get("built_at"),
            "index_version": m.get("index_version"),
        }


def _sanity_check_rows(flat: memoryview, count: int, dim: int, where: str) -> None:
    """抽查若干行：必须是有限值，且 L2 范数 ≈ 1（向量在服务端已归一化）。

    这一条同时能抓住四类**静默**错误：字节序搞反、dtype 读错、行列错位、写盘截断。
    它们都不抛异常，只会让检索返回"看起来排过序、其实毫无意义"的结果 ——
    本项目在等级链上已经吃过三次这种亏，所以索引这里放一道硬守卫。
    """
    if count <= 0:
        raise ValueError(f"{where}：索引里没有任何向量")
    probes = sorted({0, count // 3, count // 2, count - 1})
    for i in probes:
        row = flat[i * dim : (i + 1) * dim]
        if len(row) != dim:
            raise ValueError(f"{where}：第 {i} 行长度 {len(row)} != {dim}，索引被截断")
        vals = list(row)
        if any(v != v or v in (float("inf"), float("-inf")) for v in vals):
            raise ValueError(
                f"{where}：第 {i} 行含 NaN/Inf，索引已损坏。"
                "最常见的成因是字节序判断写反、把整段向量 byteswap 了"
            )
        norm = math.sqrt(sum(v * v for v in vals))
        if abs(norm - 1.0) > 1e-2:
            raise ValueError(
                f"{where}：第 {i} 行的 L2 范数是 {norm:.4f} 而不是 1 —— "
                "索引里的向量不是归一化向量（字节序 / dtype / 行列错位）"
            )


def load_index(index_dir: Path | str) -> SearchIndex:
    index_dir = Path(index_dir)
    meta_path = index_dir / "index.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"找不到索引：{meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    ver = int(meta.get("index_version", -1))
    if ver != INDEX_VERSION:
        # 宁可拒绝，也不要拿旧结构的索引算出一个"看起来正常"的错结果
        raise ValueError(
            f"索引版本 {ver} 与本代码期望的 {INDEX_VERSION} 不符，请重建索引"
        )

    vec_path = index_dir / "vectors.f32"
    raw = vec_path.read_bytes()
    n, d = int(meta["frame_count"]), int(meta["dim"])
    if len(raw) != n * d * 4:
        raise ValueError(
            f"vectors.f32 大小 {len(raw)} 与 {n}×{d}×4={n*d*4} 不符，索引已损坏"
        )
    flat = memoryview(raw).cast("f")  # 本机与远端都是小端
    _sanity_check_rows(flat, n, d, str(vec_path))

    chips: dict[str, array] = {}
    chips_path = index_dir / "chips.json"
    if chips_path.is_file():
        for item in json.loads(chips_path.read_text(encoding="utf-8")):
            arr = array("f", base64.b64decode(item["vec_b64"]))
            if len(arr) != d:
                raise ValueError(
                    f"chips.json 里「{item['text']}」的向量维度 {len(arr)} != {d}"
                )
            norm = math.sqrt(sum(v * v for v in arr))
            if abs(norm - 1.0) > 1e-2:
                raise ValueError(
                    f"chips.json 里「{item['text']}」的向量范数是 {norm:.4f} 而不是 1"
                )
            chips[item["text"]] = arr

    return SearchIndex(dir=index_dir, meta=meta, _flat=flat, _chips=chips)


def index_dir_for(base: Path | str) -> Path:
    return Path(base) / INDEX_DIRNAME


# ---- 建索引 ---------------------------------------------------------------- #


def build_index(
    video: Path | str,
    out_dir: Path | str,
    client: EncoderClient,
    stride: int = 5,
    jpeg_quality: int = 80,
    chips: list[str] | None = None,
    on_progress=None,
    fps_override: float | None = None,
) -> dict:
    """抽帧 → 送远端编码 → 落盘索引。

    抽帧与缩略图都在本地做（本地有 cv2，后端没有）。送出去的是**缩略图本身**，
    不是原帧：模型反正要压到 224，送 320 宽既省带宽又不影响结果，
    而且和界面展示的图是同一张，不会出现"搜到的和看到的不是同一张"。

    stride 是抽帧间隔（帧）。默认 5 = 0.2 秒一个候选点。
    取太密不会更准（相邻帧内容几乎相同），只是让索引线性变大。
    """
    import cv2

    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"找不到视频：{video}")

    out_dir = Path(out_dir)
    frames_dir = out_dir / "thumbs"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频：{video}")
    fps = fps_override or cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    frames: list[dict] = []
    jpegs: list[bytes] = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            h, w = frame.shape[:2]
            if w > THUMB_WIDTH:
                nh = max(1, round(h * THUMB_WIDTH / w))
                frame = cv2.resize(frame, (THUMB_WIDTH, nh), interpolation=cv2.INTER_AREA)
            ok2, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
            )
            if not ok2:
                raise RuntimeError(f"第 {idx} 帧 JPEG 编码失败")
            data = buf.tobytes()
            name = f"f{idx:05d}.jpg"
            (frames_dir / name).write_bytes(data)
            frames.append(
                {
                    "frame_index": idx,
                    "time_sec": round(idx / fps, 4),
                    "thumb": f"thumbs/{name}",
                }
            )
            jpegs.append(data)
        idx += 1
    cap.release()

    if not frames:
        raise RuntimeError(f"没有抽到任何帧（视频可读吗？）：{video}")

    # 分批送编码：单请求体是 base64，太大既慢又容易被中间层截断
    vectors: list[array] = []
    batch = 48
    t0 = time.time()
    for i in range(0, len(jpegs), batch):
        vectors.extend(client.embed_images(jpegs[i : i + batch]))
        if on_progress:
            on_progress(min(i + batch, len(jpegs)), len(jpegs), time.time() - t0)
    if len(vectors) != len(frames):
        raise RuntimeError(
            f"编码服务返回 {len(vectors)} 条向量，与 {len(frames)} 帧不符 —— 拒绝落盘"
        )
    dim = len(vectors[0])

    # 向量落成裸 float32 二进制（小端）
    flat = array("f")
    for v in vectors:
        flat.extend(v)
    if _needs_byteswap():
        flat.byteswap()
    (out_dir / "vectors.f32").write_bytes(flat.tobytes())

    # 预置查询也编码进来：现场断网时这些示例仍然可用
    chip_list = list(chips if chips is not None else DEFAULT_CHIPS)
    chip_rows: list[dict] = []
    if chip_list:
        try:
            cvecs = client.embed_text(chip_list)
            for text, vec in zip(chip_list, cvecs):
                chip_rows.append(
                    {
                        "text": text,
                        "vec_b64": base64.b64encode(_as_f32(vec).tobytes()).decode("ascii"),
                    }
                )
        except EncoderUnavailable:
            # 预置查询是锦上添花，编不出来不该让整个索引失败
            chip_rows = []
            chip_list = []

    try:
        health = client.health()
        encoder = {
            "model": health.get("model"),
            "weights_sha256": health.get("weights_sha256"),
            "device": health.get("device"),
            "dtype": health.get("dtype"),
            "embed_dim": health.get("embed_dim"),
            "image_resolution": health.get("image_resolution"),
        }
    except EncoderUnavailable:
        encoder = {}

    meta = {
        "index_version": INDEX_VERSION,
        "video": video.name,
        "video_path": str(video),
        "fps": fps,
        "duration_sec": round(total / fps, 3) if total else None,
        "source_frames": total,
        "stride": stride,
        "stride_sec": round(stride / fps, 4),
        "frame_count": len(frames),
        "dim": dim,
        "thumb_width": THUMB_WIDTH,
        "chips": chip_list,
        "chips_cached": len(chip_rows),
        "encoder": encoder,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "index.json").write_text(
        json.dumps({**meta, "frames": frames}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "chips.json").write_text(
        json.dumps(chip_rows, ensure_ascii=False), encoding="utf-8"
    )

    # 回读校验。写盘这一步出错不会有任何提示 —— 只能自己读回来验一遍。
    # 这在"字节序搞反"那次是真出过问题的：写出去的文件表面完全正常，
    # 直到有人真的搜一次才发现分数全是 NaN。
    check = load_index(out_dir)
    if check.count != len(frames) or check.dim != dim:
        raise RuntimeError(
            f"索引写盘后回读不一致：文件里 {check.count}×{check.dim}，"
            f"期望 {len(frames)}×{dim}"
        )

    return {**meta, "elapsed_sec": round(time.time() - t0, 1)}


# ---- 给后端用的小工具 ------------------------------------------------------ #


def service_status(client: EncoderClient | None = None) -> dict:
    """编码服务状态。**不抛异常** —— 它是给健康检查用的。"""
    client = client or EncoderClient()
    try:
        h = client.health()
        return {
            "available": True,
            "url": client.base_url,
            "model": h.get("model"),
            "device": h.get("device"),
            "dtype": h.get("dtype"),
            "weights_sha256": h.get("weights_sha256"),
            "gpu": h.get("gpu") or {},
            "uptime_sec": h.get("uptime_sec"),
        }
    except EncoderUnavailable as exc:
        return {"available": False, "url": client.base_url, "reason": str(exc)}


def fmt_sec(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:05.2f}" if m else f"{s:.2f}s"


def margins(hits: list[Hit], all_scores: list[float] | None = None) -> dict:
    """把「分数」翻译成**人能解释**的东西。

    绝对分数在这个模型上不可解释（见模块头部说明），所以这里给的是相对量：
    第一名与最后一名的差距、以及名次。界面应当展示这个，而不是"相似度 87%"。
    """
    if not hits:
        return {}
    top = hits[0].score
    last = hits[-1].score
    return {
        "top_score": round(top, 4),
        "spread_in_hits": round(top - last, 4),
        "median_score": round(all_scores[len(all_scores) // 2], 4) if all_scores else None,
        "note": "分数是余弦相似度，只用于**排序**；不同查询之间的绝对值不可比",
    }


__all__ = [
    "DEFAULT_CHIPS",
    "DEFAULT_SERVICE_URL",
    "EncoderClient",
    "EncoderUnavailable",
    "Hit",
    "INDEX_DIRNAME",
    "INDEX_VERSION",
    "SearchIndex",
    "build_index",
    "fmt_sec",
    "index_dir_for",
    "load_index",
    "margins",
    "service_status",
]
