"""标注视频编码路径的回归测试。

跑法（需要 cv2 / ultralytics，用 pipeline 那套环境）：

    # 服务器
    PYTHONPATH=<项目>/vendor /remote/Janus/.venv/bin/python \
        tests/test_detector_video.py
    # 本地
    /path/to/visual_search/bin/python tests/test_detector_video.py

守的是一条很容易悄悄失守的约定：**交付出去的标注视频必须是浏览器能播的**。

失守方式特别隐蔽——mp4v 能写成功、文件非空、ffprobe 也能报出编码格式，
只是浏览器里一片黑、控制台还不报错。本项目踩过一次，所以这里有判别性用例。

背景：能不能直接写 H.264 取决于 OpenCV 自带的 FFmpeg 有没有编码器。
  · 本地 conda 版 opencv：有 → 直接写 avc1
  · pip 装的 opencv wheel（服务器）：**没有** → 必须靠系统 ffmpeg 转码
所以这条路径在两台机器上会走不同分支，两边都得测。
"""

from __future__ import annotations

import http.server
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

import detector  # noqa: E402


def _ffprobe_codec(path: Path) -> str | None:
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return out or None


def _write_mp4v(path: Path, n: int = 10) -> None:
    w, h, fps = 320, 180, 25
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert wr.isOpened(), "连 mp4v 都写不了，环境不完整"
    import numpy as np

    for i in range(n):
        wr.write(np.full((h, w, 3), (i * 25) % 255, dtype="uint8"))
    wr.release()


# ───────────────────────── 基础 ─────────────────────────

def test_ffmpeg_available_returns_bool():
    assert isinstance(detector._ffmpeg_available(), bool)
    assert detector._ffmpeg_available() == (shutil.which("ffmpeg") is not None)


# ───────────────────── 失败路径：不许丢文件、不许抛 ─────────────────────


def test_transcode_failure_keeps_source_and_does_not_raise():
    """ffmpeg 解不开的垃圾文件：必须返回 mp4v-only，且**原文件被挪到目标位置**。

    这条守的是"检测跑了 5 分钟，别因为一个编码器把视频弄丢"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "broken.mp4"
        dst = Path(tmp) / "out.mp4"
        payload = b"this is definitely not a video" * 10
        src.write_bytes(payload)

        mode = detector._transcode_to_h264(src, dst, verbose=False)

        assert mode == "mp4v-only", f"垃圾输入却报成功：{mode}"
        assert dst.is_file(), "失败时没把源文件挪过去，视频丢了"
        assert dst.read_bytes() == payload, "挪过去的内容不是原文件"


def test_transcode_failure_replaces_partial_dst():
    """目标位置已有残留（ffmpeg 可能写出半个文件）时必须被覆盖，不能留下坏的。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "broken.mp4"
        dst = Path(tmp) / "out.mp4"
        src.write_bytes(b"garbage-garbage")
        dst.write_bytes(b"stale-partial-output")

        mode = detector._transcode_to_h264(src, dst, verbose=False)

        assert mode == "mp4v-only"
        assert dst.read_bytes() == b"garbage-garbage", "残留的半个文件没被覆盖"


# ───────────────────── 成功路径：产物必须真是 H.264 ─────────────────────


def test_transcode_produces_h264_and_removes_temp():
    if not detector._ffmpeg_available():
        print("     （跳过：本机没有 ffmpeg）")
        return
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "raw.mp4"
        dst = Path(tmp) / "final.mp4"
        _write_mp4v(src)
        assert _ffprobe_codec(src) == "mpeg4", "前置条件不成立：源文件不是 mp4v"

        mode = detector._transcode_to_h264(src, dst, verbose=False)

        assert mode == "h264-ffmpeg", f"转码没成功：{mode}"
        assert dst.is_file() and dst.stat().st_size > 0
        codec = _ffprobe_codec(dst)
        if codec is None:
            print("     （没有 ffprobe，跳过编码名断言）")
        else:
            assert codec == "h264", f"产物编码是 {codec}，不是 h264——浏览器会黑屏"
        assert not src.exists(), "转码成功后应清掉 mp4v 临时文件"


def test_avc1_probe_reflects_this_environment():
    """把"这台机器能否直接写 H.264"记录下来，不做事先假设。

    不断言一定是 True 或 False——两台机器本来就不同；断言的是
    **探测结果与 ffmpeg 兜底能力至少有一个成立**，即最终产物能是 H.264。
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mp4"
        w = cv2.VideoWriter(str(probe), cv2.VideoWriter_fourcc(*"avc1"), 25, (64, 64))
        direct = w.isOpened()
        w.release()
        can_direct = direct
        can_via_ffmpeg = detector._ffmpeg_available()
        assert can_direct or can_via_ffmpeg, (
            "这台机器既不能直接写 avc1，也没有 ffmpeg —— "
            "标注视频将只能是浏览器播不了的 mp4v"
        )
        print(
            f"     （本机：直接写 avc1={'可以' if can_direct else '不行'}，"
            f"ffmpeg 兜底={'有' if can_via_ffmpeg else '没有'}）"
        )


# ═══════════════ 视频流输入（摄像头接入） ═══════════════
#
# 这一段是「接入真实摄像头」这条能力的证明。用不了真摄像机，就把一段本地 mp4
# 用 HTTP 暴露出去当流喂进去 —— 走的是**与 rtsp 完全相同的代码路径**
# （cv2.VideoCapture 打开 URL → 逐帧读 → 命中就存 ROI），只是协议不同。
#
# ⚠ 必须临时清掉 http_proxy 一类环境变量：FFmpeg 会跟随它们，
#   于是本应直连 127.0.0.1 的请求被丢给代理，报错却是"无法打开视频"，
#   跟"地址写错了"完全区分不开。本机环境变量里确实设了 HTTP_PROXY。
_PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 别把每个请求都打到 stderr 上
        pass


def _serve_dir(directory) -> tuple:
    """起一个只服务该目录的本地 HTTP 服务，返回 (base_url, 关闭函数)。"""
    import functools
    import socketserver
    import threading

    handler = functools.partial(_QuietHandler, directory=str(directory))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def shutdown():
        httpd.shutdown()
        httpd.server_close()

    return f"http://127.0.0.1:{port}", shutdown


def _without_proxy(fn, *a, **kw):
    """在"没有代理变量"的环境下执行 fn（跑完恢复原样）。"""
    import os

    saved = {k: os.environ.pop(k, None) for k in _PROXY_KEYS}
    try:
        return fn(*a, **kw)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_stream_url_does_not_hit_the_file_check():
    """流地址不能被 `is_file()` 拦下。

    改动之前这条是失效的：`detect_video` 无条件 `Path(src).is_file()`，
    于是 rtsp 地址会得到 `FileNotFoundError: 视频不存在: rtsp:/...`——
    注意 `Path` 会把 `//` 压成 `/`，报出来的路径跟用户输入长得不一样，
    看起来像"路径写错了"，而真正的原因是"这条能力没有入口"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _without_proxy(
                detector.detect_video,
                video_path="rtsp://127.0.0.1:1/nonexistent",
                save_video=False,
                output_dir=Path(tmp) / "out",
                verbose=False,
                max_frames=1,
            )
        except FileNotFoundError as exc:
            raise AssertionError(f"流地址被文件检查拦下了：{exc}") from None
        except Exception:
            # 连不上是正常的（没有那台摄像机），**能走到"打开失败"就说明闸门已放行**
            return
        raise AssertionError("一个不存在的流地址居然跑成功了？")


def test_detect_video_reads_http_stream_and_honours_max_frames():
    """端到端：HTTP 流能被逐帧检测，且 max_frames 真的生效。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _write_mp4v(src, n=20)
        base, shutdown = _serve_dir(tmp)
        try:
            meta = _without_proxy(
                detector.detect_video,
                video_path=f"{base}/clip.mp4",
                save_video=False,
                output_dir=Path(tmp) / "out",
                verbose=False,
                max_frames=5,
            )
        finally:
            shutdown()

        assert meta["source_kind"] == "stream", f"没把这次分析标成流：{meta['source_kind']}"
        assert meta["max_frames"] == 5
        # 实时流不会自己结束，max_frames 是唯一的上界——必须真的截断
        assert meta["frames_read"] == 5, f"max_frames 没生效，读了 {meta['frames_read']} 帧"
        assert meta["video"].startswith(base), "元数据里的源地址被改写过了"
        # 流读出来的画面尺寸得是真的，否则后面 ROI 裁剪会全错
        assert meta["width"] == 320 and meta["height"] == 180, (
            f"流解析出的画面尺寸不对：{meta['width']}x{meta['height']}"
        )


def test_local_file_stmt_still_reports_source_kind_file():
    """回归：文件输入的行为与标记不变（这条守的是"改动是增量的"）。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _write_mp4v(src, n=8)
        meta = detector.detect_video(
            video_path=src, save_video=False,
            output_dir=Path(tmp) / "out", verbose=False, max_frames=3,
        )
        assert meta["source_kind"] == "file"
        assert meta["max_frames"] == 3
        assert meta["frames_read"] == 3
        # total_frames 是容器声明的总帧数，跟 frames_read 是两回事，不该被 max_frames 改写
        assert meta["total_frames"] == 8, (
            f"total_frames 被 max_frames 带偏了：{meta['total_frames']}"
        )


def test_missing_file_still_raises_file_not_found():
    """普通文件不存在时，报错必须仍然是那句可读的「视频不存在」。"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            detector.detect_video(
                video_path=Path(tmp) / "nope.mp4", save_video=False,
                output_dir=Path(tmp) / "out", verbose=False,
            )
        except FileNotFoundError as exc:
            assert "视频不存在" in str(exc), f"报错信息变了：{exc}"
            return
        raise AssertionError("不存在的文件居然没报错")


def test_max_frames_is_wired_from_cli():
    """`--max-frames` 必须同时出现在 argparse 定义**和**调用处。

    这是本项目踩过的那类"半成品"：只改了 `args.max_frames` 的用法、
    却没在 `add_argument` 里定义它 —— 静态检查看不出来，跑起来才
    `AttributeError`/`SystemExit`，而且往往崩在检测跑了几分钟之后。
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            detector._main([
                "-v", str(Path(tmp) / "nope.mp4"),
                "-o", str(Path(tmp) / "out"),
                "--no-video",
                "--max-frames", "7",
            ])
        except FileNotFoundError:
            return  # argparse 认了这个参数，卡在"文件不存在"——正是我们要的
        except SystemExit as exc:
            raise AssertionError(
                f"argparse 没认出 --max-frames（exit {exc.code}）——"
                "参数定义与调用处不一致"
            ) from None
        raise AssertionError("不存在的文件却没有报错")


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}")
            print("     " + traceback.format_exc().replace("\n", "\n     ").strip())
    print()
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
