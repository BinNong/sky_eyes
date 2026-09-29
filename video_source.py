"""视频源判定：区分「本地文件」与「网络视频流地址」。

**这个模块必须只依赖标准库。**
它被两边同时 import：
  - `detector.py`（跑在装了 ultralytics/torch 的推理环境里）
  - `server/app.py`（跑在 `server/.venv` 里，**刻意没有 torch**）
所以这里一旦引入任何第三方依赖，后端启动就会直接失败。
这和 `alerting.py` / `video_search.py` 当初的取舍是同一条规矩。

为什么单独成文件、而不是在各处各写一遍协议列表：
「哪些协议算视频流」是一条**会漂移的判断**——检测核心放行了 rtsp，
后端却仍按文件路径校验，结果就是接口层把能跑的能力挡在门外，
而报错信息只会说"视频必须位于 source/raw_videos 下"，完全指不到真正原因。
共用一份实现，就没有这种可能。
"""

from __future__ import annotations

from pathlib import Path

# 判定为「视频流」的协议前缀。
# 只做协议白名单，不做连通性探测 —— 探测要联网、会挂住、也无法离线单测。
_STREAM_SCHEMES = (
    "rtsp://",
    "rtsps://",
    "rtmp://",
    "http://",
    "https://",
    "udp://",
    "rtp://",
    "srt://",
)

# 开给本地路径的 URI 形式。`file://` 在语义上是文件不是流，
# 但 cv2 用同一套 URL 解析处理它，所以这里一并放行；名字上仍归为「流式来源」。
_URI_SCHEMES = _STREAM_SCHEMES + ("file://", "filesrc://", "v4l2://")


def is_stream_source(source) -> bool:
    """入参是「视频流地址」（或 URI 形式）还是「本地文件路径」。"""
    text = str(source).strip().lower()
    # 先排除 Windows 盘符（`C:\\...`）这类"带冒号但不是协议"的路径
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        return False
    return text.startswith(_URI_SCHEMES)


def is_network_stream(source) -> bool:
    """是不是真正的网络流（不含 `file://` 这类本地 URI）。

    后端用它决定是否需要 SSRF 相关的开关；`file://` 不该被当成"网络访问"放行。
    """
    return str(source).strip().lower().startswith(_STREAM_SCHEMES)


def source_display_name(source) -> str:
    """给视频源取一个人能读的名字，用于报告、日志与告警卡片。

    **不能直接 `Path(source).name`** —— 实测（`tests/test_video_source.py` 钉着）：

        Path("rtsp://").name        -> "rtsp:"    协议名被当成文件名
        Path("http://x/?a=b").name  -> "?a=b"     查询串被当成文件名

    这类名字看起来像"文件名填错了"，实际是拿文件路径的解析器去解析 URL。

    更要注意的是**只取主机名、绝不取路径段**：`rtsp://cam/stream?token=SECRET`
    这种地址的凭证就在路径与查询串里，而 video_name 会进报告标题与告警卡片——
    取错了等于把密钥写进产物。这是它与 `alerting.py` 里那条
    「凭证不落进任何日志或产物」同源的要求。
    """
    text = str(source).strip()
    if not is_stream_source(text):
        return Path(text).name
    rest = text.split("://", 1)[-1]
    host = rest.split("/", 1)[0].split("?", 1)[0].strip()
    return host or "实时视频流"
