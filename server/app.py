"""
sky_eyes 实时推理后端。

职责边界（这是整个 P5 最重要的设计决定）：
    本进程**不 import torch / ultralytics / cv2**，只做编排——
    用子进程调用 visual_search 环境里的 pipeline.py，逐行读它的
    `@@PROGRESS {...}` 输出转成 SSE。因此它跑在一个只装了 fastapi/uvicorn/httpx
    的独立 venv 里（见 requirements.txt）。

为什么不让后端直接 import pipeline：
    torch 那一套有 2GB+，且和 conda 环境强绑定。Web 层只要一 import 就把
    整个深度学习栈拖进进程，启动慢、依赖脆弱、还没法单独升级。子进程隔离后，
    Web 层崩了不影响推理，推理崩了也不影响其他接口。

任务隔离（又一个关键决定）：
    每次分析写进 `output/runs/<job_id>/`，**绝不写 `output/`**。
    `output/` 是静态 demo 的数据源（web/public/data/demo.json 由它导出），
    实时任务一旦覆盖它，路演兜底数据就没了——那是最不该发生的连锁故障。

启动：
    ./scripts/serve_api.sh          # 推荐，会自动建 venv
    # 或手动
    server/.venv/bin/uvicorn app:app --port 8787 --app-dir server
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 人工复核反馈模块与本文件同级。显式把所在目录塞进 sys.path，
# 这样 `uvicorn app:app --app-dir server` 和 `uvicorn server.app:app` 都能 import 到，
# 不必依赖启动时的工作目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import feedback as feedback_store  # noqa: E402
import labeling  # noqa: E402

# 视频源判定（文件 vs 视频流）。**用 append 而不是 insert(0)**：根目录下同样有
# `feedback/`、`labeling/` 这些**同名目录**，插到最前面会把上一行刚导进来的
# server 侧模块遮蔽掉。
# 这个模块是纯 stdlib 的——`server/.venv` 刻意不装 torch/ultralytics，
# **绝不能在这里 import detector**（它顶部就 import ultralytics），那会让后端起不来。
sys.path.append(str(Path(__file__).resolve().parent.parent))
from video_source import is_network_stream, source_display_name  # noqa: E402

# ───────────────────────────── 配置 ─────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.environ.get("SKYEYES_OUTPUT", PROJECT_ROOT / "output"))
RUNS_DIR = OUTPUT_DIR / "runs"
PIPELINE = PROJECT_ROOT / "pipeline.py"
EXPORT_SCRIPT = PROJECT_ROOT / "scripts" / "export_demo_data.py"
VIDEO_DIR = PROJECT_ROOT / "source" / "raw_videos"

# 「文字找帧」的索引目录。默认落在前端静态目录里 —— 缩略图由 vite 直接当静态文件发，
# 后端只读 index.json + vectors.f32 做排序，不必自己当图床。
SEARCH_INDEX_DIR = Path(
    os.environ.get(
        "SKYEYES_SEARCH_INDEX", PROJECT_ROOT / "web" / "public" / "data" / "search"
    )
)

# ⚠ 必须用 append，**不能 insert(0, ...)**。
#   项目根下有 `feedback/` 和 `labeling/` 两个同名**目录**（前者存复核记录、
#   后者存标注答案）。Python 3 会把无 __init__.py 的目录当成命名空间包，
#   于是 insert(0) 之后 `import feedback` 会命中那个目录而不是 server/feedback.py，
#   表现为 `AttributeError: module 'feedback' has no attribute 'STATIC_RUN'`——
#   报错信息完全指不到真正的原因。放队尾则 server/ 优先，两边都不冲突。
sys.path.append(str(PROJECT_ROOT))
import alerting  # noqa: E402
import video_search  # noqa: E402

# 必须指向装了 torch/ultralytics 的环境，不能是本服务的 venv。
#
# 这个默认值只是**开发机上跑得通的那个 conda 环境**，不是通用约定：
# 部署到服务器时一定由启动脚本覆盖（deploy/skyeyes.sh 设成 Janus 的 venv +
# vendor 覆盖层，见 deploy/setup_env.sh）。没有它时 create_job 会报 503 并
# 说明该设哪个变量——**故意不在 import 期就崩**，否则连 /api/health 都拿不到，
# 现场无从判断是"没配解释器"还是"服务没起来"。
PYTHON = os.environ.get(
    "SKYEYES_PY", "/path/to/visual_search/bin/python"
)
JANUS_BASE = os.environ.get("JANUS_API_BASE", "http://127.0.0.1:8000")
DEFAULT_VIDEO = VIDEO_DIR / "burstling_street.mp4"

# 传给 pipeline.py 的参数。**必须与生成静态 demo 的那一轮完全一致**，
# 否则两种模式下的数字对不上，演示时"有 1 起高等级事故"这句话会当场变成假话。
#
# ⚠ `--frames-per-event` 尤其关键：pipeline 的默认值是 1，而静态数据是按 2 生成的。
#   用默认值重跑一遍，事件 6（帧 593-610）只会送检首帧 593 → 判「中」，
#   而决定事件等级的那一帧 600（判「高」）根本没被送进模型，
#   结果事件级分布从「高 1」变成「高 0」——正是本项目最不可接受的漏报高等级。
#   实测复现过一次（送检 9 帧 / 高 0），见 DEMO_PLAN.md 的 P5 记录。
PIPELINE_ARGS = [
    "--frames-per-event",
    os.environ.get("SKYEYES_FRAMES_PER_EVENT", "2"),
]

# 三段耗时占比（实测）：检测 959 帧 CPU 约 5.3 分钟，聚类瞬间完成，多模态 18 帧约 35 秒。
# 用它把三个阶段的百分比合成一根总进度条，否则进度会长时间停在 0%。
STAGE_WEIGHT = {"detect": 0.90, "cluster": 0.02, "understand": 0.08}
STAGE_ORDER = ["detect", "cluster", "understand"]

# 每个 run 目录里的两份元数据。
# 任务状态本来只在内存里，后端一重启就全丢——靠扫目录"猜"出来的状态撑不起
# 「载入上一次结果」这个演示功能（一次分析要 6 分钟，没人愿意为讲解重跑）。
MANIFEST = "job.json"  # 结构化元数据，原子写（tmp + os.replace）
RUN_LOG = "job.log"  # 完整 stdout，纯文本追加，便于事后排查
MANIFEST_VERSION = 1

TERMINAL_STATUSES = ("done", "error", "cancelled", "interrupted")

# 进度落盘节流。检测阶段每 25 帧推一次进度（约 8 秒），每次都写盘没必要。
PERSIST_MIN_INTERVAL = 3.0

# 产物保留策略。**只在总量超过阈值时才清理**，且永远保留最新的若干个、
# 永远不碰正在跑的任务。自动删除用户数据是危险动作，阈值必须留足余量：
# 5GB ≈ 230 次分析，正常使用根本碰不到。设为 0 可彻底关掉。
MAX_RUNS_GB = float(os.environ.get("SKYEYES_MAX_RUNS_GB", "5"))
MIN_KEEP_RUNS = int(os.environ.get("SKYEYES_MIN_KEEP_RUNS", "3"))

RUNS_DIR.mkdir(parents=True, exist_ok=True)

# ───────────────────────────── 任务模型 ─────────────────────────────


@dataclass
class Job:
    id: str
    video: str
    run_dir: str
    status: str = "queued"  # queued|running|done|error|cancelled|interrupted
    stage: str = "queued"
    stage_pct: float = 0.0
    error: str | None = None
    detail: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    log: list[str] = field(default_factory=list)
    # 产物体积与上次落盘时刻。后者只用于节流，不进 manifest。
    size_mb: float | None = None
    last_persist: float = 0.0
    #: 本次任务实际传给 pipeline.py 的参数。
    #: **不能直接用全局 PIPELINE_ARGS**——那份是所有任务的公共基线，
    #: 而企微告警开关是按任务选的（每跑一次就往群里发一遍，不该由上一次的选择决定）。
    pipeline_args: list[str] = field(default_factory=lambda: list(PIPELINE_ARGS))


JOBS: dict[str, Job] = {}
PROCS: dict[str, subprocess.Popen] = {}
LOCK = threading.Lock()

LOG_LIMIT = 300


def _overall_pct(job: Job) -> float:
    """把「当前阶段 + 该阶段内百分比」换算成一根总进度条。"""
    if job.status == "done":
        return 100.0
    base = 0.0
    for s in STAGE_ORDER:
        if s == job.stage:
            return round(min(99.9, base + job.stage_pct / 100 * STAGE_WEIGHT[s] * 100), 1)
        base += STAGE_WEIGHT[s] * 100
    return 0.0


def _elapsed(job: Job) -> float:
    """已用秒数。

    ⚠ 必须夹到 >= 0：`finished_at - started_at` 在两种情况下会算出负数——
    ① 机器时钟被校过；② 恢复出来的 started_at 来自别处（清单被改过、
    或与文件 mtime 混用）。负数会让界面显示成 `-50:22` 这种无意义的值。
    """
    if not job.started_at:
        return 0.0
    return max(0.0, round((job.finished_at or time.time()) - job.started_at, 1))


def _run_alerts(job: Job) -> dict | None:
    """读这次运行的企微告警台账。

    `None` 与"推了 0 条"是两件事，界面必须分开显示：
    None = 这次压根没开告警（或者任务还没跑到那一步）；有值但 sent=0 = 开了但没达标事件。
    前者说明"没推是对的"，后者可能需要人去查配置——混起来就分不清了。
    """
    path = Path(job.run_dir) / "alerts.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _alert_view() -> dict:
    """告警通道配置的对外视图。**脱敏在 alerting.channel_view 里做**，
    这里不再自己拼一遍——两处各写一份，迟早会有一处漏掉某个凭证字段。

    **每次都重读配置**，不做缓存：自检的典型场景就是"改完配置立刻刷新页面看生效没有"，
    缓存 30 秒正好会让这件事变得很困惑。读一个几百字节的 JSON 代价可以忽略。
    """
    return alerting.channel_view(alerting.load_config())


def _job_view(job: Job, with_log: bool = True) -> dict:
    return {
        "job_id": job.id,
        "video": job.video,
        "status": job.status,
        "stage": job.stage,
        "stage_pct": round(job.stage_pct, 1),
        "pct": _overall_pct(job),
        "detail": job.detail,
        "error": job.error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed": _elapsed(job),
        "video_name": source_display_name(job.video),
        "size_mb": job.size_mb,
        "has_report": (Path(job.run_dir) / "web" / "data" / "demo.json").is_file(),
        "pipeline_args": list(job.pipeline_args),
        # 告警开关是按任务选的，界面要能看出"这一次到底推没推、推了几条"
        "alert_requested": "--alert" in job.pipeline_args,
        "alerts": _run_alerts(job),
        "log": job.log[-40:] if with_log else [],
    }


def _append_log(job: Job, line: str) -> None:
    with LOCK:
        job.log.append(line)
        if len(job.log) > LOG_LIMIT:
            del job.log[: len(job.log) - LOG_LIMIT]


# ─────────────────────────── 落盘 / 恢复 / 清理 ───────────────────────────


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return round(total / 1048576, 1)


_size_cache: tuple[float, float] = (0.0, 0.0)


def _runs_size_mb(ttl: float = 5.0) -> float:
    """带缓存的体积统计。/api/health 是前端探活的高频入口，不能每次都全盘 stat。"""
    global _size_cache
    ts, mb = _size_cache
    if time.time() - ts < ttl:
        return mb
    mb = _dir_size_mb(RUNS_DIR)
    _size_cache = (time.time(), mb)
    return mb


def _safe_run_dir(job_id: str) -> Path | None:
    """把 URL 里的 job_id 安全地解析成 RUNS_DIR 下的目录。

    job_id 直接来自 URL，而删除接口会 rmtree。不校验的话
    `/api/jobs/..%2f..%2foutput` 这类请求就能删掉整个 output/——
    而 output/ 是静态 demo 的数据源，正是本项目最不该被破坏的东西。

    只接受"单层目录名"，并确认解析后确实落在 RUNS_DIR 内。
    """
    if not job_id or job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
        return None
    if Path(job_id).name != job_id:
        return None
    root = RUNS_DIR.resolve()
    target = (root / job_id).resolve()
    if target == root or root not in target.parents:
        return None
    if not target.is_dir():
        return None
    return target


def _persist(job: Job) -> None:
    """把任务元数据写进 run 目录。

    ⚠ **绝不能在持有 LOCK 时调用**（threading.Lock 不可重入，会自锁）。
    """
    run_dir = Path(job.run_dir)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with LOCK:
            snap = {
                "version": MANIFEST_VERSION,
                "job_id": job.id,
        "video": job.video,
        "pipeline_args": list(job.pipeline_args),
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "status": job.status,
                "stage": job.stage,
                "stage_pct": round(job.stage_pct, 2),
                "error": job.error,
                "detail": dict(job.detail),
                "size_mb": job.size_mb,
                "log_tail": job.log[-40:],
            }
        tmp = run_dir / f"{MANIFEST}.tmp"
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, run_dir / MANIFEST)
    except OSError as exc:
        # 落盘失败不该打断推理本身——内存里的状态仍然是对的
        print(f"[警告] 写任务清单失败 {job.id}: {exc}")


def _append_run_log(run_dir: Path, line: str) -> None:
    """完整 stdout 追加到 job.log。

    刻意逐行打开、不持有句柄：进程被强杀时不会留下半截缓冲。
    """
    try:
        with open(run_dir / RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _finalize(job: Job, status: str, error: str | None = None) -> None:
    """收尾：定状态、算体积、落盘。终态一定走这里，别再手写一遍。"""
    with LOCK:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        job.stage = "done" if status == "done" else status
    job.size_mb = _dir_size_mb(Path(job.run_dir))
    _persist(job)


def _prune_runs(trigger: str) -> list[str]:
    """总量超阈值时淘汰最旧的产物。

    三条硬约束：不碰正在跑的任务、永远保留最新的 MIN_KEEP_RUNS 个、
    阈值内一个都不删。默认 5GB ≈ 230 次分析，正常演示完全碰不到。
    """
    if MAX_RUNS_GB <= 0 or not RUNS_DIR.exists():
        return []
    entries: list[tuple[Path, float]] = []
    for d in sorted(RUNS_DIR.iterdir()):
        if d.is_dir() and _safe_run_dir(d.name):
            entries.append((d, _dir_size_mb(d)))
    if len(entries) <= MIN_KEEP_RUNS:
        return []

    total = sum(s for _, s in entries)
    limit_mb = MAX_RUNS_GB * 1024
    if total <= limit_mb:
        return []

    with LOCK:
        busy = {j.id for j in JOBS.values() if j.status in ("queued", "running")}
    protected = busy | {d.name for d, _ in entries[-MIN_KEEP_RUNS:]}

    removed: list[str] = []
    for d, size in entries:  # 由旧到新
        if total <= limit_mb:
            break
        if d.name in protected:
            continue
        try:
            shutil.rmtree(d)
        except OSError as exc:
            print(f"[警告] 清理 {d.name} 失败：{exc}")
            continue
        with LOCK:
            JOBS.pop(d.name, None)
        total -= size
        removed.append(d.name)

    if removed:
        global _size_cache
        _size_cache = (0.0, 0.0)
        print(
            f"[清理] {trigger}：删除 {len(removed)} 个旧产物"
            f"（{'、'.join(removed)}），现为 {total:.0f} MB"
        )
    return removed


def _apply_progress(job: Job, payload: dict) -> None:
    stage = payload.get("stage")
    with LOCK:
        if stage in STAGE_WEIGHT:
            job.stage = stage
            job.stage_pct = float(payload.get("pct") or 0.0)
        # 把计数类字段（事故帧数、送检进度等）透传给前端展示
        for k, v in payload.items():
            if k not in ("stage", "status", "pct"):
                job.detail[k] = v
        if payload.get("status") == "done" and stage in STAGE_ORDER:
            job.detail[f"{stage}_done"] = True


def _restore_runs() -> tuple[int, int]:
    """启动时从磁盘重建任务列表，返回（已完成数, 中断数）。

    本进程写的 `job.json` 就是权威来源——不再靠文件 mtime 猜起止时间
    （那既不准，也让"列表顺序正确"变成了排序巧合：恢复出来的 created_at
    全是同一个时刻，顺序只靠 dict 插入序 + 稳定排序撑着）。

    对没有 job.json 的旧目录（本次改动之前产生的）退回老办法：
    看有没有 accident_report.json，用 mtime 近似时间。

    顺带把**中断的任务**带进列表。目录建了、状态还停在 running 的，
    说明上次是后端被杀或崩溃收场的；以前这种目录会被静默忽略，永远躺在
    output/runs/ 里却不出现在界面上，也没有任何入口能删掉它。
    """
    if not RUNS_DIR.exists():
        return (0, 0)

    done = interrupted = 0
    for d in sorted(RUNS_DIR.iterdir()):
        if not d.is_dir() or d.name in JOBS or not _safe_run_dir(d.name):
            continue

        report_path = d / "accident_report.json"
        report_exists = report_path.is_file()

        manifest: dict = {}
        mpath = d / MANIFEST
        if mpath.is_file():
            try:
                manifest = json.loads(mpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}  # 清单坏了就退回老办法，不影响产物本身

        stamps = [p.stat().st_mtime for p in d.rglob("*") if p.is_file()]
        fallback_created = min(stamps) if stamps else d.stat().st_mtime
        fallback_finished = max(stamps) if stamps else fallback_created

        status = str(
            manifest.get("status") or ("done" if report_exists else "interrupted")
        )
        if status in ("queued", "running"):
            status = "interrupted"  # 进程早没了，状态却还停在"跑着"
        if status == "done" and not report_exists:
            status = "interrupted"  # 清单说完成了，产物却不在——以磁盘为准
        error = manifest.get("error")
        if status == "interrupted":
            error = error or "分析未完成（后端重启或进程被终止），无产物"

        video = manifest.get("video") or ""
        if not video and report_exists:
            try:
                video = json.loads(report_path.read_text(encoding="utf-8")).get("video", "")
            except (OSError, json.JSONDecodeError):
                pass

        job = Job(id=d.name, video=video, run_dir=str(d))
        job.status = status
        job.stage = str(manifest.get("stage") or ("done" if status == "done" else status))
        job.stage_pct = float(manifest.get("stage_pct") or (100.0 if status == "done" else 0.0))
        job.error = error
        job.detail = dict(manifest.get("detail") or {})
        # 老清单（本次改动之前写的）没有这个键，退回全局基线即可
        job.pipeline_args = list(manifest.get("pipeline_args") or PIPELINE_ARGS)
        job.created_at = float(manifest.get("created_at") or fallback_created)
        job.started_at = float(manifest.get("started_at") or fallback_created)
        job.finished_at = float(manifest.get("finished_at") or fallback_finished)
        # 清单里的时间与文件 mtime 是两套来源，可能对不上（跨机器、时钟被校过、
        # 清单是手改的）。起止倒挂时以"结束"为准，否则界面会显示负数耗时。
        if job.started_at > job.finished_at:
            job.started_at = job.finished_at

        log_file = d / RUN_LOG
        tail: list[str] = []
        if log_file.is_file():
            try:
                tail = log_file.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-LOG_LIMIT:]
            except OSError:
                tail = []
        job.log = tail or list(manifest.get("log_tail") or [])
        if not job.log:
            job.log = ["[后端] 本次启动时从磁盘恢复的历史任务"]

        job.size_mb = manifest.get("size_mb") or _dir_size_mb(d)

        JOBS[d.name] = job
        if status == "done":
            done += 1
        elif status == "interrupted":
            interrupted += 1

    return (done, interrupted)


_restored_done, _restored_interrupted = _restore_runs()
if _restored_done or _restored_interrupted:
    print(
        f"[启动] 从 output/runs 恢复 {_restored_done} 个已完成任务、"
        f"{_restored_interrupted} 个中断任务"
    )
_prune_runs("启动时")




def _run_job(job: Job) -> None:
    run_dir = Path(job.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    with LOCK:
        job.status = "running"
        job.started_at = time.time()
        job.stage = "detect"

    cmd = [
        PYTHON,
        str(PIPELINE),
        "-v",
        job.video,
        "-o",
        str(run_dir),
        "--progress-json",
        *job.pipeline_args,
    ]
    _append_log(job, "$ " + " ".join(cmd))
    _append_run_log(run_dir, "$ " + " ".join(cmd))
    # 先把"开始跑了"记下来：万一片刻后进程被强杀，下次启动也能看出这次没跑完
    _persist(job)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        _finalize(job, "error", f"无法启动推理进程：{exc}")
        return

    PROCS[job.id] = proc

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith("@@PROGRESS "):
            try:
                payload = json.loads(line[len("@@PROGRESS ") :])
            except json.JSONDecodeError:
                continue  # 进度行坏了不影响主流程
            _apply_progress(job, payload)
            # 节流落盘：检测阶段约每 8 秒推一次进度，每次都写没必要
            if time.time() - job.last_persist >= PERSIST_MIN_INTERVAL:
                job.last_persist = time.time()
                _persist(job)
        elif line.strip():
            _append_log(job, line)
            _append_run_log(run_dir, line)

    rc = proc.wait()
    PROCS.pop(job.id, None)

    with LOCK:
        cancelled = job.status == "cancelled"
    if cancelled:
        _finalize(job, "cancelled", job.error or "已手动取消")
        return
    if rc != 0:
        _finalize(job, "error", f"推理进程退出码 {rc}，详见日志")
        return

    with LOCK:
        job.stage = "export"
        job.stage_pct = 0.0
    _persist(job)

    # 把 pipeline 产物转成前端可消费的同构数据（demo.json + 抽帧 + 视频）
    _append_log(job, "[后端] 导出前端数据 …")
    exp = subprocess.run(
        [
            PYTHON,
            str(EXPORT_SCRIPT),
            "--output-dir",
            str(run_dir),
            "--dest",
            str(run_dir / "web"),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    for ln in (exp.stdout or "").splitlines():
        if ln.strip():
            _append_log(job, ln)
            _append_run_log(run_dir, ln)
    if exp.returncode != 0:
        _finalize(job, "error", f"导出前端数据失败：{(exp.stderr or '')[-300:]}")
        return

    elapsed = round(time.time() - (job.started_at or time.time()), 1)
    done_line = f"[后端] 完成，耗时 {elapsed}s"
    _append_log(job, done_line)
    _append_run_log(run_dir, done_line)

    with LOCK:
        job.stage = "done"
        job.stage_pct = 100.0
    # 体积要在导出之后才算，否则少算几十 MB
    _finalize(job, "done")
    _prune_runs("分析完成后")


# ───────────────────────────── HTTP ─────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # 关闭时必须把推理子进程一起带走。
    # 不做这件事的话，uvicorn 一停，pipeline.py 会变成孤儿继续吃 CPU，
    # 下次起服务再跑一个任务就是两个进程抢一张显卡——在 8GB 显存的共享机器上
    # 直接表现为多模态服务 OOM。
    for jid, proc in list(PROCS.items()):
        if proc.poll() is None:
            print(f"[退出] 终止仍在运行的推理进程 job={jid} pid={proc.pid}")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    PROCS.clear()
    # 被我们终止的任务要在清单里留痕。否则磁盘上会留下"状态永远 running"的记录，
    # 下次启动虽然能靠 _restore_runs 兜成 interrupted，但原因就说不清了。
    for job in list(JOBS.values()):
        if job.status in ("queued", "running"):
            _finalize(job, "interrupted", "后端关闭，任务被终止")


app = FastAPI(title="sky_eyes live inference API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    """探活。前端用它决定能否切到实时模式，必须**快**——Janus 探测限时 1 秒。"""
    janus: dict = {"ok": False, "base": JANUS_BASE}
    t0 = time.perf_counter()
    try:
        # ⚠ trust_env=False 是必须的。httpx 默认读 HTTP_PROXY/HTTPS_PROXY，
        #   而本机（WorkBuddy 沙箱）恰好设了 HTTP_PROXY=http://127.0.0.1:60410。
        #   于是这个本该直连 127.0.0.1:8000 的探活被丢给代理，代理回了个非 JSON
        #   的响应，前端看到的是 `JSONDecodeError: Expecting value: line 1 column 1`
        #   ——而不是那句真正有用的"未连接，先去执行 janus_up.sh"。
        #   环回地址本来就不该走代理，这里显式关掉。
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            resp = await client.get(f"{JANUS_BASE}/health")
            body = resp.json()
        janus.update(
            ok=resp.status_code == 200 and body.get("status") == "ok",
            device=body.get("device"),
            cuda=body.get("cuda"),
            model=Path(body.get("model_path", "")).name,
        )
    except Exception as exc:  # 连不上是常态（隧道没开），不该报 500
        janus["error"] = f"{type(exc).__name__}: {exc}"[:200]
    janus["latency_ms"] = round((time.perf_counter() - t0) * 1000)

    py_ok = Path(PYTHON).is_file()
    return {
        "ok": True,
        "janus": janus,
        "pipeline": {
            "ready": py_ok and PIPELINE.is_file(),
            "python": PYTHON,
            "python_exists": py_ok,
            "script_exists": PIPELINE.is_file(),
        },
        "default_video": str(DEFAULT_VIDEO),
        "pipeline_args": PIPELINE_ARGS,
        # 告警通道状态。放在这里是刻意的：现场演示前先看这一栏，
        # 比跑完一轮 6 分钟才发现没配 webhook 要好得多。
        "alerts": _alert_view(),
        "runs": len(JOBS),
        "runs_mb": _runs_size_mb(),
        "runs_keep": {"max_gb": MAX_RUNS_GB, "min_keep": MIN_KEEP_RUNS},
        # 实测：959 帧 CPU 检测约 330s + 模型加载 25s + 多模态 18 帧约 40s
        # + 导出约 25s ≈ 7 分钟。机器有负载时会明显更慢，所以取略微保守的值。
        "estimate_sec": 400,
    }


@app.get("/api/videos")
async def videos() -> dict:
    """可选的输入视频。只列原视频目录，不接受任意路径。"""
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(VIDEO_DIR.glob("*")):
        if p.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv"}:
            items.append({"path": str(p), "name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1)})
    return {"videos": items, "default": str(DEFAULT_VIDEO)}


@app.post("/api/alerts/test")
async def test_alert() -> dict:
    """往企微群里发一条通道自检消息。

    存在的理由和 `pipeline.py --alert-test` 一样：现场演示前必须先确认通道是通的。
    放在按钮上比记一条命令行更可靠——讲解的人未必记得住命令。

    这个接口能让服务以系统名义往群里发消息，所以两点要清楚：
    ① 本服务只监听本机（演示用，所有接口都没有鉴权，与本机其他接口同等对待）；
    ② 只有运维真的配了 webhook 它才发得出去，没配时是纯 no-op（返回 configured=false）。
    """
    cfg = alerting.load_config()
    # 发消息是阻塞 IO，扔给线程池，别把事件循环卡住（否则进度 SSE 会一起顿住）
    outcome = await asyncio.to_thread(alerting.send_test, cfg)
    # 视图统一走 channel_view——脱敏只在一个地方做，两处各写一份迟早会漏掉某个凭证字段
    return {**alerting.channel_view(cfg), **outcome.to_dict()}


# ───────────────────── 视频检索（文字找帧） ───────────────────── #

# 索引里的向量是 192×512 个 float32（约 0.4 MB），不必每个请求都读盘；
# 但**重建索引后要立刻生效**（演示前会重跑建索引），所以按 (mtime, size) 失效，
# 而不是启动时读一次就锁死。
_index_cache: dict = {"key": None, "index": None}
_index_lock = threading.Lock()


def _load_search_index():
    """返回 (index, 失败原因)。失败原因永远是可读的一句话，界面直接显示。"""
    meta_path = SEARCH_INDEX_DIR / "index.json"
    vec_path = SEARCH_INDEX_DIR / "vectors.f32"
    if not meta_path.is_file() or not vec_path.is_file():
        return None, (
            f"还没建索引（缺 {SEARCH_INDEX_DIR.name}/index.json）。"
            "先跑 scripts/build_search_index.py"
        )
    st = vec_path.stat()
    key = (str(vec_path), st.st_mtime_ns, st.st_size)
    with _index_lock:
        if _index_cache["key"] == key and _index_cache["index"] is not None:
            return _index_cache["index"], None
    try:
        idx = video_search.load_index(SEARCH_INDEX_DIR)
    except Exception as exc:  # noqa: BLE001 - 索引坏了要如实说，不能装作"没结果"
        return None, f"索引读不出来：{exc}"
    with _index_lock:
        _index_cache["key"] = key
        _index_cache["index"] = idx
    return idx, None


class SearchIn(BaseModel):
    q: str
    top_k: int = 12


@app.get("/api/search/status")
async def search_status() -> dict:
    """检索功能的自检：索引在不在、编码服务通不通。

    刻意与 `/api/health` 分开：这个探测要往 :8001 发一次 HTTP（超时 6 秒），
    塞进 health 会把整页的状态灯拖慢。演示前点一下这个接口就够了。
    """
    idx, err = _load_search_index()
    encoder = await asyncio.to_thread(video_search.service_status)
    out: dict = {
        "index_available": idx is not None,
        "index_dir": str(SEARCH_INDEX_DIR),
        "encoder": encoder,
        # 缩略图由 vite 当静态文件发；前端用 thumb_base + hit.thumb 拼路径
        "thumb_base": "/data/search/",
    }
    if idx is not None:
        out["index"] = idx.describe()
    else:
        out["reason"] = err
    return out


@app.post("/api/search")
async def search(body: SearchIn) -> dict:
    """用一句自然语言在本段视频里找帧。"""
    q = (body.q or "").strip()
    if not q:
        raise HTTPException(400, "查询不能为空")
    if len(q) > 120:
        raise HTTPException(400, "查询太长了（上限 120 字）")

    idx, err = _load_search_index()
    if idx is None:
        raise HTTPException(503, err)

    top_k = max(1, min(int(body.top_k), 48))
    started = time.time()
    try:
        # 走网络的活儿扔线程池，别卡住事件循环
        hits = await asyncio.to_thread(idx.search, q, top_k)
    except video_search.EncoderUnavailable as exc:
        # ⚠ 这里必须是 503 而不是"返回空结果"。
        #   「编码服务不可用」和「这个词没搜到」在界面上都表现为"没有结果"，
        #   但处置方向完全相反：一个去查隧道和服务，一个去改查询词。
        #   混成一个响应，排查的人会白白在错误的方向上找很久。
        raise HTTPException(503, f"编码服务不可用：{exc}") from None

    return {
        "query": q,
        "hits": [h.to_dict() for h in hits],
        # 查询词若命中预置缓存，是本地算的（服务挂了也能用）——如实标出来
        "source": "cached_chip" if idx.cached_query(q) else "encoder",
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "top_k": top_k,
        "thumb_base": "/data/search/",
        "video": idx.meta.get("video"),
        "index": {
            "frame_count": idx.count,
            "stride": idx.stride,
            "stride_sec": idx.meta.get("stride_sec"),
            "fps": idx.fps,
        },
        "score_note": (
            "分数是余弦相似度，只用于排序。不同查询之间的绝对值不可比，"
            "也不存在「高于多少才算找到」的阈值——请按名次看，并核对缩略图。"
        ),
    }


@app.get("/api/report")
async def report(job: str | None = None) -> dict:
    """回吐一份前端同构数据。

    不传 job：返回静态 demo（等同 web/public/data/demo.json）。
    传 job：返回该次实时分析的产物。
    """
    if job:
        path = RUNS_DIR / job / "web" / "data" / "demo.json"
    else:
        path = PROJECT_ROOT / "web" / "public" / "data" / "demo.json"
    if not path.is_file():
        raise HTTPException(404, f"找不到数据：{path.name}（job={job}）")
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────── 人工复核反馈（模型可信度） ───────────────────
#
# 与上面这些接口的区别：那些回答"系统跑得通不通"，这里回答"**模型答对了没有**"。
# 前者是工程可用性，后者才需要人的判断，所以数据来自人工复核而非模型自评。
# 详细取舍见 server/feedback.py 的模块注释。


def _dataset(run_id: str) -> dict:
    """读某个数据集的事件清单。

    返回 `{"path", "total", "severity": {event_id: 等级}}`。

    事件数用于算复核覆盖率——只复核了 2 起就说"精确率 100%"是有误导性的，
    界面上必须同时给出样本量。

    等级映射用于**服务端自己确认模型当时的判定**，而不是照收前端传来的值：
    它是混淆矩阵的行标签，行一旦错位，整个矩阵的解读就反了，
    而且矩阵看上去照样"有数据"、照样自成一体——典型的静默算错。
    """
    if run_id == feedback_store.STATIC_RUN:
        path = PROJECT_ROOT / "web" / "public" / "data" / "demo.json"
    else:
        run_dir = _safe_run_dir(run_id)  # 复用防目录穿越的校验
        path = (run_dir / "web" / "data" / "demo.json") if run_dir else None

    if not path or not path.is_file():
        return {"path": None, "total": 0, "severity": {}}
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events") or []
    except (OSError, json.JSONDecodeError):
        return {"path": path, "total": 0, "severity": {}}

    severity: dict[int, str | None] = {}
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get("event_id"), int):
            continue
        s = e.get("event_severity")
        severity[e["event_id"]] = s if s in feedback_store.SEVERITIES else None
    return {"path": path, "total": len(events), "severity": severity}


def _feedback_view(run_id: str) -> dict:
    ds = _dataset(run_id)
    items = feedback_store.by_run(run_id)
    return {
        "run_id": run_id,
        "available": ds["total"] > 0,
        "verdicts": items,
        "metrics": feedback_store.compute_metrics(items, ds["total"] or None),
    }


class VerdictIn(BaseModel):
    event_id: int
    verdict: str
    run_id: str = feedback_store.STATIC_RUN
    predicted_severity: str | None = None
    true_severity: str | None = None
    note: str | None = None


@app.get("/api/feedback")
async def get_feedback(run: str = feedback_store.STATIC_RUN) -> dict:
    """某个数据集上的复核结论，以及由它算出的指标。"""
    return _feedback_view(run)


@app.post("/api/feedback")
async def post_feedback(body: VerdictIn) -> dict:
    """提交一条人工判定。同一 (run_id, event_id) 是覆盖，不是累加。"""
    ds = _dataset(body.run_id)

    # 数据集存在时，事件必须在里面。否则就是伪造的 event_id，
    # 而它会变成混淆矩阵里一行不存在的记录。
    if ds["total"] and body.event_id not in ds["severity"]:
        raise HTTPException(400, f"数据集 {body.run_id} 中没有事件 {body.event_id}")

    # 以服务端数据里的等级为准，不采用前端传来的 `predicted_severity`。
    # 前端传错（或有人手搓请求）会让矩阵的行标签错位，而矩阵看上去依然自洽。
    predicted = ds["severity"].get(body.event_id, body.predicted_severity)

    try:
        feedback_store.put_verdict(
            run_id=body.run_id,
            event_id=body.event_id,
            verdict=body.verdict,
            predicted_severity=predicted,
            true_severity=body.true_severity,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _feedback_view(body.run_id)


@app.delete("/api/feedback")
async def delete_feedback(run: str | None = None, event_id: int | None = None) -> dict:
    """撤销一条（同时给 run 和 event_id），或清空某个数据集（只给 run）。"""
    if run is not None and event_id is not None:
        removed = 1 if feedback_store.clear_verdict(run, event_id) else 0
    else:
        removed = feedback_store.clear_all(run)
    return {"removed": removed, **_feedback_view(run or feedback_store.STATIC_RUN)}


# ─────────────────── 检测召回率的抽样标注 ───────────────────
#
# 与上面的「人工复核」互补：
#   复核 回答"模型报出来的事件里有多少是真的"（精确率）
#   标注 回答"模型没报的帧里有多少其实有事故"（漏检 → 召回率）
# 后者需要人真的看过那些没被检出的帧，没有捷径。


class LabelIn(BaseModel):
    truth: str


@app.get("/api/labeling")
async def get_labeling() -> dict:
    """抽样清单 + 已标注情况 + 由它外推的召回率指标。"""
    return labeling.view()


@app.post("/api/labeling/{item_id}")
async def post_labeling(item_id: str, body: LabelIn) -> dict:
    """记录一条标注。同一 id 覆盖。"""
    try:
        entry = labeling.put_label(item_id, body.truth)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    view = labeling.view()
    # 清单是静态的（80 条由脚本生成后不变），标注时不必回传——
    # 每点一下就拖 12KB 的题目没意义。
    view.pop("set", None)
    return {"label": entry, **view}


@app.delete("/api/labeling/{item_id}")
async def delete_labeling(item_id: str) -> dict:
    """撤销一条标注。"""
    labeling.clear_labels(item_id)
    view = labeling.view()
    view.pop("set", None)
    return view


@app.post("/api/jobs")
async def create_job(payload: dict | None = None) -> dict:
    payload = payload or {}
    raw_source = str(payload.get("video") or DEFAULT_VIDEO)

    # ── 视频源分两条路：本地文件 / 摄像头视频流 ──
    #
    # 文件那条路拦的是"任意文件读取"；流这条路拦的是 **SSRF** —— 本服务默认在
    # 本机裸跑且没有认证，若无条件接受请求里的 URL，任何人都能借它去探内网任意地址。
    # 所以流接入**默认关闭**，必须显式开启，并且只放行协议白名单内的地址。
    if is_network_stream(raw_source):
        if os.environ.get("SKYEYES_ALLOW_STREAM", "").strip().lower() not in ("1", "true", "yes"):
            raise HTTPException(
                400,
                "本服务未开启视频流接入。要分析摄像头实时流，请用 "
                "SKYEYES_ALLOW_STREAM=1 启动后端。"
                "（开启后本服务会主动连接请求中给出的流地址，请只在可信内网使用）",
            )
        video = raw_source
        is_stream = True
    else:
        resolved = Path(raw_source).resolve()
        # 只允许分析视频目录下的文件：这个接口在本机裸跑，不该成为任意文件读取入口
        try:
            resolved.relative_to(VIDEO_DIR.resolve())
        except ValueError:
            raise HTTPException(400, f"视频必须位于 {VIDEO_DIR} 下") from None
        if not resolved.is_file():
            raise HTTPException(400, f"视频不存在：{resolved.name}")
        video = str(resolved)
        is_stream = False

    if not Path(PYTHON).is_file():
        # 报出路径**和怎么修**。只报路径的话，在服务器上看到一串
        # /Users/... 会不知道从哪下手（那是开发机的默认值，部署必须覆盖它）。
        raise HTTPException(
            503,
            f"找不到推理环境：{PYTHON}。"
            "用环境变量 SKYEYES_PY 指向装了 torch/ultralytics 的解释器"
            "（服务器上用 deploy/skyeyes.sh 启动，它已经设好了）",
        )

    with LOCK:
        running = [j for j in JOBS.values() if j.status in ("queued", "running")]
    if running:
        # 串行执行：共享服务器只有 8GB 显存，并发跑两次多模态必然 OOM
        raise HTTPException(409, f"已有分析在进行中（{running[0].id}），请等它结束")

    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    job = Job(id=job_id, video=video, run_dir=str(RUNS_DIR / job_id))

    # 实时流不会自己结束，**必须**给分析一个上界。不给的话这个任务永远不会完成，
    # 而在串行队列下它会把后面所有任务一起堵死 —— 表现是界面永远停在"检测中"，
    # 且没有任何报错。所以流任务强制带上 max_frames（默认 300 帧 ≈ 实时 12 秒）。
    if is_stream:
        # ⚠ 这里**不能**写成 `payload.get("max_frames") or 300`：
        #   客户端传 0 时 `0 or 300` 得到 300，一个明确的值被默认值悄悄替换掉了，
        #   请求方以为"我只让它看 0 帧"，实际跑的是 300 帧。只有**没传**（None）
        #   才该落到默认值，传了非法值就老实报 400。
        raw_max = payload.get("max_frames")
        if raw_max is None:
            raw_max = 300
        try:
            max_frames = int(raw_max)
        except (TypeError, ValueError):
            raise HTTPException(400, f"max_frames 必须是整数，收到 {raw_max!r}") from None
        if max_frames <= 0:
            raise HTTPException(400, f"max_frames 必须大于 0，收到 {max_frames}")
        job.pipeline_args = [*job.pipeline_args, "--max-frames", str(max_frames)]

    # 告警开关是按任务选的。**这里提前校验**而不是交给子进程：等 6 分钟跑完
    # 才在日志尾部说一句"没配 webhook"，现场是没法补救的。
    if payload.get("alert"):
        if not alerting.load_config().enabled:
            raise HTTPException(
                400,
                "未配置企业微信 webhook，无法开启告警推送。请设置环境变量 "
                "SKYEYES_WECOM_WEBHOOK，或在项目根写 alert.config.json 后重试。",
            )
        job.pipeline_args = [*job.pipeline_args, "--alert"]

    with LOCK:
        JOBS[job_id] = job
    # 建目录 + 写清单都要在起线程之前：这样即使后端在任务起步阶段就被杀，
    # 磁盘上也有据可查（否则这个任务对界面而言等于从未存在过）
    Path(job.run_dir).mkdir(parents=True, exist_ok=True)
    _persist(job)

    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return _job_view(job)


@app.get("/api/jobs")
async def list_jobs() -> dict:
    with LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
        return {"jobs": [_job_view(j, with_log=False) for j in items[:20]]}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"未知任务 {job_id}")
    return _job_view(job)


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict:
    """中止任务。注意这是"停止"，不是"删除产物"。"""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"未知任务 {job_id}")
    proc = PROCS.get(job_id)
    if proc and proc.poll() is None:
        proc.terminate()
        with LOCK:
            job.status = "cancelled"
            job.error = "已手动取消"
        # 先落盘记下"取消"这个意图：_run_job 的读循环退出后会走 _finalize 再写一次
        _persist(job)
    return _job_view(job)


@app.delete("/api/jobs/{job_id}/artifacts")
async def delete_job_artifacts(job_id: str) -> dict:
    """删除某次分析的全部产物（整个 run 目录）。

    与上面的"取消"是两件事：取消只是停进程，产物还留着可以载入。
    这个接口是真删，所以路径必须严格校验（见 _safe_run_dir）——
    本服务在本机裸跑，一个目录穿越就能删掉 output/ 里静态 demo 的数据源。
    """
    job = JOBS.get(job_id)
    if job and job.status in ("queued", "running"):
        raise HTTPException(409, "任务正在运行，请先取消再删除")

    # ⚠ 「已取消」不等于「已经收完尾」。`DELETE /api/jobs/{id}` 只负责 terminate，
    #   子进程真正退出、以及 `_run_job` 读循环结束后的 `_finalize`（还会写一次
    #   job.json）都在这之后。界面上状态已经是 cancelled、删除按钮立刻可用，
    #   所以这个窗口是**用户点得到**的：此时 rmtree 与写盘撞在一起会抛 OSError，
    #   用户拿到一个 500，除了一条 traceback 什么线索都没有。
    #   实测复现过一次。这里明确拒绝，让用户"稍后再点"。
    proc = PROCS.get(job_id)
    if proc is not None and proc.poll() is None:
        raise HTTPException(409, "任务进程正在退出，请稍后再删除")

    target = _safe_run_dir(job_id)
    if target is None:
        if job is None:
            raise HTTPException(404, f"未知任务 {job_id}")
        raise HTTPException(400, f"路径不合法或不存在：{job_id}")

    freed = _dir_size_mb(target)
    # 再兜一层：进程刚被判死但收尾可能还差最后一次落盘，重试几次基本都能过。
    for attempt in range(3):
        try:
            shutil.rmtree(target)
            break
        except FileNotFoundError:
            break  # 已经被删掉了，等价于成功
        except OSError as exc:
            if attempt == 2:
                raise HTTPException(500, f"删除失败：{exc}") from None
            await asyncio.sleep(0.3)

    with LOCK:
        JOBS.pop(job_id, None)
    global _size_cache
    _size_cache = (0.0, 0.0)
    return {"deleted": job_id, "freed_mb": freed, "runs": len(JOBS)}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """SSE 进度推送。

    刻意用**轮询快照**而不是消息队列：任务在子线程里跑，
    往 asyncio 队列投递消息要跨线程调度，容易出竞态；而进度本来就是
    低频事件（检测每 25 帧一次 = 约 8 秒），0.5 秒轮询完全够用且简单得多。
    """
    if job_id not in JOBS:
        raise HTTPException(404, f"未知任务 {job_id}")

    async def gen():
        last = ""
        while True:
            job = JOBS.get(job_id)
            if job is None:
                yield "event: gone\ndata: {}\n\n"
                return
            snapshot = json.dumps(_job_view(job), ensure_ascii=False)
            if snapshot != last:
                last = snapshot
                yield f"data: {snapshot}\n\n"
            if job.status in ("done", "error", "cancelled"):
                yield "event: end\ndata: {}\n\n"
                return
            # 心跳，防止中间的代理掐掉空闲连接
            yield ": keep-alive\n\n"
            await _sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# 实时产物（抽帧图 / 标注视频）直接静态托管。
# 前端把 assetBase 指到 /api/runs/<job_id>/web/ 即可复用同一套取图逻辑。
app.mount("/api/runs", StaticFiles(directory=RUNS_DIR), name="runs")


# ─────────────── 可选：由同一个端口托管前端构建产物 ───────────────
#
# 设了 SKYEYES_STATIC_DIR（指向 web/dist）时，打开后端端口就是整个应用：
# 前端与 /api **同源**，不需要 vite 代理、也不存在跨域。
#
# **不设这个变量则行为与之前完全一致**——本地开发照旧是
# `npm run dev`(:5180) + vite 代理到 :8787。这个开关是给"部署到服务器、
# 只想暴露一个入口"的场景准备的：那种情况下让前端再独占一个端口，
# 等于白多一条链路、多一个会挂的东西，也少一个要记的地址。
#
# 必须放在所有 /api 路由**之后**注册：Starlette 按注册顺序匹配，
# 这条 `/{full_path:path}` 是兜底，注册早了会把 /api 全吃掉。
STATIC_DIR = os.environ.get("SKYEYES_STATIC_DIR")
if STATIC_DIR:
    _static_root = Path(STATIC_DIR).resolve()
    if not (_static_root / "index.html").is_file():
        print(
            f"[警告] SKYEYES_STATIC_DIR={_static_root} 下没有 index.html，"
            "跳过静态托管（API 不受影响）"
        )
    else:

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _serve_frontend(full_path: str) -> FileResponse:
            index = _static_root / "index.html"
            # `/api/*` 是 API 的命名空间，没匹配上就是 404，**绝不能被 SPA 兜底接管**。
            # 接管了的后果是：路径写错时前端拿到 200 + 一段 HTML，然后拿去
            # JSON.parse 报一个"语法错误"，完全指不到真正原因（少打一个字母）。
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(404, f"no such API: /{full_path}")
            if full_path:
                candidate = (_static_root / full_path).resolve()
                # 防路径穿越：解析后必须仍在静态根内（`..` 会被 resolve 掉）
                if candidate.is_file() and candidate.is_relative_to(_static_root):
                    return FileResponse(candidate)
                # 「长得像静态资源」却不存在 → 老老实实 404，**不要回 index.html**。
                # 回了的话，前端会拿一段 HTML 当成 JS/图片去解析，
                # 报错信息完全指不到真正原因（这个项目最忌讳的那类失效）。
                if Path(full_path).suffix:
                    raise HTTPException(404, f"not found: {full_path}")
            # 前端是 history 路由（/events/6 这类），刷新要能落到 index.html
            return FileResponse(index)

        print(f"[静态] 前端由本端口托管：{_static_root}")


def _cleanup_hint() -> None:
    total = sum(p.stat().st_size for p in RUNS_DIR.rglob("*") if p.is_file()) if RUNS_DIR.exists() else 0
    if total > 2 * 1024**3:
        print(f"[提示] output/runs 已占 {total / 1024**3:.1f} GB，可手动清理旧任务")


if __name__ == "__main__":
    import uvicorn

    _cleanup_hint()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8787")))
