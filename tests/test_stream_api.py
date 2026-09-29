"""后端「视频流接入」开关的回归测试。

跑法（**必须用后端自己的 venv**，它才有 fastapi/httpx）：

    server/.venv/bin/python tests/test_stream_api.py

不需要 GPU，也不会真去分析任何视频 —— `_run_job` 被替换成空函数，
这里考的是**闸门**，不是推理。

为什么要写这些用例：

这个接口默认在本机裸跑、**没有认证**。放开"接受请求里的 URL"就多了一条
SSRF 通道：任何人都能借这台服务器去连内网任意地址。所以流接入默认关闭，
必须显式开。这类开关有两种典型的失守方式，两种都不会报错：

1. **开关默认变成了开** —— 部署的人以为没开，实际谁都能拿它探内网。
2. **开关开着时，文件那条路的校验被顺手放过了** —— 于是"只允许分析
   source/raw_videos 下的文件"这条防线被一个无关的改动带走了。

第 2 条特别隐蔽：功能一切正常，只有安全边界悄悄消失了。所以下面的用例
**同时**覆盖两条路，且两条都在开关开启的状态下再测一遍。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

# ⚠ 都必须在 import app 之前设好：它们在 app 里是**模块级**读进变量的。
#   不隔离的话这个测试会读写真实的 output/ 目录。
_TMP = tempfile.mkdtemp(prefix="skyeyes-test-stream-")
os.environ["SKYEYES_OUTPUT"] = _TMP
os.environ["SKYEYES_PY"] = sys.executable  # 只要是个真实存在的解释器即可

import app as skyapp  # noqa: E402
from fastapi import HTTPException  # noqa: E402

# 别真的去起子进程跑 pipeline（那会需要一个装了 torch 的解释器 + 几分钟）
skyapp._run_job = lambda job: None  # type: ignore[assignment]


def _create(payload: dict):
    """调 create_job，把"抛异常"和"返回结果"统一成一种形态给断言用。"""
    skyapp.JOBS.clear()  # 否则上一次留下的 queued 任务会触发 409 串行保护
    try:
        return asyncio.run(skyapp.create_job(payload))
    except HTTPException as exc:
        return exc


# ───────────────── 默认状态：流必须被挡在门外 ─────────────────

def test_stream_rejected_when_flag_absent():
    os.environ.pop("SKYEYES_ALLOW_STREAM", None)
    res = _create({"video": "rtsp://192.168.1.64:554/Streaming/Channels/101"})
    assert isinstance(res, HTTPException), f"未开启开关却放行了流：{res}"
    assert res.status_code == 400
    # 报错必须说清**怎么开**，否则现场只知道"不行"，不知道下一步做什么
    assert "SKYEYES_ALLOW_STREAM" in res.detail, f"报错没告诉人怎么开：{res.detail}"


def test_stream_rejected_for_other_falsy_flag_values():
    """开关只认 1/true/yes；`0`、空串、随便一个字符串都不算开。"""
    for val in ("0", "", "false", "no", "off", "enable", " TRUE "):
        os.environ["SKYEYES_ALLOW_STREAM"] = val
        res = _create({"video": "rtsp://10.0.0.9/live"})
        if val.strip().lower() in ("1", "true", "yes"):
            assert not isinstance(res, HTTPException), f"{val!r} 应当算开启"
        else:
            assert isinstance(res, HTTPException), f"{val!r} 不该被当成开启"
    os.environ.pop("SKYEYES_ALLOW_STREAM", None)


def test_non_whitelisted_scheme_is_not_treated_as_stream():
    """白名单外的协议不是"流"，会走文件那条路并被拒——而不是被当成流放行。

    这条守的是"不要把白名单写成'看着像 URL 就收'"。
    """
    os.environ["SKYEYES_ALLOW_STREAM"] = "1"
    try:
        res = _create({"video": "ftp://evil.example.com/x.mp4"})
        assert isinstance(res, HTTPException), f"白名单外协议被放行了：{res}"
        assert "视频必须位于" in res.detail, f"走到了意料之外的分支：{res.detail}"
    finally:
        os.environ.pop("SKYEYES_ALLOW_STREAM", None)


# ───────────────── 开关开启后：流放行、但文件那条路不能被带松 ─────────────────

def test_stream_accepted_when_enabled_and_gets_max_frames():
    os.environ["SKYEYES_ALLOW_STREAM"] = "1"
    try:
        res = _create({"video": "rtsp://192.168.1.64:554/live"})
        assert not isinstance(res, HTTPException), f"开关开着却仍被拒：{res.detail}"
        args = res["pipeline_args"]
        # 实时流不会自己结束。不给上界 = 任务永不完成 = 串行队列被它堵死，
        # 而界面上只会显示"检测中"，一个字都不报错。
        assert "--max-frames" in args, f"流任务没带上 max-frames：{args}"
        assert args[args.index("--max-frames") + 1] == "300", f"默认上界不是 300：{args}"
        assert res["video_name"] != "", "video_name 不能是空串"
    finally:
        os.environ.pop("SKYEYES_ALLOW_STREAM", None)


def test_stream_respects_explicit_max_frames():
    os.environ["SKYEYES_ALLOW_STREAM"] = "1"
    try:
        res = _create({"video": "rtsp://10.0.0.1/live", "max_frames": 50})
        args = res["pipeline_args"]
        assert args[args.index("--max-frames") + 1] == "50"
    finally:
        os.environ.pop("SKYEYES_ALLOW_STREAM", None)


def test_bad_max_frames_is_rejected_not_silently_ignored():
    os.environ["SKYEYES_ALLOW_STREAM"] = "1"
    try:
        for bad in ("abc", 0, -5):
            res = _create({"video": "rtsp://10.0.0.1/live", "max_frames": bad})
            assert isinstance(res, HTTPException), f"非法 max_frames={bad!r} 被接受了"
            assert res.status_code == 400
    finally:
        os.environ.pop("SKYEYES_ALLOW_STREAM", None)


def test_file_outside_video_dir_still_rejected_with_stream_enabled():
    """**最重要的回归**：开关开着时，文件那条路的目录校验不能被带松。

    不然就是把"允许连摄像头"顺手变成了"允许读整台机器的任意文件"，
    而功能测试一切正常，谁也不会发现。
    """
    os.environ["SKYEYES_ALLOW_STREAM"] = "1"
    try:
        res = _create({"video": str(ROOT / "pipeline.py")})  # 存在，但不在 VIDEO_DIR 下
        assert isinstance(res, HTTPException), f"目录校验被放过了：{res}"
        assert res.status_code == 400
        assert "视频必须位于" in res.detail, f"报错信息变了：{res.detail}"
    finally:
        os.environ.pop("SKYEYES_ALLOW_STREAM", None)


def test_file_inside_video_dir_is_still_accepted():
    """反向确认：合法文件仍然能建任务（证明上面那些 400 不是"什么都拒"）。"""
    skyapp.VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    sample = skyapp.VIDEO_DIR / "_stream_api_test_sample.mp4"
    sample.write_bytes(b"not a real video, but a real file")
    try:
        res = _create({"video": str(sample)})
        assert not isinstance(res, HTTPException), f"合法文件被拒了：{res.detail}"
        assert "--max-frames" not in res["pipeline_args"], "文件任务不该被塞上 max-frames"
    finally:
        sample.unlink(missing_ok=True)
        skyapp.JOBS.clear()


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
