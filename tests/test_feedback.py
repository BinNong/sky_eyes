"""人工复核反馈与可信度指标的回归测试。

跑法（用后端自己的 venv，它只有 fastapi/httpx，不装 torch）：

    server/.venv/bin/python tests/test_feedback.py

不需要 GPU、不需要网络、不需要真跑分析。

为什么专门给这块写测试

这个模块算的是"模型答对了没有"。它和本项目其他几处一样，
**不会抛异常，只会安静地给出错的数字**——而错的精度指标比没有指标更糟：
没有指标时人会存疑，有指标时人会相信。

所以下面每条用例都对应一个具体的、已经能想清楚后果的失效模式：

1. 无反馈时返回 0 / 100 —— 把"尚未复核"说成"准确率 0%"或"准确率 100%"
2. 重复提交累加 —— 点两次"误报"被记成两次误报，指标随点击次数漂移
3. 误报进等级混淆矩阵 —— 它在对角线上凭空多一格，且让对角线和不再等于判对数
4. 误报拉低等级准确率 —— 把"检测准不准"和"等级准不准"混成一件事
5. run_id 不隔离 —— 实时任务的判定把静态 demo 的指标带偏
6. 覆盖率缺 total —— 只复核 2 起就说"精确率 100%"，却没给样本量
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# 必须在 import feedback 之前设环境变量——FEEDBACK_DIR 是模块级常量。
_TMP = tempfile.mkdtemp(prefix="skyeyes-feedback-test-")
os.environ["SKYEYES_FEEDBACK"] = _TMP

import feedback as fb  # noqa: E402

FB_DIR = Path(_TMP)


def reset():
    shutil.rmtree(FB_DIR, ignore_errors=True)


def write(run="static", event_id=1, verdict=fb.CORRECT, pred="高", true=None):
    return fb.put_verdict(
        run_id=run, event_id=event_id, verdict=verdict,
        predicted_severity=pred, true_severity=true,
    )


def m(run="static", total=9):
    items = fb.by_run(run)
    return fb.compute_metrics(items, total)


# ───────────────────── 1. 没有复核时不许给数字 ─────────────────────


def test_empty_returns_none_not_zero():
    """没有复核记录时，指标必须是 None。

    这是本模块最重要的一条约束。返回 0 会被读成"准确率 0%"（模型完全不可用），
    返回 1.0 会被读成"完美"。两者都不是事实——事实是**还没测过**。
    """
    reset()
    got = m("static", total=9)
    assert got["reviewed"] == 0
    assert got["precision"] is None, f"精确率应为 None，实际 {got['precision']}"
    assert got["severity_accuracy"] is None
    assert got["coverage"] is None
    assert got["evaluated"] is False
    assert got["confusion_diagonal"] == 0  # 这个可以是 0，它表示"累计判对数"


def test_total_missing_still_safe():
    """连事件总数都不知道时（数据集缺失），也不能崩、不能编数字。"""
    reset()
    write(verdict=fb.CORRECT)
    got = m("static", total=None)
    assert got["reviewed"] == 1
    assert got["precision"] == 1.0
    assert got["coverage"] is None, "没有 total 就不该编覆盖率"


# ───────────────────── 2. 两个指标的分母必须分开 ─────────────────────


def test_all_correct():
    reset()
    for i, s in enumerate(["低", "中", "高", "高"], start=1):
        write(event_id=i, pred=s)
    got = m(total=9)
    assert got["precision"] == 1.0
    assert got["severity_accuracy"] == 1.0
    assert got["confusion_diagonal"] == got["correct"] == 4


def test_false_positive_lowers_precision_only():
    """误报只该影响精确率，不该污染等级准确率。

    误报的事件根本不是事故，它没有"真值等级"。
    如果把它算进等级准确率的分母，会得出"等级准确率下降"的结论，
    而事实上模型在**真实事故**上的等级判断一个都没错。
    """
    reset()
    write(event_id=1, pred="高")
    write(event_id=2, pred="中")
    write(event_id=3, verdict=fb.FALSE_POSITIVE, pred="高")

    got = m(total=9)
    assert got["correct"] == 2
    assert got["false_positive"] == 1
    # 3 次复核里 2 次是真的 -> 2/3
    assert abs(got["precision"] - 2 / 3) < 1e-9, got["precision"]
    # 两次真事故等级都对 -> 100%，不因为那次误报掉下来
    assert got["severity_accuracy"] == 1.0, (
        f"误报污染了等级准确率：{got['severity_accuracy']}"
    )


def test_wrong_severity_lowers_accuracy_only():
    """等级错判仍是"真的发生了事故"，所以精确率的分子要算上它。

    把等级错判当假阳性，会让"检测漏报"和"等级不准"两个完全不同的问题
    显示成同一个数字，使用方无法判断该去修哪一环。
    """
    reset()
    write(event_id=1, pred="高")
    write(event_id=2, pred="低", verdict=fb.WRONG_SEVERITY, true="高")

    got = m(total=9)
    assert got["precision"] == 1.0, f"等级错判不该拉低精确率：{got['precision']}"
    assert got["severity_accuracy"] == 0.5
    assert got["wrong_severity"] == 1


# ───────────────────── 3. 重复提交必须是覆盖 ─────────────────────


def test_resubmit_overwrites_not_appends():
    """同一个人对同一事件改判，必须覆盖。

    若累加：先点"判定正确"再改成"误报"，会被算成 1 对 1 错 —— 精确率 50%，
    而实际上只有 1 起事件、且结论是误报。指标会随点击次数漂移。
    """
    reset()
    write(event_id=7, pred="高")
    assert m()["correct"] == 1

    write(event_id=7, verdict=fb.FALSE_POSITIVE, pred="高")
    got = m(total=9)
    assert got["reviewed"] == 1, f"应只剩 1 条，实际 {got['reviewed']}"
    assert got["correct"] == 0
    assert got["false_positive"] == 1
    assert got["precision"] == 0.0

    # 再改回来也要正确覆盖
    write(event_id=7, pred="中")
    got = m(total=9)
    assert got["reviewed"] == 1
    assert got["correct"] == 1
    assert got["precision"] == 1.0


def test_resubmit_keeps_store_file_single_entry():
    """覆盖不只是内存里数字对——落盘文件里也只能有一条。"""
    reset()
    for _ in range(5):
        write(event_id=3, pred="中")
    raw = json.loads((FB_DIR / fb.STORE).read_text(encoding="utf-8"))
    assert len(raw["verdicts"]) == 1, f"文件里堆了 {len(raw['verdicts'])} 条"


# ───────────────────── 4. 数据集之间必须隔离 ─────────────────────


def test_run_isolation():
    """实时任务的复核记录不能算进静态 demo 的指标。

    这两种数据的事件编号会重复（都有 event 1..9），混在一起会张冠李戴；
    而演示时先看静态再看实时是常规操作。
    """
    reset()
    write(run="static", event_id=1, pred="高")
    write(run="20260920-120000-abcdef", event_id=1, verdict=fb.FALSE_POSITIVE, pred="高")
    write(run="20260920-120000-abcdef", event_id=2, pred="中")

    static = m("static")
    live = m("20260920-120000-abcdef")

    assert static["reviewed"] == 1 and static["correct"] == 1
    assert live["reviewed"] == 2 and live["false_positive"] == 1
    assert static["precision"] == 1.0
    assert abs(live["precision"] - 0.5) < 1e-9


# ───────────────────── 5. 混淆矩阵 ─────────────────────


def test_confusion_diagonal_equals_correct():
    """恒等式：对角线和 == 判对数。

    它在两个地方会被破坏：误报被塞进矩阵（对角线凭空多一格），
    或 predicted/true 有一个不在 低中高 里（被静默跳过）。
    """
    reset()
    write(event_id=1, pred="高")
    write(event_id=2, pred="低")
    write(event_id=3, pred="中", verdict=fb.WRONG_SEVERITY, true="高")
    write(event_id=4, verdict=fb.FALSE_POSITIVE, pred="中")
    write(event_id=5, pred="中")

    got = m(total=9)
    assert got["confusion_diagonal"] == got["correct"], (
        f"对角线和 {got['confusion_diagonal']} != 判对数 {got['correct']}"
    )


def test_false_positive_excluded_from_confusion():
    """误报没有真值等级，不能进矩阵的任何一格。"""
    reset()
    write(event_id=1, verdict=fb.FALSE_POSITIVE, pred="高")
    got = m(total=9)
    total_cells = sum(sum(row.values()) for row in got["confusion"].values())
    assert total_cells == 0, f"误报进了混淆矩阵：{got['confusion']}"
    assert got["confusion_diagonal"] == 0


def test_confusion_cells_placed_correctly():
    """错判必须落在 (预测行, 真值列)，不能落到对角线或转置位置。

    转置是最容易写错的一种：矩阵看起来照样"有数据"、和也照样对得上，
    但行与列的含义整体反了，读出来的结论是错的。
    """
    reset()
    write(event_id=1, pred="高", verdict=fb.WRONG_SEVERITY, true="低")
    write(event_id=2, pred="低", verdict=fb.WRONG_SEVERITY, true="高")

    c = m(total=9)["confusion"]
    assert c["高"]["低"] == 1, f"应为 高(预测)->低(真值)：{c}"
    assert c["低"]["高"] == 1
    assert c["高"]["高"] == 0 and c["低"]["低"] == 0


def test_confusion_has_full_3x3():
    """矩阵必须是完整的 3x3（含 0 格）。

    缺格会让前端渲染时对不上坐标，而且"没有数据"与"数量为 0"分不清。
    """
    reset()
    write(event_id=1, pred="中")
    got = m(total=9)
    assert set(got["confusion"]) == set(fb.SEVERITIES)
    for row in fb.SEVERITIES:
        assert set(got["confusion"][row]) == set(fb.SEVERITIES)


# ───────────────────── 6. 写入时的归一与校验 ─────────────────────


def test_correct_fills_true_from_predicted():
    """选「判定正确」就意味着等级也对，真值等级自动等于模型判定。"""
    reset()
    e = write(event_id=1, pred="高")
    assert e["predicted_severity"] == "高"
    assert e["true_severity"] == "高"


def test_false_positive_has_no_true_severity():
    """误报不是事故，真值等级必须是 None——留个等级会把它带进混淆矩阵。"""
    reset()
    e = write(event_id=1, verdict=fb.FALSE_POSITIVE, pred="高", true="低")
    assert e["true_severity"] is None, f"误报不该带真值等级：{e}"


def test_wrong_severity_requires_valid_true():
    reset()
    for bad in (None, "", "严重", "极高"):
        try:
            write(event_id=1, verdict=fb.WRONG_SEVERITY, pred="高", true=bad)
        except ValueError:
            continue
        raise AssertionError(f"非法真值等级 {bad!r} 竟然被接受了")


def test_reject_true_equals_predicted():
    """真值等级与模型判定相同，说明选错了按钮（应该选「判定正确」）。

    放过去的话，混淆矩阵的对角线会走这条路径而不是 correct 的分支，
    两处计数就会不一致。
    """
    reset()
    try:
        write(event_id=1, verdict=fb.WRONG_SEVERITY, pred="高", true="高")
    except ValueError:
        return
    raise AssertionError("真值等级与预测相同时也被接受了")


def test_reject_unknown_verdict():
    reset()
    try:
        write(event_id=1, verdict="maybe")
    except ValueError:
        return
    raise AssertionError("未知判定值被接受了")


def test_correct_rejected_when_model_gave_no_severity():
    """模型没判出等级时，不能选「判定正确」。

    这是写前端时才想到的边界：`correct` 会计入判对数，却没有合法的预测等级
    可以放进混淆矩阵，于是 `confusion_diagonal == correct` 会被静默破坏。
    实际数据里 9 起事件都有等级，所以线上碰不到——但边界必须堵住。
    """
    reset()
    try:
        write(event_id=1, verdict=fb.CORRECT, pred=None)
    except ValueError:
        return
    raise AssertionError("模型未判定时，「判定正确」竟然被接受了")


def test_diagonal_identity_holds_with_unknown_severity():
    """预测等级为 None 时的错判，也不能破坏对角线和恒等式。"""
    reset()
    write(event_id=1, pred="高")
    e = write(event_id=2, pred=None, verdict=fb.WRONG_SEVERITY, true="高")
    assert e["predicted_severity"] is None

    got = m(total=9)
    assert got["confusion_diagonal"] == got["correct"], (
        f"对角线和 {got['confusion_diagonal']} != 判对数 {got['correct']}"
    )


def test_unknown_predicted_severity_is_nulled():
    """模型没判出等级（未判定）时，predicted 归一为 None，不能是任意字符串。"""
    reset()
    e = write(event_id=1, verdict=fb.FALSE_POSITIVE, pred="未知")
    assert e["predicted_severity"] is None
    e2 = write(event_id=2, verdict=fb.CORRECT, pred="中")
    assert e2["predicted_severity"] == "中"


# ───────────────────── 7. 撤销与清空 ─────────────────────


def test_clear_single_and_all():
    reset()
    write(run="static", event_id=1)
    write(run="static", event_id=2)
    write(run="jobA", event_id=1)

    assert fb.clear_verdict("static", 1) is True
    assert len(fb.by_run("static")) == 1
    assert len(fb.by_run("jobA")) == 1, "撤销不该动别的数据集"

    assert fb.clear_verdict("static", 999) is False, "撤销不存在的应返回 False"

    assert fb.clear_all("static") == 1
    assert fb.by_run("static") == []
    assert len(fb.by_run("jobA")) == 1

    assert fb.clear_all() == 1
    assert fb.by_run("jobA") == []


# ───────────────────── 8. 落盘 ─────────────────────


def test_persist_roundtrip():
    """重启后复核记录必须还在——它是人工成本，丢了就没人愿意再标一遍。"""
    reset()
    write(event_id=5, pred="高")
    write(event_id=6, verdict=fb.WRONG_SEVERITY, pred="中", true="高")

    # 不带任何缓存，直接重新读文件（等价于进程重启）
    items = fb._read()
    assert len(items) == 2
    got = fb.compute_metrics(items, 9)
    assert got["reviewed"] == 2
    assert got["correct"] == 1 and got["wrong_severity"] == 1
    assert got["severity_accuracy"] == 0.5


def test_corrupt_file_degrades_to_empty():
    """文件损坏时按空处理，不能让接口 500、也不能读出半截数据当真。"""
    reset()
    write(event_id=1)
    (FB_DIR / fb.STORE).write_text('{"verdicts": [{"run_id": "stat', encoding="utf-8")
    assert fb._read() == []
    got = m(total=9)
    assert got["reviewed"] == 0 and got["precision"] is None


def test_no_leftover_tmp_file():
    """原子写的临时文件不该留在目录里。"""
    reset()
    write(event_id=1)
    leftovers = [p.name for p in FB_DIR.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"残留临时文件：{leftovers}"


def test_coverage_reported():
    """只复核了一部分时必须给出覆盖率——否则"精确率 100%"会被当成整体结论。"""
    reset()
    write(event_id=1)
    got = m(total=9)
    assert got["reviewed"] == 1 and got["total_events"] == 9
    assert abs(got["coverage"] - 1 / 9) < 1e-9


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
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    print(f"{len(fns) - failed}/{len(fns)} 通过")
    raise SystemExit(1 if failed else 0)
