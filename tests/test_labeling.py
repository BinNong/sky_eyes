"""检测召回率的抽样标注与指标 —— 回归测试。

跑法（用后端自己的 venv）：`server/.venv/bin/python tests/test_labeling.py`

不需要视频、不需要 GPU、不需要真跑分析：抽样清单是构造出来的。

为什么专门给这块写测试

这一模块做的事是**用 80 帧的抽样，去估计 959 帧的召回率**。
抽样估计的每一步都可能安静地算错，而且错的数字看起来照样像个数字：

1. 没有标注时返回 0 —— 把"还没标"说成"漏检 0%"，正是最危险的方向
2. 把 boundary / inside 的漏检率当成全视频漏检率 —— 那两层是**有意偏置**的，
   专挑最可能漏检的位置，混起来会得出"漏检 60%"这种吓人的假结论
3. 召回率的分母用错 —— 它应该是「检出真阳 + 漏检」，而不是全部帧。
   用全部帧当分母会把召回率系统性压低（因为视频里绝大多数帧本来就没事故）
4. 外推时用抽样的**条数**而不是**比例 × 该层总体** —— 各层抽样数不同（16/16/16/32），
   不乘总体就会让样本量大的层权重过大
5. Wilson 区间在 p=0/p=1 时退化成零宽 —— "16 帧全对"会被说成"精确率恰好 100%，无误差"
6. 清单与答案按 id 对齐，错位会让整个指标指向错误的帧
"""

import json
import math
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# 必须在 import labeling 之前设环境变量——路径常量是模块级求值的
_TMP = tempfile.mkdtemp(prefix="skyeyes-labeling-test-")
os.environ["SKYEYES_LABEL_DIR"] = _TMP
os.environ["SKYEYES_LABEL_SET"] = str(Path(_TMP) / "set.json")
os.environ["SKYEYES_LABEL_TRUTH"] = str(Path(_TMP) / "truth")

import labeling as L  # noqa: E402

SET_FILE = Path(os.environ["SKYEYES_LABEL_SET"])
# 四层的总体刻意取成不一样的量级，这样"忘了乘总体"一定会被下面的数值断言抓到
POPS = {"detected": 100, "inside": 10, "boundary": 20, "far": 200}
PER_LAYER = 4


def make_set(per_layer: int = PER_LAYER, pops: dict | None = None) -> None:
    pops = pops or POPS
    items = []
    for st in L.STRATA:
        for i in range(per_layer):
            items.append(
                {
                    "id": f"{st}-{i}",
                    "frame_index": 100 + len(items),
                    "time_sec": round(len(items) / 25, 3),
                    "stratum": st,
                    "detected": st == L.DETECTED,
                    "confidence": 0.5 if st == L.DETECTED else None,
                    "image": f"labeling/frames/{st}-{i}.jpg",
                }
            )
    SET_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "seed": 1,
                "video": {"src": "x.mp4", "total_frames": 500, "fps": 25, "width": 1280, "height": 720},
                "strata": {st: {"sampled": per_layer, "population": pops[st]} for st in L.STRATA},
                "detected_frames": pops[L.DETECTED],
                "unchecked_frames": sum(pops[s] for s in L.MISS_STRATA),
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def reset(per_layer: int = PER_LAYER, pops: dict | None = None) -> None:
    shutil.rmtree(_TMP, ignore_errors=True)
    Path(_TMP).mkdir(parents=True, exist_ok=True)
    L.clear_labels()
    make_set(per_layer, pops)


def label(stratum: str, n_accident: int, per_layer: int = PER_LAYER) -> None:
    """把某层的前 n 条标为「有事故」，其余标为「无事故」。"""
    for i in range(per_layer):
        L.put_label(f"{stratum}-{i}", L.ACCIDENT if i < n_accident else L.NONE)


def label_all(det=3, ins=4, bnd=2, far=0, per_layer: int = PER_LAYER) -> None:
    label(L.DETECTED, det, per_layer)
    label("inside", ins, per_layer)
    label("boundary", bnd, per_layer)
    label("far", far, per_layer)


# ───────────────────── 1. 没有标注时不许给数字 ─────────────────────


def test_no_labels_returns_none():
    """一次都没标时，指标必须是 None。

    返回 0 会被读成"漏检 0%、召回率 100%"——这是最危险的方向：
    它让一个从未验证过的系统看起来完美。
    """
    reset()
    m = L.compute()
    assert m["available"] is True
    assert m["ready"] is False
    assert m["frame_precision"] is None
    assert m["recall"] is None
    assert m["miss_rate"] is None
    assert m["progress"]["labeled"] == 0
    assert set(m["missing"]) == set(L.STRATA)


def test_no_set_file_is_not_an_error():
    """没跑过抽样脚本时要老实说"没有清单"，而不是崩或编一个空指标。"""
    shutil.rmtree(_TMP, ignore_errors=True)
    Path(_TMP).mkdir(parents=True, exist_ok=True)
    m = L.compute()
    assert m["available"] is False and m["reason"] == "no_set"
    v = L.view()
    assert v["available"] is False and v["set"] is None
    reset()


def test_partial_labels_give_partial_metrics():
    """只标了 detected 层时：帧级精确率可算，召回率必须仍为 None。"""
    reset()
    label(L.DETECTED, 3)
    m = L.compute()
    assert m["ready"] is False
    assert m["frame_precision"]["value"] == 0.75
    assert m["recall"] is None, "缺层时不能给召回率"
    assert set(m["missing"]) == set(L.MISS_STRATA)


# ───────────────────── 2. 外推口径 ─────────────────────


def test_extrapolation_multiplies_by_population():
    """外推必须用「比例 × 该层总体」，不是抽样条数。

    四层总体刻意差两个量级（100 / 10 / 20 / 200），抽样数却相同（各 4 条）。
    忘了乘总体的话，far 层 200 帧的权重会被压成 4，漏检量严重低估。
    """
    reset()
    label_all(det=3, ins=4, bnd=2, far=0)
    m = L.compute()
    est = m["estimate"]

    assert m["ready"] is True
    assert m["frame_precision"]["value"] == 0.75
    assert math.isclose(est["true_positive"], 0.75 * 100), est
    assert math.isclose(est["missed_by_stratum"]["inside"], 1.0 * 10)
    assert math.isclose(est["missed_by_stratum"]["boundary"], 0.5 * 20)
    assert math.isclose(est["missed_by_stratum"]["far"], 0.0 * 200)
    assert math.isclose(est["false_negative"], 10 + 10 + 0)

    # 召回率 = TP / (TP + FN)
    expect = (0.75 * 100) / (0.75 * 100 + 20)
    assert math.isclose(m["recall"]["value"], expect), (m["recall"]["value"], expect)

    # 漏检率的分母是「未检出帧总数」= 10+20+200
    assert math.isclose(m["miss_rate"]["value"], 20 / 230)
    assert m["miss_rate"]["population"] == 230


def test_recall_denominator_is_not_total_frames():
    """召回率的分母必须是「检出真阳 + 漏检」，不能是全部帧。

    视频里绝大多数帧本来就没事故，用全部帧当分母会把召回率压到很低，
    而且压多少取决于视频有多长——完全是错的。
    """
    reset()
    label_all(det=4, ins=0, bnd=0, far=0)
    m = L.compute()
    assert math.isclose(m["recall"]["value"], 1.0), m["recall"]
    assert m["estimate"]["false_negative"] == 0


def test_biased_layers_do_not_become_the_overall_rate():
    """inside / boundary 的漏检率**不能**当成全视频的漏检率。

    构造：inside 与 boundary 全判为事故（漏检率 100%），far 全判无事故。
    inside+boundary 强偏置层的"漏检率"是 100%，但全视频的漏检率
    必须被 far 层的 200 帧稀释下来——否则会得出"漏检 100%"这种假结论。
    """
    reset()
    label_all(det=4, ins=4, bnd=4, far=0)
    m = L.compute()
    per = m["strata"]
    assert per["inside"]["rate"] == 1.0
    assert per["boundary"]["rate"] == 1.0
    # 漏检总量只来自偏置层：10 + 20 = 30，除以未检出总数 230
    assert math.isclose(m["miss_rate"]["value"], 30 / 230)
    assert m["miss_rate"]["value"] < 0.5, "偏置层把整体漏检率带飞了"


# ───────────────────── 3. 不确定性 ─────────────────────


def test_wilson_never_zero_width_at_boundaries():
    """p=0 / p=1 时区间必须有宽度。

    这是选用 Wilson 而不是正态近似的全部理由：抽样 16 帧全判对，
    正态近似会给出 [1.0, 1.0]，等于宣称"精确率恰好 100%、毫无误差"。
    """
    lo, hi = L.wilson(0, 16)
    assert lo == 0.0 and hi > 0.1, (lo, hi)
    lo, hi = L.wilson(16, 16)
    assert hi == 1.0 and lo < 0.9, (lo, hi)

    lo, hi = L.wilson(0, 32)
    assert hi < 0.12, "样本量翻倍，上界应变窄"


def test_wilson_degenerate():
    assert L.wilson(0, 0) is None


def test_wilson_brackets_point_estimate():
    for s, n in [(3, 16), (8, 16), (15, 16), (1, 32)]:
        lo, hi = L.wilson(s, n)
        p = s / n
        assert lo <= p <= hi, (s, n, lo, hi)


def test_recall_ci_ordered_and_contains_estimate():
    """召回率区间必须包含点估计，且方向不能反。

    反了的话界面会显示"[0.98, 0.62]"这种自相矛盾的东西——
    而且它看起来依然像个区间，不仔细看发现不了。
    """
    reset()
    label_all(det=3, ins=2, bnd=1, far=0)
    m = L.compute()
    lo, hi = m["recall"]["ci"]
    v = m["recall"]["value"]
    assert lo <= v <= hi, (lo, v, hi)
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0


# ───────────────────── 4. 写入校验 ─────────────────────


def test_resubmit_overwrites():
    reset()
    L.put_label(f"{L.DETECTED}-0", L.ACCIDENT)
    L.put_label(f"{L.DETECTED}-0", L.NONE)
    m = L.compute()
    assert m["progress"]["by_stratum"][L.DETECTED]["labeled"] == 1
    assert m["strata"][L.DETECTED]["accident"] == 0


def test_reject_unknown_id():
    """清单里没有的 id 必须拒绝。

    放过去的话会写成一条永远不会被任何层统计到的孤立记录——
    标注者以为标了，指标却没变，而且没有任何提示。属于静默丢失。
    """
    reset()
    try:
        L.put_label("does-not-exist", L.ACCIDENT)
    except ValueError:
        return
    raise AssertionError("未知 id 被接受了")


def test_reject_unknown_truth_value():
    reset()
    for bad in ("yes", "no", "", "事故"):
        try:
            L.put_label(f"{L.DETECTED}-0", bad)
        except ValueError:
            continue
        raise AssertionError(f"非法标注值 {bad!r} 被接受了")


def test_clear_single_and_all():
    reset()
    label_all()
    assert L.compute()["progress"]["labeled"] == PER_LAYER * len(L.STRATA)

    assert L.clear_labels(f"{L.DETECTED}-0") == 1
    assert L.compute()["progress"]["labeled"] == PER_LAYER * len(L.STRATA) - 1

    assert L.clear_labels("nonexistent") == 0

    assert L.clear_labels() == PER_LAYER * len(L.STRATA) - 1
    assert L.compute()["progress"]["labeled"] == 0


# ───────────────────── 5. 落盘与健壮性 ─────────────────────


def test_persist_roundtrip():
    """标注是人工成本，重启后必须还在。"""
    reset()
    label_all(det=3, ins=4, bnd=2, far=0)
    before = L.compute()["recall"]["value"]

    # 换一个进程语义：模块内部无缓存，直接重读文件即可
    labels = L._read_truth()
    assert len(labels) == PER_LAYER * len(L.STRATA)
    assert math.isclose(L.compute()["recall"]["value"], before)


def test_corrupt_truth_degrades_to_empty():
    reset()
    label_all()
    Path(os.environ["SKYEYES_LABEL_TRUTH"], "truth.json").write_text(
        '{"labels": {"det', encoding="utf-8"
    )
    assert L._read_truth() == {}
    assert L.compute()["ready"] is False


def test_no_leftover_tmp_file():
    reset()
    L.put_label(f"{L.DETECTED}-0", L.ACCIDENT)
    leftovers = [
        p.name for p in Path(os.environ["SKYEYES_LABEL_TRUTH"]).iterdir() if p.name.endswith(".tmp")
    ]
    assert not leftovers, leftovers


def test_sampled_more_than_population_is_handled():
    """抽样数超过总体时（例如 inside 只有 3 帧却要求抽 16），
    指标仍应按实际的样本量算，不能虚报 n。"""
    reset(per_layer=3, pops={"detected": 100, "inside": 3, "boundary": 20, "far": 200})
    label_all(det=3, ins=3, bnd=1, far=0, per_layer=3)
    m = L.compute()
    assert m["strata"]["inside"]["sampled"] == 3
    assert m["strata"]["inside"]["labeled"] == 3
    # inside 总体只有 3 帧，全判事故 -> 漏检量就是 3，不能按 16 条抽样去乘
    assert math.isclose(m["estimate"]["missed_by_stratum"]["inside"], 3.0)


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
