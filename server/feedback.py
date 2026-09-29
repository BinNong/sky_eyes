"""人工复核反馈与模型可信度指标。

为什么需要这个模块
    `/analytics` 上原有的"质量"数字全是**工程可用性**——18/18 成功、0 失败、
    0 退化、5 次追问补判。它们回答的是"模型有没有正常应答"，
    而不是"**答对了没有**"。把前者当后者展示，会让看的人以为精度已经验证过，
    而实际上一次都没有验证。这个模块补的是后者，数据只能来自人的判断。

三条刻意的约束（都是为了不让指标"看起来有数字"）
    1. **没有复核时不给数字。** 分母为 0 一律返回 None，不返回 0 也不返回 100。
       "尚未复核"和"准确率 0%"是完全不同的两件事，混在一起就是误导。
    2. **按 (run_id, event_id) 唯一。** 重复提交是覆盖，不是追加。
       若追加，同一个人点两次"误报"就会被记成两次误报，指标随点击次数漂移。
    3. **误报不进等级混淆矩阵。** 误报的事件不是事故，没有"真值等级"，
       硬塞进矩阵会在某一行凭空多出一格，让矩阵的对角线和不再等于判对数。

还有一条边界要写在界面上：**本模块不含漏检**。
    漏检 = 视频里真出了事故但检测器没报。要算它必须有一份"负样本标注集"
    （人工看过那些没被检出的帧），那是另一项工作，不能拿这里的数字冒充。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# ───────────────────────────── 位置与常量 ─────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_DIR = Path(os.environ.get("SKYEYES_FEEDBACK", PROJECT_ROOT / "feedback"))
STORE = "verdicts.json"
STORE_VERSION = 1

#: 静态 demo 数据集用的 run_id；实时任务用它的 job_id。
STATIC_RUN = "static"

SEVERITIES = ("低", "中", "高")

CORRECT = "correct"
WRONG_SEVERITY = "wrong_severity"
FALSE_POSITIVE = "false_positive"
VERDICTS = (CORRECT, WRONG_SEVERITY, FALSE_POSITIVE)

_LOCK = threading.Lock()


# ───────────────────────────── 读写 ─────────────────────────────


def _read() -> list[dict]:
    path = FEEDBACK_DIR / STORE
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 反馈文件坏了就当空的重来。它是"锦上添花"的数据，
        # 为它让整个接口 500 不值得——但会记一条到 stderr 便于排查。
        print(f"[反馈] {path} 无法解析，按空处理")
        return []
    items = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [v for v in items if isinstance(v, dict)]


def _write(items: list[dict]) -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = FEEDBACK_DIR / STORE
    # 原子写：先落临时文件再 rename。直接覆写的话，正好在写一半时被杀
    # 会留下截断的 JSON，那份"复核记录"就静默没了。
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {"version": STORE_VERSION, "verdicts": items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ───────────────────────────── 写入 / 撤销 ─────────────────────────────


def put_verdict(
    run_id: str,
    event_id: int,
    verdict: str,
    predicted_severity: str | None = None,
    true_severity: str | None = None,
    note: str | None = None,
) -> dict:
    """写入一条复核结论。同一 (run_id, event_id) 覆盖。

    `true_severity` 的归一规则集中在这里，就是为了防止调用方各行其是：
        误报      -> None（不是事故，没有真值等级）
        判定正确  -> 等于模型判定（判定正确就意味着等级也对）
        等级错判  -> 由调用方给出，且必须与模型判定不同
    """
    if verdict not in VERDICTS:
        raise ValueError(f"未知判定：{verdict}")

    pred = predicted_severity if predicted_severity in SEVERITIES else None

    if verdict == FALSE_POSITIVE:
        true = None
    elif verdict == CORRECT:
        # 模型连等级都没判出来（未判定）时，"判定正确"这句话没有内容——
        # 而如果放它过去，它会同时计入 correct 却不进混淆矩阵（没有合法的预测等级），
        # 于是"对角线和 == 判对数"这个恒等式被悄悄破坏。
        # 这种情况必须让人工给出等级，走 wrong_severity 那条路。
        if pred is None:
            raise ValueError("模型未判出等级，不能选「判定正确」，请给出正确的等级")
        true = pred
    else:
        if true_severity not in SEVERITIES:
            raise ValueError("判为等级错误时，必须给出正确的等级（低/中/高）")
        if true_severity == pred:
            raise ValueError("给出的等级与模型判定相同，这种情况应选「判定正确」")
        true = true_severity

    entry = {
        "run_id": run_id,
        "event_id": int(event_id),
        "verdict": verdict,
        "predicted_severity": pred,
        "true_severity": true,
        "at": time.time(),
        "note": (note or "").strip() or None,
    }

    with _LOCK:
        items = [
            v
            for v in _read()
            if not (v.get("run_id") == run_id and v.get("event_id") == entry["event_id"])
        ]
        items.append(entry)
        _write(items)
    return entry


def clear_verdict(run_id: str, event_id: int) -> bool:
    """撤销一条。返回是否真的有东西被删掉。"""
    with _LOCK:
        items = _read()
        kept = [
            v
            for v in items
            if not (v.get("run_id") == run_id and v.get("event_id") == int(event_id))
        ]
        if len(kept) == len(items):
            return False
        _write(kept)
    return True


def clear_all(run_id: str | None = None) -> int:
    """清空全部，或只清某个数据集。返回清掉的条数。"""
    with _LOCK:
        items = _read()
        kept = [v for v in items if run_id is not None and v.get("run_id") != run_id]
        removed = len(items) - len(kept)
        if removed:
            _write(kept)
    return removed


def by_run(run_id: str) -> list[dict]:
    return [v for v in _read() if v.get("run_id") == run_id]


# ───────────────────────────── 指标 ─────────────────────────────


def compute_metrics(verdicts: list[dict], total_events: int | None) -> dict:
    """基于已复核的结论算指标。

    两个指标的分母不同，回答的是不同问题，别混用：
        精确率   = (判对 + 等级错) / 已复核     ← 模型报出来的事件里，真的是事故的比例
        等级准确率 = 判对 / (判对 + 等级错)      ← 在确实是事故的事件里，等级判对的比例

    等级错判计入精确率的分子，因为那种情况**检测是对的**，只是等级不准；
    把它算成假阳性会把"检测好不好"和"等级准不准"混成一件事。
    """
    reviewed = len(verdicts)
    correct = sum(1 for v in verdicts if v.get("verdict") == CORRECT)
    wrong = sum(1 for v in verdicts if v.get("verdict") == WRONG_SEVERITY)
    false_pos = sum(1 for v in verdicts if v.get("verdict") == FALSE_POSITIVE)

    precision = ((correct + wrong) / reviewed) if reviewed else None

    graded = correct + wrong
    severity_accuracy = (correct / graded) if graded else None

    confusion = {p: {t: 0 for t in SEVERITIES} for p in SEVERITIES}
    for v in verdicts:
        if v.get("verdict") == FALSE_POSITIVE:
            continue  # 没有真值等级
        p, t = v.get("predicted_severity"), v.get("true_severity")
        if p in confusion and t in SEVERITIES:
            confusion[p][t] += 1

    evaluated = reviewed > 0
    return {
        "total_events": total_events,
        "reviewed": reviewed,
        "correct": correct,
        "wrong_severity": wrong,
        "false_positive": false_pos,
        # None 表示"算不出来"，不是 0
        "precision": precision,
        "severity_accuracy": severity_accuracy,
        "confusion": confusion,
        # 对角线之和必须等于 correct —— 测试里有一条专门守这个恒等式
        "confusion_diagonal": sum(confusion[s][s] for s in SEVERITIES),
        "evaluated": evaluated,
        # 复核覆盖率：只复核了 2 起时说"精确率 100%"是有误导性的，必须带上样本量
        "coverage": (reviewed / total_events) if (total_events and reviewed) else None,
    }
