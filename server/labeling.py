"""检测召回率的抽样标注与指标计算。

为什么需要它
    P8 的「模型可信度」能算"模型报出来的事件里有多少是真的"（精确率），
    但算不出**召回率**——因为没有人看过"模型**没报**的那些帧"。
    而召回率恰恰是交管场景最在意的：**漏报一起高等级事故，比多报十次更严重**。

    算召回率没有捷径：必须有人真的看过那些没被检出的帧。
    本模块负责把人的判断变成指标——题目在 `web/public/labeling/set.json`
    （由 `scripts/make_label_set.py` 抽样生成），答案在这里的 `truth.json`。

抽样口径（四层，按漏检概率分层）
    detected  检出帧（296）        → 帧级精确率：报出来的有多少是真的
    inside    事故区间内未检出（45） → 漏检。事故还在持续，模型却断了
    boundary  区间外侧 4 帧内未检出（72）→ 漏检。事故起止被截断
    far       远离事故区未检出（546）   → 漏检。真正的"没事故"时段

    指标由这四层的抽样**外推**到全体，所以每一层都必须有样本，
    否则对应部分的漏检量就无从估计——`compute()` 在这种情况下返回 None 而不是猜。

三条刻意的约束
    1. **没有标注时不给数字。** 分母为 0 一律返回 None，不返回 0 或 100。
       "还没标"和"漏检 0%"是完全不同的结论。
    2. **召回率必须带不确定性。** 全部指标都由抽样得出，"召回率 98%"这种
       光秃秃的数字会让人以为它是精确值。这里一律附 Wilson 95% 区间。
    3. **不把 boundary / inside 的漏检率当成全视频的漏检率。**
       这两层是**有意偏置**的（专挑最可能漏检的位置），单独看会得出
       "漏检 60%"这种吓人的数字。它们只用来外推各自层的漏检总量。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

# ───────────────────────────── 位置与常量 ─────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 题目（抽样清单）。在 web/public 下，因为它同时要给前端取图用。
LABEL_DIR = Path(
    os.environ.get("SKYEYES_LABEL_DIR", PROJECT_ROOT / "web" / "public" / "labeling")
)
SET_FILE = Path(os.environ.get("SKYEYES_LABEL_SET", LABEL_DIR / "set.json"))

#: 答案（人工标注）。与 feedback/ 并列放项目根，不进版本库。
TRUTH_DIR = Path(os.environ.get("SKYEYES_LABEL_TRUTH", PROJECT_ROOT / "labeling"))
TRUTH_FILE = TRUTH_DIR / "truth.json"
TRUTH_VERSION = 1

ACCIDENT = "accident"
NONE = "none"
TRUTH_VALUES = (ACCIDENT, NONE)

DETECTED = "detected"
MISS_STRATA = ("inside", "boundary", "far")
STRATA = (DETECTED, *MISS_STRATA)

#: Wilson 区间用的 z 值（95%）
Z = 1.96

_LOCK = threading.Lock()


# ───────────────────────────── 抽样清单 ─────────────────────────────


def load_set() -> dict | None:
    """读抽样清单。没跑过 make_label_set.py 时返回 None。"""
    if not SET_FILE.is_file():
        return None
    try:
        data = json.loads(SET_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


# ───────────────────────────── 标注读写 ─────────────────────────────


def _read_truth() -> dict[str, dict]:
    if not TRUTH_FILE.is_file():
        return {}
    try:
        raw = json.loads(TRUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"[标注] {TRUTH_FILE} 无法解析，按空处理")
        return {}
    labels = raw.get("labels") if isinstance(raw, dict) else None
    return labels if isinstance(labels, dict) else {}


def _write_truth(labels: dict[str, dict]) -> None:
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    # 原子写：标注是人工成本，写坏一次就白标了
    tmp = TRUTH_FILE.with_name(TRUTH_FILE.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": TRUTH_VERSION, "labels": labels}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, TRUTH_FILE)


def put_label(item_id: str, truth: str) -> dict:
    """记录一条标注。同一 id 重复提交是覆盖。

    校验 id 必须存在于抽样清单里：拼错的 id 会变成一条永远不会被任何层统计到的
    孤立记录，标注者以为标了、指标却没变——这种"静默丢失"必须堵住。
    """
    if truth not in TRUTH_VALUES:
        raise ValueError(f"未知标注值：{truth}（只能是 {TRUTH_VALUES}）")

    s = load_set()
    if s is None:
        raise ValueError("尚无抽样清单，请先运行 scripts/make_label_set.py")
    if item_id not in {it.get("id") for it in s["items"]}:
        raise ValueError(f"清单里没有这一项：{item_id}")

    entry = {"truth": truth, "at": time.time()}
    with _LOCK:
        labels = _read_truth()
        labels[item_id] = entry
        _write_truth(labels)
    return {"id": item_id, **entry}


def clear_labels(item_id: str | None = None) -> int:
    """撤销一条（给 id）或清空全部。"""
    with _LOCK:
        labels = _read_truth()
        if item_id is None:
            n = len(labels)
            if n:
                _write_truth({})
            return n
        if item_id not in labels:
            return 0
        labels.pop(item_id)
        _write_truth(labels)
        return 1


# ───────────────────────────── 统计 ─────────────────────────────


def wilson(successes: int, n: int, z: float = Z) -> tuple[float, float] | None:
    """比例的 Wilson 95% 区间。

    用它而不是 `p ± 1.96·sqrt(p(1-p)/n)`：后者在 p=0 或 p=1 时给出**零宽**区间——
    "16 帧全判对"会被说成"精确率恰好 100%，毫无误差"，而这是最需要如实说明
    不确定性的时刻。Wilson 在边界上依然给出合理的宽度。
    """
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def _stratum_stat(items: list[dict], labels: dict[str, dict], stratum: str, population: int) -> dict:
    rows = [it for it in items if it.get("stratum") == stratum]
    labeled = [r for r in rows if labels.get(r["id"], {}).get("truth") in TRUTH_VALUES]
    hits = sum(1 for r in labeled if labels[r["id"]]["truth"] == ACCIDENT)
    n = len(labeled)
    return {
        "stratum": stratum,
        "sampled": len(rows),
        "labeled": n,
        "accident": hits,
        "population": population,
        # 该层"抽样帧中判为有事故"的比例。n=0 时为 None，不是 0。
        "rate": (hits / n) if n else None,
        "ci": wilson(hits, n),
    }


def compute() -> dict:
    """由抽样标注外推检测精度指标。任何一步缺样本都返回 None，不猜。"""
    s = load_set()
    if s is None:
        return {"available": False, "reason": "no_set"}

    items = s["items"]
    strata_pop = {k: int(v.get("population") or 0) for k, v in (s.get("strata") or {}).items()}
    labels = _read_truth()

    per = {st: _stratum_stat(items, labels, st, strata_pop.get(st, 0)) for st in STRATA}

    labeled_total = sum(per[st]["labeled"] for st in STRATA)
    progress = {
        "labeled": labeled_total,
        "total": len(items),
        "by_stratum": {st: {"labeled": per[st]["labeled"], "sampled": per[st]["sampled"]} for st in STRATA},
    }

    # 帧级精确率：检出帧里真的发生了事故的比例
    det = per[DETECTED]
    frame_precision = None
    if det["rate"] is not None:
        frame_precision = {
            "value": det["rate"],
            "ci": det["ci"],
            "n": det["labeled"],
            "population": det["population"],
        }

    # 召回率需要四层都有样本——缺任何一层，那部分的漏检量就无从估计
    ready = all(per[st]["rate"] is not None for st in STRATA)
    if not ready:
        return {
            "available": True,
            "ready": False,
            "progress": progress,
            "strata": per,
            "frame_precision": frame_precision,
            "recall": None,
            "miss_rate": None,
            "estimate": None,
            "missing": [st for st in STRATA if per[st]["rate"] is None],
        }

    def extrapolate(stratum: str, rate: float) -> float:
        """比例 × **该层总体**。

        必须乘总体：四层的抽样数各不相同（16/16/16/32），
        只把抽样条数相加会让样本量大的层权重过大，漏检量随之算错。
        """
        return rate * strata_pop.get(stratum, 0)

    def totals_at(det_rate: float, miss_rates: dict[str, float]) -> tuple[float, float]:
        tp = extrapolate(DETECTED, det_rate)
        fn = sum(extrapolate(st, miss_rates[st]) for st in MISS_STRATA)
        return tp, fn

    rates = {st: per[st]["rate"] for st in STRATA}
    tp, fn = totals_at(rates[DETECTED], rates)
    recall = tp / (tp + fn) if (tp + fn) > 0 else None

    # 保守包络：让"漏检最多的那种组合"作为下界，反之作为上界。
    # 比把两个区间端点独立相加更粗糙，但方向不会错——宁可区间偏宽，不可偏窄。
    lo_rates = {st: (per[st]["ci"][0] if per[st]["ci"] else rates[st]) for st in STRATA}
    hi_rates = {st: (per[st]["ci"][1] if per[st]["ci"] else rates[st]) for st in STRATA}
    tp_lo, fn_hi = totals_at(lo_rates[DETECTED], hi_rates)
    tp_hi, fn_lo = totals_at(hi_rates[DETECTED], lo_rates)
    recall_ci = (
        (tp_lo / (tp_lo + fn_hi)) if (tp_lo + fn_hi) > 0 else None,
        (tp_hi / (tp_hi + fn_lo)) if (tp_hi + fn_lo) > 0 else None,
    )

    unchecked_pop = int(s.get("unchecked_frames") or sum(strata_pop.get(st, 0) for st in MISS_STRATA))
    miss_rate = (fn / unchecked_pop) if unchecked_pop > 0 else None
    # 漏检率同样给区间：它由三层各自的区间相加而来，取保守的两端
    fn_lo = sum(extrapolate(st, lo_rates[st]) for st in MISS_STRATA)
    fn_hi = sum(extrapolate(st, hi_rates[st]) for st in MISS_STRATA)

    return {
        "available": True,
        "ready": True,
        "progress": progress,
        "strata": per,
        "frame_precision": frame_precision,
        "recall": {
            "value": recall,
            # 注意方向：recall 下界对应 FN 上界
            "ci": (recall_ci[0], recall_ci[1]),
            "n": labeled_total,
        },
        "miss_rate": {
            "value": miss_rate,
            "ci": (
                (fn_lo / unchecked_pop) if unchecked_pop else None,
                (fn_hi / unchecked_pop) if unchecked_pop else None,
            ),
            "population": unchecked_pop,
        },
        "estimate": {
            "true_positive": tp,
            "false_negative": fn,
            "detected_frames": int(s.get("detected_frames") or 0),
            "total_frames": int((s.get("video") or {}).get("total_frames") or 0),
            # 各层外推出的漏检帧数，用来回答"漏在哪"
            "missed_by_stratum": {st: extrapolate(st, rates[st]) for st in MISS_STRATA},
        },
        "missing": [],
    }


def view() -> dict:
    """给前端的完整视图：清单 + 标注 + 指标。"""
    s = load_set()
    if s is None:
        return {
            "available": False,
            "reason": "no_set",
            "set": None,
            "labels": {},
            "metrics": {"available": False, "reason": "no_set"},
        }
    labels = _read_truth()
    return {
        "available": True,
        "set": s,
        "labels": labels,
        "metrics": compute(),
    }
