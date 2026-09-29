"""交通事故图片的多模态理解客户端。

服务端：远程服务器上的 Janus FastAPI 服务（连接信息见 remote_server.md）。
Janus **只绑 `127.0.0.1:8000`**（2026-09-29 起；本服务无认证，不再对同网段暴露），
所以从本机调用**必须先建隧道**：`./scripts/janus_tunnel.sh`（仅建隧道）或
`./scripts/janus_up.sh`（启服务 + 建隧道 + 验证），再用 `--api-base http://127.0.0.1:8000`
（实测往返 1.3s 完成一次图像理解）。在服务器本机上跑则无需隧道。
地址也可用 JANUS_API_BASE 环境变量覆盖。

用法：
    # 单张图片
    python multimodal_understanding.py output/accident_frames/accident_frame_0084_roi.jpg

    # 整个目录批量推理，结果写入 JSON
    # ⚠ 输出路径别写成 output/accident_report.json——那是 pipeline.py 的产物，
    #   格式也不同（这里是逐图清单），照着覆盖会把演示报告毁掉。
    python multimodal_understanding.py output/accident_frames -o /tmp/janus_batch.json

    # 自定义问题 / 自定义服务地址 / 采样温度（0 = 贪心，默认）
    python multimodal_understanding.py xxx.jpg -q "图中是否有人员受伤？"
    python multimodal_understanding.py xxx.jpg --api-base http://127.0.0.1:8000
    python multimodal_understanding.py xxx.jpg -t 0.1
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

DEFAULT_API_BASE = os.environ.get("JANUS_API_BASE", "http://127.0.0.1:8000")
DEFAULT_QUESTION = "请描述一下图中的交通事故,同时给出事故严重等级（低，中，高）"
# 贪心解码。实测 temperature=0.1 时 Janus 容易陷入重复输出
#（同一段话带不同等级反复出现），temperature=0 的回答干净且稳定。
DEFAULT_TEMPERATURE = 0.0

UNDERSTAND_PATH = "/understand_image_and_question/"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _make_session(api_base: str) -> requests.Session:
    """构造会话；访问本机回环地址时忽略系统代理，避免被 HTTP_PROXY 劫持。"""
    session = requests.Session()
    host = requests.utils.urlparse(api_base).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        session.trust_env = False
    return session


class JanusClient:
    """Janus 多模态服务客户端。"""

    def __init__(self, api_base: str = DEFAULT_API_BASE, timeout: int = 300):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.session = _make_session(self.api_base)

    def health(self) -> dict:
        """查询服务状态。"""
        resp = self.session.get(f"{self.api_base}/health", timeout=20)
        resp.raise_for_status()
        return resp.json()

    def understand_image(
        self,
        image_path,
        question: str = DEFAULT_QUESTION,
        seed: int = 42,
        top_p: float = 0.95,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        """上传一张图片，返回模型给出的文字描述。"""
        image_path = Path(image_path)
        if not image_path.is_file():
            raise FileNotFoundError(f"图片不存在: {image_path}")

        with image_path.open("rb") as f:
            files = {"file": (image_path.name, f, "image/jpeg")}
            data = {
                "question": question,
                "seed": seed,
                "top_p": top_p,
                "temperature": temperature,
            }
            resp = self.session.post(
                f"{self.api_base}{UNDERSTAND_PATH}",
                files=files,
                data=data,
                timeout=self.timeout,
            )
        resp.raise_for_status()
        return resp.json()["response"]

    def understand_folder(
        self,
        folder,
        question: str = DEFAULT_QUESTION,
        output_json=None,
        temperature: float = DEFAULT_TEMPERATURE,
        verbose: bool = True,
    ) -> list:
        """对目录下所有图片逐张推理，可选写出 JSON 报告。"""
        folder = Path(folder)
        images = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        if not images:
            raise FileNotFoundError(f"目录下没有图片: {folder}")

        results = []
        for idx, image_path in enumerate(images, 1):
            record = {"image": image_path.name, "path": str(image_path)}
            try:
                record["description"] = self.understand_image(
                    image_path, question, temperature=temperature
                )
                record["ok"] = True
            except Exception as exc:  # 批量场景下不让单张失败中断整体流程
                record["description"] = None
                record["ok"] = False
                record["error"] = str(exc)

            results.append(record)
            if verbose:
                status = "OK " if record["ok"] else "ERR"
                print(f"[{idx}/{len(images)}] {status} {image_path.name}")
                if record["ok"]:
                    print(f"    {record['description']}")
                else:
                    print(f"    {record['error']}")

        if output_json:
            output_json = Path(output_json)
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if verbose:
                print(f"\n结果已写入: {output_json}")

        return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="交通事故图片多模态理解客户端")
    parser.add_argument("path", help="图片路径，或包含图片的目录")
    parser.add_argument("-q", "--question", default=DEFAULT_QUESTION, help="提问内容")
    parser.add_argument("-o", "--output", help="批量模式下的 JSON 输出路径")
    parser.add_argument(
        "-t",
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"采样温度，0 为贪心解码（默认 {DEFAULT_TEMPERATURE}）",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"服务地址，默认 {DEFAULT_API_BASE}",
    )
    args = parser.parse_args(argv)

    client = JanusClient(api_base=args.api_base)

    try:
        info = client.health()
        print(f"[服务] {args.api_base}  device={info.get('device')}  cuda={info.get('cuda')}")
    except Exception as exc:
        print(f"[错误] 无法连接服务 {args.api_base}: {exc}", file=sys.stderr)
        print("       请确认服务已启动，且 SSH 隧道已建立（scripts/janus_tunnel.sh）", file=sys.stderr)
        return 1

    target = Path(args.path)
    if target.is_dir():
        client.understand_folder(
            target, args.question, args.output, temperature=args.temperature
        )
    else:
        print(client.understand_image(target, args.question, temperature=args.temperature))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
