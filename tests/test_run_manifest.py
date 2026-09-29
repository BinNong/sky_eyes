"""实时任务清单（持久化）与产物清理的回归测试。

跑法（**必须用后端自己的 venv**，它才有 fastapi/httpx）：

    server/.venv/bin/python tests/test_run_manifest.py

不需要 GPU、不需要真跑一次 6 分钟的分析。

为什么专门给这块写测试：

改之前在 `_restore_runs()` 里，恢复出来的任务 `created_at` 全是 `time.time()`
默认值（彼此只差几毫秒），列表顺序之所以"看起来对"，靠的是
**dict 插入序 + 稳定排序 + 目录名恰好是时间序**三者叠加——不是显式设计。
把 `job_id` 命名格式一改就会乱，而且不会报错。

同类的还有三处，都属于"静默算错、不抛异常"：

1. 状态判断：清单说 done、产物却不在，该判中断而不是沿用 done
2. 中断识别：清单停在 running 说明上次没跑完，不能当成功
3. 删除接口的路径校验：`job_id` 直接来自 URL，不校验就能 rmtree 掉整个 `output/`

这些用例存在的唯一理由就是守住它们。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

# ⚠ 必须在 import app 之前设好：app 在导入时就会建 RUNS_DIR 并恢复任务，
#   不隔离的话这个测试会读写真实产物。
_TMP = tempfile.mkdtemp(prefix="skyeyes-test-runs-")
os.environ["SKYEYES_OUTPUT"] = _TMP

# 同理必须在 import app 之前：`PYTHON` 是在模块级读的。
#
# `create_job` 会先校验推理解释器是否存在，不存在就报 503。而它的默认值是
# **开发机上的一个 conda 绝对路径** —— 换台机器（或部署到服务器）跑这套测试，
# 就会先撞上那个 503，而不是用例真正要断言的那个错，看起来像"代码坏了"。
# 这里给一个**当前确实存在**的解释器（就是正在跑测试的这个 python），
# 让这条链上后面那些校验能被测到。这些用例考的是参数拼装与状态流转，
# 不是推理环境本身——推理环境由 deploy/setup_env.sh 的自检负责。
os.environ.setdefault("SKYEYES_PY", sys.executable)

import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402

RUNS = Path(_TMP) / "runs"
PAYLOAD_BYTES = 1_000_000  # 每个假目录 1MB，用来把体积阈值算准


# ───────────────────────────── 夹具 ─────────────────────────────


def reset() -> None:
    app.JOBS.clear()
    RUNS.mkdir(parents=True, exist_ok=True)
    for d in RUNS.iterdir():
        shutil.rmtree(d, ignore_errors=True)


def make_run(
    name: str,
    *,
    status: str = "done",
    report: bool = True,
    manifest: bool = True,
    created_at: float | None = None,
    detail: dict | None = None,
    size_mb: float | None = None,
    payload: bool = False,
    manifest_args: list[str] | None = None,
) -> Path:
    """造一个 run 目录。report/manifest 可分别关掉，用来模拟各种残缺状态。"""
    d = RUNS / name
    d.mkdir(parents=True, exist_ok=True)
    if payload:
        (d / "blob.bin").write_bytes(b"\0" * PAYLOAD_BYTES)
    if report:
        (d / "accident_report.json").write_text(
            json.dumps({"video": "/x/burstling_street.mp4"}), encoding="utf-8"
        )
        (d / "web" / "data").mkdir(parents=True, exist_ok=True)
        (d / "web" / "data" / "demo.json").write_text("{}", encoding="utf-8")
    if manifest:
        t = created_at if created_at is not None else time.time()
        (d / app.MANIFEST).write_text(
            json.dumps(
                {
                    "version": app.MANIFEST_VERSION,
                    "job_id": name,
                    "video": "/x/burstling_street.mp4",
                    "pipeline_args": manifest_args or ["--frames-per-event", "2"],
                    "created_at": t,
                    "started_at": t,
                    "finished_at": t + 100,
                    "status": status,
                    "stage": status,
                    "stage_pct": 100.0,
                    "error": None,
                    "detail": detail if detail is not None else {"accident_frames": 296},
                    "size_mb": size_mb,
                    "log_tail": ["manifest-line"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return d


# ───────────────────── ① 落盘 / 恢复的往返 ─────────────────────


def test_persist_writes_manifest_atomically():
    reset()
    job = app.Job(
        id="20260101-000000-aaaaaa", video="/x/a.mp4", run_dir=str(RUNS / "20260101-000000-aaaaaa")
    )
    job.status = "running"
    job.stage = "detect"
    job.stage_pct = 12.5
    job.detail = {"accident_frames": 64}
    job.log = ["line-1", "line-2"]
    app._persist(job)

    mpath = RUNS / job.id / app.MANIFEST
    assert mpath.is_file(), "job.json 没写出来"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    assert m["status"] == "running"
    assert m["stage_pct"] == 12.5
    assert m["detail"]["accident_frames"] == 64
    assert m["pipeline_args"] == ["--frames-per-event", "2"], "参数要记下来，便于事后核对"
    assert m["log_tail"] == ["line-1", "line-2"]
    assert not (RUNS / job.id / f"{app.MANIFEST}.tmp").exists(), "临时文件应被 os.replace 收走"


# ─────────────────── ①b 企微告警的按任务参数 ───────────────────


def test_alert_flag_is_per_job_and_does_not_pollute_the_baseline():
    """`--alert` 只能影响这一次任务，**不能污染全局基线**。

    写成 `PIPELINE_ARGS.append("--alert")` 的话，从那一刻起**每一次**分析都会
    往企微群里推送，而且不会有任何报错——现场表现是"群里莫名其妙开始收消息"，
    排查方向完全指不到"某次点过告警开关"。
    """
    reset()
    baseline = list(app.PIPELINE_ARGS)

    with_alert = app.Job(id="j1", video="/x/a.mp4", run_dir=str(RUNS / "j1"))
    with_alert.pipeline_args = [*with_alert.pipeline_args, "--alert"]
    assert app._job_view(with_alert)["alert_requested"] is True
    assert app.PIPELINE_ARGS == baseline, "全局参数被污染了"

    plain = app.Job(id="j2", video="/x/a.mp4", run_dir=str(RUNS / "j2"))
    assert app._job_view(plain)["alert_requested"] is False
    assert "--alert" not in plain.pipeline_args


def test_pipeline_args_survive_the_manifest_round_trip():
    """运行参数要能从清单恢复出来。

    恢复不出来的话，界面会给一次**真的推送过告警**的任务显示"未开启告警"，
    与台账对不上；这类不一致不会报错，只有人肉比对才发现。
    """
    reset()
    make_run("with-alert", manifest_args=["--frames-per-event", "2", "--alert"])
    make_run("no-alert", manifest_args=["--frames-per-event", "2"])
    app._restore_runs()
    got = {j.id: "--alert" in j.pipeline_args for j in app.JOBS.values()}
    assert got["with-alert"] is True
    assert got["no-alert"] is False


def test_run_alerts_returns_none_when_absent_not_empty_dict():
    """没开告警时必须返回 None，不能返回 `{}`。

    `{}` 会让界面把"这次没开告警"显示成"开了但推了 0 条"——后者会让人去查配置。
    两种状态的含义完全不同，不能合并。清单损坏时同样退回 None（别让界面崩）。
    """
    reset()
    d = make_run("plain-done")
    job = app.Job(id="plain-done", video="x", run_dir=str(d))
    assert app._run_alerts(job) is None

    (d / "alerts.json").write_text(
        json.dumps({"enabled": True, "counts": {"sent": 1}}), encoding="utf-8"
    )
    assert app._run_alerts(job)["counts"]["sent"] == 1

    (d / "alerts.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    assert app._run_alerts(job) is None


def test_create_job_appends_alert_only_when_channel_is_configured():
    """请求告警但通道没配 → 立刻 400，**不能静默忽略**。

    静默忽略是最糟的处理：勾了开关、界面也显示"本次会推送"，唯独群里没消息，
    而且要等 6 分钟跑完才发现。配好了才把 `--alert` 追加到**这次任务的**参数上。
    """
    reset()
    started: list[str] = []
    original_run_job = app._run_job
    # 换掉 _run_job，避免测试真跑一次 6 分钟的检测
    app._run_job = lambda job: started.append(job.id)
    saved_env = os.environ.pop("SKYEYES_WECOM_WEBHOOK", None)
    try:
        try:
            asyncio.run(app.create_job({"alert": True}))
            raise AssertionError("没配 webhook 时应当抛 400")
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "webhook" in exc.detail
        assert started == [], "校验失败时不该启动任何任务"

        os.environ["SKYEYES_WECOM_WEBHOOK"] = (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=testkey"
        )
        app.JOBS.clear()
        view = asyncio.run(app.create_job({"alert": True}))
        assert view["alert_requested"] is True
        assert "--alert" in view["pipeline_args"]
        assert app.PIPELINE_ARGS == [*app.PIPELINE_ARGS], "全局基线不该被追加"
        assert "--alert" not in app.PIPELINE_ARGS

        app.JOBS.clear()
        plain = asyncio.run(app.create_job({}))
        assert plain["alert_requested"] is False
        assert "--alert" not in plain["pipeline_args"]
    finally:
        app._run_job = original_run_job
        os.environ.pop("SKYEYES_WECOM_WEBHOOK", None)
        if saved_env is not None:
            os.environ["SKYEYES_WECOM_WEBHOOK"] = saved_env


def test_delete_refuses_while_process_is_still_exiting():
    """取消后立刻删除会撞上"进程正在收尾"。

    实测复现过一次 500：`DELETE /api/jobs/{id}` 只负责 terminate，
    子进程退出和 `_run_job` 的收尾（还会写一次 job.json）都还没完成；
    界面上状态已经是 cancelled、删除按钮立刻可用，于是 rmtree 与写盘撞在一起。
    现在应当返回 409 并说清原因，而不是一个查不出所以然的 500。
    """
    reset()
    d = make_run("settling")
    job = app.Job(id="settling", video="x", run_dir=str(d))
    app.JOBS["settling"] = job

    class FakeProc:
        def poll(self):
            return None  # 还没退出

    app.PROCS["settling"] = FakeProc()
    try:
        try:
            asyncio.run(app.delete_job_artifacts("settling"))
            raise AssertionError("进程仍在退出时应当拒绝删除")
        except HTTPException as exc:
            assert exc.status_code == 409
        assert d.is_dir(), "被拒时不能动目录"
    finally:
        app.PROCS.pop("settling", None)


def test_restore_reads_back_manifest_fields():
    reset()
    d = make_run("20260102-000000-bbbbbb", detail={"accident_frames": 296, "total": 18})
    (d / app.RUN_LOG).write_text("实际日志第一行\n实际日志第二行\n", encoding="utf-8")

    app._restore_runs()
    job = app.JOBS["20260102-000000-bbbbbb"]
    assert job.status == "done"
    assert job.detail["total"] == 18, "计数类字段要从清单恢复，前端进度面板依赖它"
    assert job.log == ["实际日志第一行", "实际日志第二行"], "有 job.log 就优先用它"


def test_running_manifest_is_treated_as_interrupted():
    """核心回归：清单停在 running = 上次没跑完，绝不能当成功。"""
    reset()
    make_run("20260103-000000-cccccc", status="running", report=False)
    make_run("20260104-000000-dddddd", status="queued", report=False)

    app._restore_runs()
    for jid in ("20260103-000000-cccccc", "20260104-000000-dddddd"):
        job = app.JOBS[jid]
        assert job.status == "interrupted", f"{jid} 应判为中断，实际 {job.status}"
        assert job.error and "无产物" in job.error
        assert job.finished_at, "中断也要有时间，否则界面上显示 0 秒"


def test_done_manifest_without_report_is_interrupted():
    """清单说完成了、产物却不在——以磁盘为准。"""
    reset()
    make_run("20260105-000000-eeeeee", status="done", report=False)
    app._restore_runs()
    assert app.JOBS["20260105-000000-eeeeee"].status == "interrupted"


def test_restore_handles_legacy_dirs_without_manifest():
    """本次改动之前产生的目录没有 job.json，要退回老办法而不是丢掉。"""
    reset()
    make_run("20260106-000000-ffffff", manifest=False, report=True)
    make_run("20260107-000000-111111", manifest=False, report=False)

    app._restore_runs()
    assert app.JOBS["20260106-000000-ffffff"].status == "done"
    assert app.JOBS["20260107-000000-111111"].status == "interrupted"


def test_broken_manifest_falls_back_instead_of_crashing():
    reset()
    d = make_run("20260108-000000-222222")
    (d / app.MANIFEST).write_text("{ 这不是 json", encoding="utf-8")
    app._restore_runs()
    assert app.JOBS["20260108-000000-222222"].status == "done", "清单坏了应退回看产物"


# ───────────── ② 顺序：真实 created_at，而不是排序巧合 ─────────────


def test_restore_preserves_real_created_at_and_order():
    """核心回归：以前恢复出来的 created_at 全是"现在"，顺序只靠巧合。"""
    reset()
    # 故意让"目录名顺序"与"真实创建时间"相反
    make_run("20260120-000000-late", created_at=2_000_000_000.0)
    make_run("20260121-000000-early", created_at=1_000_000_000.0)

    app._restore_runs()
    assert app.JOBS["20260120-000000-late"].created_at == 2_000_000_000.0
    assert app.JOBS["20260121-000000-early"].created_at == 1_000_000_000.0

    ids = [j.id for j in sorted(app.JOBS.values(), key=lambda j: j.created_at, reverse=True)]
    assert ids == ["20260120-000000-late", "20260121-000000-early"], (
        "应按真实创建时间倒序；若这里挂了，说明顺序又回到了靠目录名/插入序"
    )


# ───────────────────── ③ 收尾与体积 ─────────────────────


def test_finalize_sets_status_size_and_persists():
    reset()
    job = app.Job(
        id="20260130-000000-333333", video="/x/a.mp4", run_dir=str(RUNS / "20260130-000000-333333")
    )
    (Path(job.run_dir)).mkdir(parents=True)
    (Path(job.run_dir) / "blob.bin").write_bytes(b"\0" * 2_000_000)

    app._finalize(job, "error", "推理进程退出码 1")
    assert job.status == "error"
    assert job.error == "推理进程退出码 1"
    assert job.stage == "error", "终态要同步到 stage，否则进度条会停在中途某段"
    assert job.size_mb is not None and job.size_mb > 1.5, f"体积没算对：{job.size_mb}"

    m = json.loads((Path(job.run_dir) / app.MANIFEST).read_text(encoding="utf-8"))
    assert m["status"] == "error"
    assert m["size_mb"] == job.size_mb


def test_elapsed_never_goes_negative():
    """端到端测试时真的看到过 -3022.7 秒。

    成因：`finished_at - started_at` 的两端来自不同来源（清单 vs 文件 mtime），
    或者机器时钟被校过。界面上会显示成 `-50:22`，比不显示还糟。
    """
    job = app.Job(id="x", video="", run_dir=str(RUNS / "x"))
    job.started_at = time.time() + 3600  # 起在"未来"
    job.finished_at = time.time()
    assert app._elapsed(job) == 0.0, f"负数耗时没被夹住：{app._elapsed(job)}"

    job.started_at = None
    assert app._elapsed(job) == 0.0


def test_restore_fixes_inverted_started_finished():
    """起止倒挂时以"结束"为准，而不是把负数带到界面上。"""
    reset()
    make_run("20260131-000000-777777", status="done")
    p = RUNS / "20260131-000000-777777" / app.MANIFEST
    m = json.loads(p.read_text(encoding="utf-8"))
    m["started_at"] = m["finished_at"] + 5000  # 倒挂
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    app._restore_runs()
    job = app.JOBS["20260131-000000-777777"]
    assert job.started_at <= job.finished_at, "恢复时没纠正倒挂的起止时间"
    assert app._elapsed(job) >= 0


# ───────────────────── ④ 清理策略 ─────────────────────


def test_prune_removes_oldest_but_protects_busy_and_newest():
    reset()
    names = [f"2026020{i}-000000-{i}{i}{i}{i}{i}{i}" for i in range(1, 6)]
    for i, n in enumerate(names):
        make_run(n, created_at=1_000.0 + i, payload=True)
        # ⚠ 必须显式写状态：Job 的默认状态是 "queued"，那在 _prune_runs 眼里
        #   就是"待跑/正在跑"，全都会被保护起来——第一版测试就栽在这里。
        job = app.Job(id=n, video="", run_dir=str(RUNS / n))
        job.status = "running" if i == 0 else "done"
        app.JOBS[n] = job

    old_gb, old_keep = app.MAX_RUNS_GB, app.MIN_KEEP_RUNS
    app.MAX_RUNS_GB, app.MIN_KEEP_RUNS = 0.003, 2  # 上限约 3MB，5 个目录共约 5MB
    try:
        removed = app._prune_runs("测试")
    finally:
        app.MAX_RUNS_GB, app.MIN_KEEP_RUNS = old_gb, old_keep

    assert removed == [names[1], names[2]], f"应删中间两个，实际删了 {removed}"
    assert (RUNS / names[0]).exists(), "正在跑的任务绝不能被清理"
    assert (RUNS / names[3]).exists() and (RUNS / names[4]).exists(), "最新两个要保留"
    assert not (RUNS / names[1]).exists() and not (RUNS / names[2]).exists()
    assert names[1] not in app.JOBS and names[2] not in app.JOBS, "清单要同步移除"


def test_prune_protects_queued_jobs():
    """核心回归：状态默认是 queued，而 queued 也属于"别动它"。"""
    reset()
    names = [f"2026021{i}-000000-{i}{i}{i}{i}{i}{i}" for i in range(1, 5)]
    for i, n in enumerate(names):
        make_run(n, created_at=1_000.0 + i, payload=True)
        job = app.Job(id=n, video="", run_dir=str(RUNS / n))  # 状态留默认 queued
        app.JOBS[n] = job

    old_gb, old_keep = app.MAX_RUNS_GB, app.MIN_KEEP_RUNS
    app.MAX_RUNS_GB, app.MIN_KEEP_RUNS = 0.001, 1
    try:
        assert app._prune_runs("测试") == [], "待跑的任务一个都不能删"
    finally:
        app.MAX_RUNS_GB, app.MIN_KEEP_RUNS = old_gb, old_keep
    assert len(list(RUNS.iterdir())) == 4


def test_prune_is_noop_under_threshold():
    reset()
    for i in range(3):
        make_run(f"2026030{i}-000000-{i}{i}{i}{i}{i}{i}", payload=True)
    old_gb = app.MAX_RUNS_GB
    app.MAX_RUNS_GB = 100.0
    try:
        assert app._prune_runs("测试") == [], "没超阈值就一个都不能删"
    finally:
        app.MAX_RUNS_GB = old_gb
    assert len(list(RUNS.iterdir())) == 3


# ───────────────────── ⑤ 删除接口（含路径校验） ─────────────────────


def test_safe_run_dir_rejects_traversal():
    """核心回归：job_id 来自 URL，不校验就能删掉 output/。"""
    reset()
    make_run("20260401-000000-444444")
    assert app._safe_run_dir("20260401-000000-444444") is not None

    for bad in [
        "",
        ".",
        "..",
        "../runs",
        "../../output",
        "../../../etc",
        "/etc",
        "/tmp",
        "a/b",
        "20260401-000000-444444/../..",
        "./20260401-000000-444444",
        "不存在的任务",
    ]:
        assert app._safe_run_dir(bad) is None, f"{bad!r} 不该被接受"


def test_delete_artifacts_removes_dir_and_updates_list():
    reset()
    make_run("20260402-000000-555555")
    app._restore_runs()
    before = app.JOBS["20260402-000000-555555"].size_mb

    res = asyncio.run(app.delete_job_artifacts("20260402-000000-555555"))
    assert res["deleted"] == "20260402-000000-555555"
    assert res["freed_mb"] == before or res["freed_mb"] >= 0
    assert not (RUNS / "20260402-000000-555555").exists(), "目录应被真删掉"
    assert "20260402-000000-555555" not in app.JOBS, "清单也要移除"


def test_delete_refuses_running_job_and_traversal():
    reset()
    make_run("20260403-000000-666666")
    app._restore_runs()
    app.JOBS["20260403-000000-666666"].status = "running"  # 手动摆成"正在跑"
    try:
        asyncio.run(app.delete_job_artifacts("20260403-000000-666666"))
    except HTTPException as exc:
        assert exc.status_code == 409, f"运行中应返回 409，实际 {exc.status_code}"
    else:
        raise AssertionError("运行中的任务不该被删除")
    assert (RUNS / "20260403-000000-666666").exists()

    try:
        asyncio.run(app.delete_job_artifacts("../../output"))
    except HTTPException as exc:
        assert exc.status_code in (400, 404)
    else:
        raise AssertionError("目录穿越不该被接受")


def test_real_output_never_touched():
    """兜底断言：上面的用例都是在临时目录里跑的，真实产物必须完好。"""
    real = ROOT / "output" / "accident_report.json"
    assert real.is_file(), "真实 output/ 被动了！"
    assert not str(RUNS).startswith(str(ROOT / "output")), "RUNS_DIR 必须已隔离到临时目录"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
        finally:
            app.JOBS.clear()
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    print(f"{len(fns) - failed}/{len(fns)} 通过")
    raise SystemExit(1 if failed else 0)
