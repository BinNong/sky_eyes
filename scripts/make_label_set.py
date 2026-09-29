#!/usr/bin/env python3
"""生成用于评估**检测召回率**的待标注集。

为什么要做这件事
    P8 让 `/analytics` 能算"模型报出来的事件里有多少是真的"（精确率），
    但仍算不出**召回率**——因为没有人看过"模型**没报**的那些帧"。
    而召回率恰恰是交管场景最在意的：**漏报一起高等级事故，比多报十次更严重**。

    算召回率没有捷径，必须有人真的看过那些没被检出的帧。
    这个脚本负责把"该看哪些帧"选出来——**选得好不好，直接决定这个指标可不可信**。

三层抽样，按"漏检概率"分层（混在一起算会让指标失真）

    inside    事故区间**内部**却没被检出的帧   → 漏检概率最高
    boundary  事故区间**外侧** span 帧内的未检出帧 → 漏检概率高（事故起止被截断）
    far       离任何事故区间都超过 span 帧      → 漏检概率低

    ⚠ 分层依据必须是**离事故区间的距离**，不能是"离最近检出帧的距离"。
      后者看着更统一，但会漏掉一个关键事实：**区间内部存在未检出的空洞**
      （实测：事件 3 的 10 帧里漏了 6 帧，全视频共 45 帧）。
      那些帧距最近的检出帧可能很远，却处在事故正中间、漏检概率极高。
      按"离检出帧距离"分层会把它们划进 `far`，从而**系统性低估漏检率**。

    ⚠ `inside` 层的估计值天然接近 100%。它是真实结论，不是 bug——
      但界面上要讲清楚：这 45 帧是"事故还在持续、模型却断了"的部分。

抽样固定 seed，同一份产物任何时候重跑都得到同一组帧。

用法
    /path/to/visual_search/bin/python scripts/make_label_set.py
    ... --detected 16 --inside 16 --boundary 16 --far 32 --seed 20260921
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FRAMES = PROJECT_ROOT / "output" / "accident_frames.json"
DEFAULT_EVENTS = PROJECT_ROOT / "output" / "accident_events.json"
DEFAULT_DEST = PROJECT_ROOT / "web" / "public" / "labeling"

#: 边界层向事故区间外侧看几帧。取 4 是经验值——25fps 下约 0.16 秒，
#: 足够覆盖"事故刚开始 / 刚结束"这种模型容易漏掉的一两帧。
BOUNDARY_SPAN = 4

STRATA = ("detected", "inside", "boundary", "far")


def build_strata(
    events: list[dict], checked: set[int], total: int, span: int
) -> tuple[set[int], set[int], set[int]]:
    """把"未检出的帧"按漏检概率分成三层。三层互斥。

    inside   —— 落在事故区间内、却没被检出。事故还在持续，模型却断了。
    boundary —— 紧贴区间外侧 span 帧内、未检出。事故的起止最容易被截断。
    far      —— 其余。离任何事故区间都超过 span 帧。
    """
    inside: set[int] = set()
    boundary: set[int] = set()
    for e in events:
        start, end = e.get("start_frame"), e.get("end_frame")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        for f in range(start, end + 1):
            if 1 <= f <= total and f not in checked:
                inside.add(f)
        for k in range(1, span + 1):
            for f in (start - k, end + k):
                if 1 <= f <= total and f not in checked:
                    boundary.add(f)

    # 互斥。区间极短时（如事件 4 只有 4 帧），inside 与 boundary 可能撞上，
    # 优先级给 inside——它离事故更近、漏检概率更高，放到 boundary 会低估。
    boundary -= inside
    return inside, boundary


def extract_frames(
    video_path: Path, wanted: set[int], dst: Path, width: int, quality: int
) -> dict[int, str]:
    """按帧号抽图，返回 {帧号: 文件名}。

    逐帧顺序读而不是 seek：部分编码下 `CAP_PROP_POS_FRAMES` 定位不准，
    会静默读到相邻帧——那样标出来的"真值"就是错的，而且没人会发现。
    本视频 959 帧，顺序解码只要几秒。

    帧号是 **1-based**（首帧 = 1），与 `accident_frames.json` 的 `frame_index`
    以及 ROI 文件名 `accident_frame_0049_roi.jpg` 对齐。
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    last = max(wanted) if wanted else 0
    dst.mkdir(parents=True, exist_ok=True)
    saved: dict[int, str] = {}
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx in wanted:
            h, w = frame.shape[:2]
            if width and w > width:
                nh = int(round(h * width / w))
                frame = cv2.resize(frame, (width, nh), interpolation=cv2.INTER_AREA)
            name = f"f{idx:05d}.jpg"
            cv2.imwrite(
                str(dst / name), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            )
            saved[idx] = name
        if idx >= last:
            break
    cap.release()
    return saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成检测召回率的待标注集")
    ap.add_argument("--frames-json", default=str(DEFAULT_FRAMES))
    ap.add_argument("--events-json", default=str(DEFAULT_EVENTS))
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="输出目录（web/public 下）")
    ap.add_argument("--detected", type=int, default=16, help="检出帧抽样数")
    ap.add_argument("--inside", type=int, default=16, help="区间内未检出帧的抽样数")
    ap.add_argument("--boundary", type=int, default=16, help="区间外侧未检出帧的抽样数")
    ap.add_argument("--far", type=int, default=32, help="远离区未检出帧的抽样数")
    ap.add_argument("--span", type=int, default=BOUNDARY_SPAN)
    ap.add_argument("--seed", type=int, default=20260921)
    ap.add_argument("--width", type=int, default=960, help="抽帧宽度（0 = 原始尺寸）")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 质量")
    args = ap.parse_args(argv)

    frames_path = Path(args.frames_json)
    events_path = Path(args.events_json)
    for p in (frames_path, events_path):
        if not p.is_file():
            print(f"[错误] 找不到 {p}\n        请先跑：python pipeline.py")
            return 1

    meta = json.loads(frames_path.read_text(encoding="utf-8"))
    total = int(meta["total_frames"])
    fps = float(meta.get("fps") or 25.0)
    video = Path(meta["video"])

    conf_of: dict[int, float | None] = {
        int(f["frame_index"]): f.get("confidence") for f in meta["frames"]
    }
    checked = set(conf_of)
    events = json.loads(events_path.read_text(encoding="utf-8"))

    inside_pool, boundary_pool = build_strata(events, checked, total, args.span)
    unchecked = {i for i in range(1, total + 1) if i not in checked}
    far_pool = sorted(unchecked - inside_pool - boundary_pool)

    print("=" * 62)
    print("检测召回率 · 待标注集生成")
    print("=" * 62)
    print(f"  视频            {total} 帧 @ {fps:g}fps")
    print(f"  已检出          {len(checked)} 帧")
    print(f"  未检出          {len(unchecked)} 帧，按漏检概率分三层：")
    print(f"    ├ inside      {len(inside_pool):>3} 帧  事故区间内却没报（漏检概率最高）")
    print(f"    ├ boundary    {len(boundary_pool):>3} 帧  区间外侧 {args.span} 帧内未报")
    print(f"    └ far         {len(far_pool):>3} 帧  离区间均超 {args.span} 帧")
    print()

    wanted_counts = {
        "detected": args.detected,
        "inside": args.inside,
        "boundary": args.boundary,
        "far": args.far,
    }
    pools = {
        "detected": sorted(checked),
        "inside": sorted(inside_pool),
        "boundary": sorted(boundary_pool),
        "far": far_pool,
    }

    rng = random.Random(args.seed)
    picks: list[tuple[int, str]] = []
    for stratum in STRATA:
        pool = pools[stratum]
        want = wanted_counts[stratum]
        if want > len(pool):
            print(f"  [提示] {stratum} 层请求 {want} 帧但总体只有 {len(pool)} 帧，按 {len(pool)} 处理")
            want = len(pool)
        picks += [(i, stratum) for i in sorted(rng.sample(pool, want))]

    print(f"  抽样合计        {len(picks)} 帧（seed={args.seed}）")
    print()

    dest = Path(args.dest)
    frame_dir = dest / "frames"
    wanted = {i for i, _ in picks}
    saved = extract_frames(video, wanted, frame_dir, args.width, args.quality)
    print(f"  抽帧完成        {len(saved)}/{len(wanted)} 张 -> {frame_dir.relative_to(PROJECT_ROOT)}")

    if len(saved) != len(wanted):
        missing = sorted(wanted - set(saved))
        print(f"  [警告] 有 {len(missing)} 帧没抽出来：{missing[:10]}")

    items = []
    for idx, stratum in picks:
        if idx not in saved:
            continue
        items.append(
            {
                "id": f"f{idx:05d}",
                "frame_index": idx,
                "time_sec": round((idx - 1) / fps, 3),
                "stratum": stratum,
                "detected": idx in checked,
                "confidence": conf_of.get(idx),
                "image": f"labeling/frames/{saved[idx]}",
            }
        )

    payload = {
        "version": 1,
        "seed": args.seed,
        "video": {
            "src": video.name,
            "total_frames": total,
            "fps": fps,
            "width": int(meta.get("width") or 0),
            "height": int(meta.get("height") or 0),
        },
        "conf_threshold": meta.get("conf_threshold"),
        # 抽样口径全部写进文件：指标必须带上"基于多少样本、各层总体多大"，
        # 否则前端只能拿到一个光秃秃的百分比，没法说清它代表什么。
        "strata": {
            s: {
                "sampled": sum(1 for _, x in picks if x == s),
                "population": len(pools[s]),
            }
            for s in STRATA
        },
        "detected_frames": len(checked),
        "unchecked_frames": len(unchecked),
        "boundary_span": args.span,
        "items": items,
    }

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "set.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=" * 62)
    print("[完成] 待标注集已生成")
    print("=" * 62)
    for s in STRATA:
        d = payload["strata"][s]
        print(f"  {s:9} 抽 {d['sampled']:>3} / 总体 {d['population']:>4}")
    print(f"  清单 -> {(dest / 'set.json').relative_to(PROJECT_ROOT)}")
    print()
    print("  下一步：到网站 /labeling 页逐帧判定「有事故 / 无事故」。")
    print("         标完后，数据看板上的召回率与漏检率才会出数字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
