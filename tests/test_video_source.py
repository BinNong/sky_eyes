"""视频源判定（文件 / 视频流）的回归测试。

跑法（**两套环境都能跑** —— 这个模块刻意只依赖标准库）：

    /path/to/visual_search/bin/python tests/test_video_source.py
    server/.venv/bin/python tests/test_video_source.py

为什么要专门给它写测试：

`is_stream_source()` 是**整条「接入摄像头」能力的第一个闸门**，
而它被三处共用（detector / pipeline / server）。它一旦判错，后果分两种，
**两种都不抛异常**：

1. **把普通文件路径判成了流** → 跳过 `is_file()` 检查，错误被推迟到
   `cv2.VideoCapture` 才暴露，报错信息从"视频不存在：xxx"退化成
   "无法打开视频"，排查方向完全被带偏。
2. **把流地址判成了文件** → 直接 `FileNotFoundError: 视频不存在: rtsp:/...`，
   而报出来的路径（`Path` 会把 `//` 压成 `/`）跟用户输入长得不一样，
   看起来像"路径写错了"。

第 2 种就是本次改动之前的状态：整条能力在代码层面根本没有入口。

Windows 盘符那一条是**同类陷阱里最容易漏的**：`C:\\video\\a.mp4` 的
第二个字符恰好是冒号，只按"含 `://`"或"含冒号"来判断就会把它误判成流，
而这个错误在本机（macOS）永远测不出来。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from video_source import (  # noqa: E402
    is_network_stream,
    is_stream_source,
    source_display_name,
)


# ───────────────────── 应当判为「流」的输入 ─────────────────────

def test_common_stream_schemes_are_streams():
    for src in (
        "rtsp://192.168.1.64:554/Streaming/Channels/101",
        "rtsps://cam.example.com/live",
        "rtmp://live.example.com/app/key",
        "http://127.0.0.1:8080/video.mp4",
        "https://cdn.example.com/a/b.mp4",
        "srt://10.0.0.5:9000",
        "udp://239.0.0.1:1234",
    ):
        assert is_stream_source(src), f"没认出这是流：{src}"


def test_scheme_match_is_case_insensitive_and_ignores_surrounding_space():
    assert is_stream_source("RTSP://192.168.1.64/live")
    assert is_stream_source("  rtsp://192.168.1.64/live  ")


def test_file_uri_counts_as_stream_source_but_not_as_network():
    """`file://` 走流式解析，但**不是网络访问**。

    这个区分是给后端安全开关用的：把 `file://` 也算成"网络流"，
    就等于用一个 SSRF 开关去管本地文件，开关的语义会变得说不清。
    """
    assert is_stream_source("file:///tmp/a.mp4"), "file:// 没被当成 URI 形式"
    assert not is_network_stream("file:///tmp/a.mp4"), "file:// 不该算网络流"


# ───────────────────── 绝不应当判为「流」的输入 ─────────────────────

def test_plain_paths_are_not_streams():
    for src in (
        "source/raw_videos/burstling_street.mp4",
        "/path/to/sky_eyes/source/raw_videos/a.mp4",
        "a.mp4",
        "./a.mp4",
        "../a.mp4",
    ):
        assert not is_stream_source(src), f"把普通路径误判成流：{src}"


def test_windows_drive_letter_is_not_a_stream():
    """`C:\\...` 的第二个字符是冒号，**极易被误判成协议**。

    本机是 macOS，这个错误在开发机上永远暴露不出来，只能靠用例守住。
    """
    for src in (r"C:\videos\a.mp4", r"d:/videos/a.mp4", r"Z:\live\x.mkv"):
        assert not is_stream_source(src), f"Windows 盘符被误判成流：{src}"
        assert not is_network_stream(src)


def test_empty_and_whitespace_are_not_streams():
    for src in ("", "   ", None):
        assert not is_stream_source(src), f"空输入被当成流：{src!r}"


def test_unknown_scheme_is_not_silently_accepted():
    """协议白名单之外的一律不放行，而不是"看着像 URL 就收"。

    放开一个没验证过的协议，等于让 cv2 去解析任意东西——
    失败时的报错会落在 OpenCV 内部，跟"地址填错了"完全区分不开。
    """
    for src in ("ftp://host/a.mp4", "gopher://host/x", "javascript:alert(1)"):
        assert not is_stream_source(src), f"白名单外的协议被放行了：{src}"


# ───────────────────── 显示名：绝不为空 ─────────────────────

def test_display_name_for_file_is_basename():
    assert source_display_name("source/raw_videos/burstling_street.mp4") == "burstling_street.mp4"
    assert source_display_name("/a/b/c.mp4") == "c.mp4"


def test_display_name_for_stream_is_never_empty():
    cases = {
        "rtsp://192.168.1.64:554/Streaming/Channels/101": "192.168.1.64:554",
        "rtsp://192.168.1.64/": "192.168.1.64",
        "rtsp://192.168.1.64": "192.168.1.64",
        "http://127.0.0.1:8080/a/b.mp4?token=xyz": "127.0.0.1:8080",
        "rtsp://": "实时视频流",
        "rtsp:///live": "实时视频流",
    }
    for src, expected in cases.items():
        got = source_display_name(src)
        assert got != "", f"流地址取到了空名字：{src}"
        assert got == expected, f"{src} -> 期望 {expected!r}，实际 {got!r}"


def test_naive_path_name_is_wrong_for_streams():
    """把「朴素写法 `Path(src).name`」钉住，防止有人"简化"回去。

    这两条是**跑出来才发现的真实缺陷**（最初我以为它会返回空字符串，实测不是）：

      Path("rtsp://").name        -> 'rtsp:'    协议名被当成了文件名
      Path("http://x/?a=b").name  -> '?a=b'     查询串被当成了文件名

    这些名字会进报告标题和告警卡片，看起来像"文件名填错了"，
    实际是解析方式用错了对象。
    """
    assert Path("rtsp://").name == "rtsp:", "前提变了：请重新核对 Path 对流地址的处理"
    assert Path("http://x/?a=b").name == "?a=b", "前提变了：请重新核对 Path 对流地址的处理"

    # 我们的实现必须与朴素写法给出不同（且有意义）的结果
    assert source_display_name("rtsp://") == "实时视频流"
    assert source_display_name("http://x/?a=b") == "x"

    # 并且**绝不把 URL 的路径段当名字**——那里可能带 token/密钥，
    # 而 video_name 会进报告与告警卡片，等于把凭证写进了产物。
    assert "live" not in source_display_name("rtsp:///live")
    assert "token" not in source_display_name("rtsp://cam/stream?token=SECRET")


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
