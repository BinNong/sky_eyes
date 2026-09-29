#!/usr/bin/env python
"""为演示视频建「文字找帧」索引。

     python scripts/build_search_index.py                         # 默认视频 + 默认 stride
     python scripts/build_search_index.py --stride 1              # 逐帧（更密，索引更大）
     python scripts/build_search_index.py --video other.mp4 --out web/public/data/search

产物落在 `web/public/data/search/`（前端静态目录），所以：
  · 缩略图由 vite 直接当静态文件发，后端不用当图床
  · 后端只读 `index.json` + `vectors.f32` 做排序

先跑一次 `python scripts/build_search_index.py --check` 可以只体检编码服务。

⚠ 建索引需要**远端编码服务在线**（scripts/search_deploy.sh + scripts/search_tunnel.sh）。
   这不是设计缺陷而是刻意的：模型不放在本地（本地无 GPU，实测 890 ms/帧 → 38 秒的
   视频要 14 分钟）。索引建好之后，预置查询在断网时仍可用（向量已随索引落盘）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from video_search import (  # noqa: E402
    DEFAULT_CHIPS,
    EncoderClient,
    EncoderUnavailable,
    build_index,
    load_index,
)

DEFAULT_VIDEO = ROOT / "source" / "raw_videos" / "burstling_street.mp4"
DEFAULT_OUT = ROOT / "web" / "public" / "data" / "search"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="为视频建可检索索引（中文 CLIP 向量）")
    ap.add_argument("--video", default=str(DEFAULT_VIDEO))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--stride",
        type=int,
        default=5,
        help="抽帧间隔（帧）。默认 5 = 25fps 下 0.2 秒一个候选点。取太密不会更准"
        "（相邻帧内容几乎一样），只是让索引线性变大、传输时间线性变长。",
    )
    ap.add_argument("--quality", type=int, default=80, help="缩略图 JPEG 质量")
    ap.add_argument("--url", default=None, help="编码服务地址（默认取 SKYEYES_CLIP_URL）")
    ap.add_argument("--check", action="store_true", help="只体检编码服务，不建索引")
    ap.add_argument("--force", action="store_true", help="已有索引也重建")
    ap.add_argument("--chips", default=None, help="预置查询，逗号分隔（会一起编码进索引）")
    args = ap.parse_args(argv)

    client = EncoderClient(args.url)

    print("=" * 66)
    print("编码服务体检")
    print("=" * 66)
    try:
        h = client.health()
    except EncoderUnavailable as exc:
        print(f"  ❌ {exc}")
        print()
        print("  先确认远端服务与隧道：")
        print("    ./scripts/search_deploy.sh      # 部署 + 起服务（需要远端可达）")
        print("    ./scripts/search_tunnel.sh      # 本地 :8001 -> 远端 :8001")
        return 1
    print(f"  ✅ {h['model']}  device={h['device']} dtype={h['dtype']}")
    print(f"     权重指纹 {h['weights_sha256']}  embed_dim={h['embed_dim']}  词表 {h['vocab_size']}")
    gpu = h.get("gpu") or {}
    if gpu:
        print(f"     GPU {gpu.get('name')}  空闲 {gpu.get('mem_free_mb')} MB / {gpu.get('mem_total_mb')} MB")
    if args.check:
        # 顺手验一次文本编码，确认服务不只会报健康
        try:
            v = client.embed_text(["黄色的小汽车"])
            print(f"  ✅ 文本编码跑通，维度 {len(v[0])}")
        except EncoderUnavailable as exc:
            print(f"  ❌ 文本编码失败：{exc}")
            return 1
        return 0

    video = Path(args.video)
    out = Path(args.out)
    if not video.is_file():
        print(f"\n[错误] 找不到视频：{video}", file=sys.stderr)
        return 1

    if (out / "index.json").is_file() and not args.force:
        try:
            old = load_index(out)
            print(f"\n[跳过] {out} 已有索引（{old.count} 帧，stride {old.stride}）。")
            print("       要重建加 --force")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"\n[注意] 已有索引读不出来（{exc}），将重建")

    chips = [c.strip() for c in args.chips.split(",")] if args.chips else DEFAULT_CHIPS

    print()
    print("=" * 66)
    print(f"建索引  {video.name}  stride={args.stride}  输出 {out}")
    print("=" * 66)

    last = {"pct": -1}

    def on_progress(done: int, total: int, elapsed: float) -> None:
        pct = int(done / total * 100)
        if pct != last["pct"]:
            last["pct"] = pct
            rate = done / elapsed if elapsed > 0 else 0
            remain = (total - done) / rate if rate > 0 else 0
            print(
                f"  编码 {done}/{total} ({pct}%)  {rate:.1f} 帧/秒  剩余约 {remain:.0f}s",
                flush=True,
            )

    try:
        meta = build_index(
            video=video,
            out_dir=out,
            client=client,
            stride=args.stride,
            jpeg_quality=args.quality,
            chips=chips,
            on_progress=on_progress,
        )
    except EncoderUnavailable as exc:
        print(f"\n[错误] 编码服务在过程中不可达：{exc}", file=sys.stderr)
        print("       索引未落盘（video_search.build_index 先收齐全部向量才写文件）",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    print("=" * 66)
    print("完成")
    print("=" * 66)
    print(f"  视频      {meta['video']}  {meta['source_frames']} 帧 / {meta['duration_sec']}s @ {meta['fps']:.0f}fps")
    print(f"  索引      {meta['frame_count']} 个候选点（每 {meta['stride']} 帧 = {meta['stride_sec']}s）")
    print(f"  维度      {meta['dim']}   预置查询 {meta['chips_cached']} 条")
    print(f"  耗时      {meta['elapsed_sec']}s")
    print(f"  索引体积  {_fmt_bytes(_dir_size(out))}  ->  {out}")
    print()
    print("  下一步：前端 /evidence 页可直接用它（后端会自动发现这个目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
