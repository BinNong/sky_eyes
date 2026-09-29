"""把 pipeline 的产物导出成网站 demo 可直接消费的数据。

做四件事：
    1. 合并 accident_report.json / accident_events.json / accident_frames.json
       -> web/public/data/demo.json（前端只需一次 fetch）
    2. 复制 18 张代表帧 ROI -> web/public/frames/roi/
    3. 用 OpenCV 从原视频抽取代表帧的**完整帧** -> web/public/frames/full/
       （详情页要展示"原图上叠加检测框"，光有 ROI 裁剪图不够）
    4. 把标注视频转成 H.264 放到 web/public/media/
       （OpenCV 默认写的 mp4v 是 MPEG-4 Part 2，浏览器不支持，必须转）

用法：
    python scripts/export_demo_data.py
    python scripts/export_demo_data.py --no-video         # 不搬视频
    python scripts/export_demo_data.py --video-scale 0.75 # 缩到 960x540 减体积

注意：本机没有 ffmpeg，转码走 OpenCV 自带的 avc1（H.264）编码器。
     若该环境没有 H.264 编码器，可用 macOS 自带的 /usr/bin/avconvert 兜底。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DEST = PROJECT_ROOT / "web" / "public"

# 与 pipeline.py 的 _SEVERITY_ORDER 保持一致，前端排序也必须用它
SEVERITY_ORDER = {"低": 0, "中": 1, "高": 2}


def _load_json(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {path}\n请先跑完整流程：python pipeline.py")
    return json.loads(path.read_text(encoding="utf-8"))


def copy_rois(output_dir: Path, roi_rel_paths: list[str], dst: Path) -> dict[str, str]:
    """把代表帧 ROI 复制到 web 目录，返回 {原相对路径: web 内路径}。"""
    dst.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for rel in roi_rel_paths:
        src = output_dir / rel
        if not src.is_file():
            missing.append(rel)
            continue
        shutil.copy2(src, dst / src.name)
        mapping[rel] = f"frames/roi/{src.name}"
    if missing:
        print(f"  [警告] {len(missing)} 张 ROI 缺失，例如 {missing[:3]}")
    return mapping


def extract_full_frames(
    video_path: Path, wanted: set[int], dst: Path, quality: int = 88
) -> dict[int, str]:
    """按帧号抽取完整视频帧，返回 {帧号: web 内路径}。

    逐帧顺序读而不是 seek：部分编码下 CAP_PROP_POS_FRAMES 定位不准，
    顺序读到最大目标帧即可（本视频 959 帧，解码只要几秒）。
    """
    dst.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    last = max(wanted)
    saved: dict[int, str] = {}
    idx = 0
    while idx <= last:
        ok, frame = cap.read()
        if not ok:
            break
        if idx in wanted:
            name = f"{idx:04d}.jpg"
            cv2.imwrite(
                str(dst / name), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            )
            saved[idx] = f"frames/full/{name}"
        idx += 1
    cap.release()
    return saved


def probe_codec(path: Path) -> str:
    """读视频的 FOURCC。'mp4v' / 'FMP4' = MPEG-4 Part 2，浏览器不支持。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return ""
    fc = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    return "".join(chr((fc >> 8 * i) & 0xFF) for i in range(4))


def transcode_h264(src: Path, dst: Path, scale: float = 1.0) -> dict:
    """转成浏览器能播的 H.264。

    ⚠ 这是被发现的一个真实坑：OpenCV 的 VideoWriter 默认写 `mp4v`，
    那是 **MPEG-4 Part 2**，Chrome / Safari / Firefox 全都不支持 —— 网页里
    视频区域会是一片黑，而且控制台不会报任何错，非常难排查。

    逐帧转码，959 帧 720p 大约几秒。
    """
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开源视频：{src}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h0 = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # H.264 要求宽高为偶数
    w = max(2, int(w0 * scale) // 2 * 2)
    h = max(2, int(h0 * scale) // 2 * 2)

    dst.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(dst), cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("H.264 编码器不可用（avc1 打不开）。可改用 macOS 自带的 avconvert")

    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if (w, h) != (w0, h0):
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        writer.write(frame)
        frames += 1

    cap.release()
    writer.release()
    return {"frames": frames, "size": (w, h), "fps": fps}


def build_demo(report: dict, frames_meta: dict, roi_map: dict, full_map: dict) -> dict:
    """合并成前端消费的单文件数据。"""
    fps = frames_meta.get("fps") or report.get("fps") or 25.0
    total_frames = frames_meta.get("total_frames") or 0

    # 帧 -> 事件 的归属表，供底部时间轴使用
    frame_to_event: dict[int, int] = {}
    for event in report["events"]:
        for f in range(event["start_frame"], event["end_frame"] + 1):
            frame_to_event[f] = event["event_id"]

    events = []
    for event in report["events"]:
        reps_by_frame = {r["frame_index"]: r for r in event.get("representatives", [])}
        reps = []
        for obs in event["observations"]:
            rep = reps_by_frame.get(obs["frame_index"], {})
            reps.append(
                {
                    "frame_index": obs["frame_index"],
                    "time_sec": rep.get("time_sec"),
                    "confidence": obs.get("confidence"),
                    "bbox": rep.get("bbox"),
                    "num_accident_boxes": rep.get("num_accident_boxes", 1),
                    "roi": roi_map.get(obs.get("roi", ""), obs.get("roi")),
                    "full": full_map.get(obs["frame_index"]),
                    "severity": obs.get("severity"),
                    "severity_source": obs.get("severity_source"),
                    "description": obs.get("description"),
                    "ok": obs.get("ok", False),
                    "ambiguous": obs.get("ambiguous", False),
                    "degenerate": obs.get("degenerate", False),
                    "is_lead": obs["frame_index"] == event.get("event_severity_frame"),
                }
            )
        lead = next((r for r in reps if r["is_lead"]), reps[0] if reps else None)
        events.append(
            {
                "event_id": event["event_id"],
                "start_sec": event["start_sec"],
                "end_sec": event["end_sec"],
                "start_frame": event["start_frame"],
                "end_frame": event["end_frame"],
                "num_frames": event["num_frames"],
                "duration_sec": round(event["end_sec"] - event["start_sec"], 2),
                "peak_confidence": event["peak_confidence"],
                "event_severity": event.get("event_severity"),
                "event_severity_frame": event.get("event_severity_frame"),
                "event_severity_levels": event.get("event_severity_levels", []),
                "representatives": reps,
                "lead": lead,
            }
        )

    # 事件级排序：高 -> 低，同级按时间。前端一律消费这个顺序，不要自己比中文字符串
    events.sort(
        key=lambda e: (
            -SEVERITY_ORDER.get(e["event_severity"] or "", -1),
            e["start_sec"],
        )
    )

    timeline = [
        {
            "frame_index": f["frame_index"],
            "time_sec": f["time_sec"],
            "confidence": f["confidence"],
            "bbox": f.get("bbox"),
            "event_id": frame_to_event.get(f["frame_index"]),
        }
        for f in frames_meta.get("frames", [])
    ]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": {"zh": "交通事故监测预警与报告解读", "en": "Traffic Accident Detection & Alerting"},
        "video": {
            "src": "media/detections.mp4",
            "fps": fps,
            "width": frames_meta.get("width"),
            "height": frames_meta.get("height"),
            "total_frames": total_frames,
            "duration_sec": round(total_frames / fps, 2) if total_frames and fps else None,
        },
        "severity_order": SEVERITY_ORDER,
        "stats": report["stats"],
        "source": {
            "video_path": report.get("video"),
            "weights": report.get("weights"),
            "conf_threshold": report.get("conf_threshold"),
            "temperature": report.get("temperature"),
            "api_base": report.get("api_base"),
        },
        "events": events,
        "timeline": timeline,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出网站 demo 所需数据")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="pipeline 产物目录")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="web/public 目录")
    parser.add_argument("--no-video", action="store_true", help="不导出标注视频（省 30+MB）")
    parser.add_argument(
        "--video-scale",
        type=float,
        default=1.0,
        help="视频缩放比例。0.75 约可减半体积，检测框是按百分比定位的，缩放不影响叠加",
    )
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="强制重新转码（默认情况下目标已是 H.264 且不比源旧就跳过，省约 29 秒）",
    )
    parser.add_argument("--quality", type=int, default=88, help="抽帧 JPEG 质量")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    dest = Path(args.dest).resolve()

    print("=" * 60)
    print("[导出] pipeline 产物 -> 网站 demo 数据")
    print("=" * 60)
    print(f"  源：{output_dir}")
    print(f"  目标：{dest}")
    print()

    report = _load_json(output_dir / "accident_report.json")
    frames_meta = _load_json(output_dir / "accident_frames.json")

    # 1) ROI
    roi_rels = [o["roi"] for e in report["events"] for o in e["observations"]]
    roi_map = copy_rois(output_dir, roi_rels, dest / "frames" / "roi")
    print(f"[1/4] 代表帧 ROI：{len(roi_map)}/{len(roi_rels)} 张 -> frames/roi/")

    # 2) 完整帧
    wanted = {o["frame_index"] for e in report["events"] for o in e["observations"]}
    video_path = Path(report["video"])
    if not video_path.is_absolute():
        video_path = PROJECT_ROOT / video_path
    full_map = extract_full_frames(video_path, wanted, dest / "frames" / "full", args.quality)
    print(f"[2/4] 完整帧（用于叠加检测框）：{len(full_map)}/{len(wanted)} 帧 -> frames/full/")

    # 3) 合并数据
    demo = build_demo(report, frames_meta, roi_map, full_map)
    data_dir = dest / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "demo.json").write_text(
        json.dumps(demo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    size_kb = (data_dir / "demo.json").stat().st_size / 1024
    print(f"[3/4] 合并数据：demo.json（{size_kb:.0f} KB，{len(demo['events'])} 事件）")

    # 4) 视频：必须落到浏览器能播的 H.264
    media_dir = dest / "media"
    if args.no_video:
        print("[4/4] 视频：已跳过（--no-video）")
    else:
        src_video = output_dir / "detections.mp4"
        dst_video = media_dir / "detections.mp4"
        if not src_video.is_file():
            print(f"[4/4] [警告] 找不到 {src_video}")
        else:
            media_dir.mkdir(parents=True, exist_ok=True)
            src_codec = probe_codec(src_video)
            dst_codec = probe_codec(dst_video) if dst_video.is_file() else ""

            # 判据必须看**目标**文件。只看源的话，因为源 output/detections.mp4 一直是
            # mp4v，每次导出都会重新转码 —— 实测白等 29 秒，而这轮开发里跑了 3 次。
            dst_usable = (
                dst_video.is_file()
                and dst_codec.lower() in {"avc1", "h264"}
                and dst_video.stat().st_mtime >= src_video.stat().st_mtime
            )

            if args.reencode:
                action = "transcode"
            elif dst_usable:
                action = "skip"
            elif src_codec.lower() in {"avc1", "h264"}:
                action = "copy"
            else:
                action = "transcode"

            if action == "skip":
                mb = dst_video.stat().st_size / 1048576
                print(f"[4/4] 视频：目标已是 H.264 且不比源旧，跳过转码（复用 {mb:.1f} MB）")
            elif action == "copy":
                shutil.copy2(src_video, dst_video)
                mb = dst_video.stat().st_size / 1048576
                print(f"[4/4] 视频：源已是 H.264（{src_codec}），直接复制 {mb:.1f} MB")
            else:
                info = transcode_h264(src_video, dst_video, args.video_scale)
                mb = dst_video.stat().st_size / 1048576
                print(
                    f"[4/4] 视频：{src_codec} -> H.264 转码完成"
                    f"（{info['size'][0]}×{info['size'][1]}，{mb:.1f} MB）"
                )

    dist = demo["stats"]["event_severity_distribution"]
    print()
    print("=" * 60)
    print("[完成] 数据已就绪")
    print("=" * 60)
    print(f"  事件级等级：高 {dist['高']} / 中 {dist['中']} / 低 {dist['低']}")
    print(f"  时间轴点位：{len(demo['timeline'])} 个")
    print(f"  视频时长：{demo['video']['duration_sec']} 秒 / {demo['video']['total_frames']} 帧")
    print(f"  输出目录：{dest}")
    video_path = dest / "media" / "detections.mp4"
    if video_path.is_file():
        print(
            f"  视频：H.264 {video_path.stat().st_size / 1048576:.1f} MB"
            "（浏览器可直接播放）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
