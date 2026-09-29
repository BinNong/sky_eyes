"""事故检测端：监控视频 -> 事故帧 ROI 图片 + 结构化元数据。

这是最初那个一次性脚本的重构版，把它改造成可被 pipeline.py
导入的函数模块，同时补齐了元数据记录与事故事件聚类。

（那个原始脚本已于 2026-09-22 删除：它是无函数的顶层代码，硬编码路径，
**直接往 `output/` 写**——而 `output/` 是静态演示数据源，误运行一次就会覆盖它。
下面这些差异同时就是当初重构的理由。）

与原脚本的差异：
  - 不再默认弹出 cv2.imshow 窗口（服务器/无 GUI 环境会崩），需要时加 --show
  - 取出 accident 类的置信度、bbox、时间戳，落盘为 JSON
  - 一帧内多个 accident 框时，取置信度最高的框作为 ROI（原实现会互相覆盖）
  - 新增 group_events()：把连号帧聚成「事故事件」，供后续抽样送多模态
  - 标注视频**保证是浏览器可播的 H.264**：拿不到 avc1 编码器时自动用 ffmpeg
    转码（部署到服务器后走的就是这条——pip 的 opencv wheel 不含 H.264 编码器），
    而不是退回浏览器一片黑的 mp4v。编码方式会记进 accident_frames.json：
    `video_encode_mode` = direct-h264 / h264-ffmpeg / mp4v-only

用法：
    python detector.py                          # 默认权重 + 默认视频
    python detector.py -v other.mp4 --conf 0.5  # 指定视频与置信度阈值
    python detector.py --no-video               # 不生成标注视频
    python detector.py --gap 12 --frames-per-event 2
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
from pathlib import Path

import cv2
from ultralytics import YOLO

# 视频源判定与 detector/pipeline/后端三处共用。**它是纯 stdlib 的**——
# 后端（server/.venv，没有 torch）也要 import 它，见 video_source.py 的说明。
from video_source import is_stream_source, source_display_name

ROOT = Path(__file__).resolve().parent

DEFAULT_WEIGHTS = ROOT / "models" / "trained_model.pt"
DEFAULT_VIDEO = ROOT / "source" / "raw_videos" / "burstling_street.mp4"
DEFAULT_OUTPUT_DIR = ROOT / "output"

ACCIDENT_CLASS = "accident"
ROI_PATTERN = "accident_frame_*_roi.jpg"


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def _accident_class_ids(model: YOLO) -> set:
    """找出名字为 accident 的类别 id（兼容大小写）。"""
    names = model.names
    items = names.items() if isinstance(names, dict) else enumerate(names)
    return {int(cid) for cid, name in items if str(name).lower() == ACCIDENT_CLASS}


def _make_colors(model: YOLO, accident_ids: set) -> dict:
    """给每个类别分配颜色，accident 固定红色（BGR）。"""
    names = model.names
    items = names.items() if isinstance(names, dict) else enumerate(names)
    colors = {}
    for cid, _ in items:
        cid = int(cid)
        colors[cid] = (
            (0, 0, 255)
            if cid in accident_ids
            else (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        )
    return colors


def _stale_rois(frames_dir: Path) -> int:
    """统计目录里已有的 ROI 图片数量（只统计，不删除）。

    本脚本刻意不做自动清理：旧图片留着不影响流程（下游一律以本次生成的
    accident_frames.json 为准，不会去遍历目录），但换了 --conf 等参数后
    可能有上一轮残留的文件同名不成对。需要干净目录时请自行删除。
    """
    return sum(1 for _ in frames_dir.glob(ROI_PATTERN))


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _replace_quietly(src: Path, dst: Path) -> None:
    """转码失败时把 mp4v 临时文件挪到最终位置——至少别把这份视频弄丢。"""
    try:
        if dst.exists():
            dst.unlink()
        src.replace(dst)
    except OSError:
        pass


def _transcode_to_h264(src: Path, dst: Path, verbose: bool = True) -> str:
    """把 OpenCV 写出的 mp4v 转成浏览器能播的 H.264。

    返回最终生效的编码方式：`h264-ffmpeg` 成功；`mp4v-only` 失败退回。

    **转码失败不抛异常**：检测本身是几分钟的活，不该因为一个编码器把整轮结果丢掉。
    但要留下看得懂的警告，而不是让一个播不了的文件悄悄流到前端。
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",      # 4:2:0 是浏览器兼容性最好的像素格式
        "-movflags", "+faststart",  # moov 前置：边下边播，演示时不用等整个文件
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[警告] ffmpeg 转码失败（{type(exc).__name__}: {exc}）："
            "标注视频仍是 mp4v，**浏览器无法播放**"
        )
        _replace_quietly(src, dst)
        return "mp4v-only"

    if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
        src.unlink(missing_ok=True)
        if verbose:
            print(f"[检测] 标注视频已转成 H.264 -> {dst.name}")
        return "h264-ffmpeg"

    print("[警告] ffmpeg 转码返回非零：标注视频仍是 mp4v，**浏览器无法播放**")
    if proc.stderr:
        print("        " + proc.stderr.strip().splitlines()[-1][:200])
    _replace_quietly(src, dst)
    return "mp4v-only"


def detect_video(
    video_path=DEFAULT_VIDEO,
    weights=DEFAULT_WEIGHTS,
    conf: float = 0.25,
    save_video: bool = True,
    show: bool = False,
    output_dir=DEFAULT_OUTPUT_DIR,
    verbose: bool = True,
    on_progress=None,
    max_frames: int | None = None,
) -> dict:
    """逐帧检测视频，保存事故帧 ROI 与元数据。

    返回元数据 dict，同时写入 output_dir/accident_frames.json。

    `on_progress(frame_index, total, n_accident_frames)` 每 25 帧回调一次。
    检测 959 帧在 CPU 上要 5 分多钟，实时模式必须让前端看到进度，
    否则页面上就是个五分钟不动的转圈。传 None 则完全不回调（保持原有行为）。

    `video_path` 可以是本地文件路径，**也可以是视频流地址**
    （`rtsp://` / `rtmp://` / `http(s)://` / `srt://` …）。摄像头接入走的就是这条路：
    实时流没有"总帧数"、也不会自己结束，所以：
      - 总帧数取不到时不做假估计，据实留 0（报告里会显示"?"）；
      - **必须**用 `max_frames` 给它一个分析上界，否则会一直读下去。
    """
    source = str(video_path)
    stream = is_stream_source(source)
    video_path = Path(source)
    weights = Path(weights)
    output_dir = Path(output_dir)
    frames_dir = output_dir / "accident_frames"
    video_out = output_dir / "detections.mp4"
    meta_path = output_dir / "accident_frames.json"

    # 流地址不是文件，is_file() 对它是恒假的 —— 早期版本正是被这一条挡在门外，
    # 于是"接入摄像头"这件事在代码层面根本没有入口。
    if not stream and not video_path.is_file():
        raise FileNotFoundError(f"视频不存在: {video_path}")
    if not weights.is_file():
        raise FileNotFoundError(f"YOLO 权重不存在: {weights}")
    if stream and max_frames is None and verbose:
        print(
            "[检测] 注意：输入是视频流且未指定 --max-frames，"
            "将一直读取直到流结束（实时流不会自己结束）"
        )

    frames_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        existing = _stale_rois(frames_dir)
        if existing:
            print(
                f"[检测] 注意：{frames_dir} 里已有 {existing} 张 ROI 图片，"
                "本次会覆盖同名文件但不删除多余的旧文件（如需干净目录请手动清理）"
            )

    model = YOLO(str(weights))
    accident_ids = _accident_class_ids(model)
    if not accident_ids:
        raise ValueError(f"权重里找不到 '{ACCIDENT_CLASS}' 类别，可用类别: {model.names}")
    colors = _make_colors(model, accident_ids)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise IOError(f"无法打开视频: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        # 实时流通常取不到总帧数（它压根没有"一共多少帧"这回事）。
        # 有 max_frames 就拿它当进度参照，没有就据实留 0 —— 进度显示成 "?"，
        # 而不是编一个看起来合理的数字。
        total = max_frames or 0

    # 标注视频的编码策略。**最终产物必须是 H.264**，否则前端是一片黑且不报错。
    # 三条路，按优先级：
    #   direct-h264 —— avc1 可用，直接写（本地 conda 版 opencv 走这条）
    #   via-ffmpeg  —— avc1 不可用但系统有 ffmpeg：先用 mp4v 写临时文件，
    #                  视频写完后转成 H.264。**pip 装的 opencv wheel 自带的
    #                  FFmpeg 不含 H.264 编码器**（部署到服务器后就是这样，
    #                  实测 `Could not find encoder for codec_id=27`），走这条。
    #   mp4v-only   —— 两者都没有：只能留 mp4v 并明确警告。
    #
    # ⚠ 这里曾经的行为是"拿不到 avc1 就退回 mp4v 并打一行警告"——那等于把
    #   一个浏览器播不了的文件当成交付物。警告没人看，黑屏一定会被发现。
    encode_mode = "none"
    video_src: Path | None = None  # via-ffmpeg 模式下真正被写入的临时文件
    writer = None
    if save_video:
        writer = cv2.VideoWriter(
            str(video_out), cv2.VideoWriter_fourcc(*"avc1"), fps, (width, height)
        )
        if writer.isOpened():
            encode_mode = "direct-h264"
        else:
            writer.release()  # 先释放半初始化的句柄，免得和下面这个抢同一个文件
            if _ffmpeg_available():
                encode_mode = "via-ffmpeg"
                video_src = output_dir / ".detections.mp4v.mp4"
            else:
                encode_mode = "mp4v-only"
            writer = cv2.VideoWriter(
                str(video_src or video_out),
                cv2.VideoWriter_fourcc(*"mp4v"),
                fps,
                (width, height),
            )
        if verbose and encode_mode != "direct-h264":
            print(f"[检测] H.264 编码器不可用，标注视频改走：{encode_mode}")

    records: list[dict] = []
    frame_index = 0
    n_accident_frames = 0

    try:
        while True:
            # 分析上界。实时流不会自己结束，没有这道闸就会一直读下去；
            # 文件也可以用它做"只看前 N 帧"的快速验证。
            if max_frames is not None and frame_index >= max_frames:
                break
            ok, frame = cap.read()
            if not ok:
                break
            frame_index += 1

            results = model.predict(frame, conf=conf, verbose=False)
            accident_boxes = []
            boxes_to_draw = []

            for result in results:
                for box in result.boxes:
                    cls = int(box.cls[0])
                    confidence = float(box.conf[0])
                    x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                    boxes_to_draw.append((cls, confidence, x1, y1, x2, y2))
                    if cls in accident_ids:
                        accident_boxes.append((confidence, x1, y1, x2, y2))

            # 一帧内多个事故框时，取置信度最高的那个作为代表 ROI
            if accident_boxes:
                confidence, x1, y1, x2, y2 = max(accident_boxes, key=lambda b: b[0])
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(width, x2), min(height, y2)
                if x2 > x1 and y2 > y1:
                    roi_name = f"accident_frame_{frame_index:04d}_roi.jpg"
                    cv2.imwrite(str(frames_dir / roi_name), frame[y1:y2, x1:x2])
                    records.append(
                        {
                            "frame_index": frame_index,
                            "time_sec": round((frame_index - 1) / fps, 3),
                            "confidence": round(confidence, 4),
                            "bbox": [x1, y1, x2, y2],
                            "num_accident_boxes": len(accident_boxes),
                            "roi": f"{frames_dir.name}/{roi_name}",  # 相对输出目录
                        }
                    )
                    n_accident_frames += 1

            for cls, confidence, x1, y1, x2, y2 in boxes_to_draw:
                color = colors.get(cls, (255, 255, 255))
                label = f"{model.names[cls]} {confidence:.2f}"
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
                text_y = y1 - 10 if y1 - 10 > 10 else y1 + 10
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(frame, (x1, text_y - th - 5), (x1 + tw, text_y + 5), color, -1)
                cv2.putText(
                    frame, label, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2
                )

            if writer is not None:
                writer.write(frame)
            if show:
                cv2.imshow("Video with Detections", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            if verbose and frame_index % 100 == 0:
                print(f"[检测] 进度 {frame_index}/{total or '?'} 帧，已存事故帧 {n_accident_frames} 张")

            # 25 帧约合 8 秒 CPU 时间，前端进度条以此粒度更新
            if on_progress is not None and frame_index % 25 == 0:
                on_progress(frame_index, total, n_accident_frames)
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        if show:
            cv2.destroyAllWindows()

    # 转码必须在 writer.release() 之后：mp4v 文件的索引( moov atom )是释放时才写进去的，
    # 提前转码会得到一个 0 时长或截断的文件。
    if encode_mode == "via-ffmpeg" and video_src is not None:
        encode_mode = _transcode_to_h264(video_src, video_out, verbose=verbose)

    meta = {
        "video": source,
        "weights": str(weights),
        # 产物自描述：事后能一眼看出这次分析的输入是"文件"还是"实时流"，
        # 以及流分析被哪道上界截断过 —— 否则「为什么只有 300 帧」会变成
        # 一个只能靠回忆回答的问题。
        "source_kind": "stream" if stream else "file",
        "max_frames": max_frames,
        "fps": round(float(fps), 3),
        "width": width,
        "height": height,
        # total_frames 是**容器声明的**总帧数（流上常常取不到，退回实际读到的帧数）；
        # frames_read 才是**这次真正读并检测了**多少帧。两者在 max_frames 生效时不等，
        # 不把这一点分开写清楚，"为什么只分析了 300 帧"就变成只能靠回忆回答的问题。
        "frames_read": frame_index,
        "total_frames": total or frame_index,
        "conf_threshold": conf,
        "accident_frames": len(records),
        "annotated_video": str(video_out) if save_video else None,
        # 产物自描述：事后能看出这个 mp4 是不是真的 H.264，
        # 不用再去猜"当时那个环境能不能编码"
        "video_encode_mode": encode_mode,
        "frames": records,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if verbose:
        print(
            f"[检测] 完成：{frame_index} 帧中检出 {len(records)} 张事故帧，"
            f"元数据 -> {meta_path}"
        )
    return meta


# --------------------------------------------------------------------------- #
# 事件聚类 / 抽帧去重
# --------------------------------------------------------------------------- #
def group_events(
    frames: list[dict],
    gap: int = 10,
    frames_per_event: int = 1,
    min_rep_gap: int = 5,
) -> list[dict]:
    """把连号的事故帧聚成「事故事件」，并为每个事件挑代表帧。

    gap            相邻两帧帧号差 <= gap 视为同一次事故
    frames_per_event 每个事件选几张代表帧送多模态（按置信度从高到低选）
    min_rep_gap    同一事件内两张代表帧的帧号至少要差这么多，避免选出重复画面
    """
    if not frames:
        return []

    ordered = sorted(frames, key=lambda f: f["frame_index"])

    groups: list[list[dict]] = []
    current: list[dict] = []
    for frame in ordered:
        if current and frame["frame_index"] - current[-1]["frame_index"] > gap:
            groups.append(current)
            current = []
        current.append(frame)
    if current:
        groups.append(current)

    events = []
    for event_id, group in enumerate(groups, 1):
        ranked = sorted(group, key=lambda f: (-f["confidence"], f["frame_index"]))

        picked: list[dict] = []
        for candidate in ranked:  # 第一轮：满足最小间隔
            if len(picked) >= frames_per_event:
                break
            if all(abs(candidate["frame_index"] - p["frame_index"]) >= min_rep_gap for p in picked):
                picked.append(candidate)
        for candidate in ranked:  # 第二轮：数量不够时放宽间隔限制补齐
            if len(picked) >= frames_per_event:
                break
            if candidate not in picked:
                picked.append(candidate)

        picked.sort(key=lambda f: f["frame_index"])
        events.append(
            {
                "event_id": event_id,
                "start_frame": group[0]["frame_index"],
                "end_frame": group[-1]["frame_index"],
                "num_frames": len(group),
                "start_sec": group[0]["time_sec"],
                "end_sec": group[-1]["time_sec"],
                "peak_confidence": round(max(f["confidence"] for f in group), 4),
                "representatives": picked,
            }
        )
    return events


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="交通事故视频检测（输出事故帧 ROI + 元数据）")
    parser.add_argument(
        "-v", "--video", default=str(DEFAULT_VIDEO),
        help="输入视频路径，或视频流地址（rtsp:// / rtmp:// / http(s):// / srt://）",
    )
    parser.add_argument("-w", "--weights", default=str(DEFAULT_WEIGHTS), help="YOLO 权重路径")
    parser.add_argument("-c", "--conf", type=float, default=0.25, help="置信度阈值")
    parser.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--no-video", action="store_true", help="不生成标注视频")
    parser.add_argument("--show", action="store_true", help="实时弹窗预览（需 GUI 环境）")
    parser.add_argument("--gap", type=int, default=10, help="事件聚类的帧间隔容差")
    parser.add_argument("--frames-per-event", type=int, default=1, help="每个事件抽几张代表帧")
    parser.add_argument(
        "--max-frames", type=int, default=None,
        help="最多分析多少帧（接入实时流时必填：流不会自己结束）",
    )
    args = parser.parse_args(argv)

    meta = detect_video(
        video_path=args.video,
        weights=args.weights,
        conf=args.conf,
        save_video=not args.no_video,
        show=args.show,
        output_dir=args.output_dir,
        max_frames=args.max_frames,
    )

    events = group_events(meta["frames"], gap=args.gap, frames_per_event=args.frames_per_event)
    events_path = Path(args.output_dir) / "accident_events.json"
    events_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[聚类] {meta['accident_frames']} 张事故帧 -> {len(events)} 个事故事件"
        f"（每个事件取 {args.frames_per_event} 张代表帧），详情 -> {events_path}"
    )
    for event in events:
        reps = ", ".join(str(r["frame_index"]) for r in event["representatives"])
        print(
            f"  事件 {event['event_id']:>2}: 帧 {event['start_frame']}-{event['end_frame']}"
            f" ({event['num_frames']} 帧) 峰值置信度 {event['peak_confidence']:.3f} 代表帧 [{reps}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
