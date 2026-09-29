"""严重等级抽取的回归测试。

不需要 pytest 也能跑：

    /path/to/visual_search/bin/python tests/test_severity.py

这些函数是纯字符串处理，不需要 GPU、不需要网络、不需要多模态服务。

为什么专门给这一块写测试：等级抽取这条链**已经出过两次真问题**——

1. 事件级聚合用了 `max()` 直接比中文串。按 Unicode 码点是 `中`(U+4E2D) < `低`(U+4F4E) < `高`(U+9AD8)，
   于是 `max("低", "中")` 返回的是 **"低"**，事件等级取反了。
2. 追问答案用裸字符类 `[低中高重]` 匹配，而「严重」里就含「重」，
   于是「无法判断严重程度」被判定成 **「高」**。

两次都是「静默算错、不抛异常」的类型——不会崩溃，只会安静地给出错的结果。
这类 bug 只能靠用例守住，靠人眼看代码看不出来。
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (  # noqa: E402
    _extract_severity,
    _severity_from_short_answer,
    _severity_mentions,
    _summarize_event_severity,
)


# ---------- 追问短答：必须严格 ---------- #

def test_short_answer_accepts_bare_level():
    assert _severity_from_short_answer("低") == "低"
    assert _severity_from_short_answer("中") == "中"
    assert _severity_from_short_answer("高") == "高"
    assert _severity_from_short_answer(" 低 ") == "低"
    assert _severity_from_short_answer("低。") == "低"
    assert _severity_from_short_answer("中档") == "中"
    assert _severity_from_short_answer("轻度") == "低"
    assert _severity_from_short_answer("是：高") == "高"
    assert _severity_from_short_answer("严重等级：中") == "中"


def test_short_answer_rejects_text_containing_yanzhong():
    """核心回归：含「严重」的非答案，绝不能被判成「高」。

    「严重」里含有「重」，旧的裸字符类写法会把它当成等级。
    「高」对应「立即派警 + 通知急救」，误判代价很高。
    """
    assert _severity_from_short_answer("无法判断严重程度") is None
    assert _severity_from_short_answer("这是一起严重的事故") is None
    assert _severity_from_short_answer("严重程度无法确定") is None


def test_short_answer_rejects_other_non_answers():
    assert _severity_from_short_answer("图片模糊，无法确定") is None
    assert _severity_from_short_answer("不确定") is None
    assert _severity_from_short_answer("") is None
    assert _severity_from_short_answer(None) is None


def test_short_answer_rejects_option_echo():
    """模型复读选项时要判不出，而不是顺手取第一个。"""
    assert _severity_from_short_answer("低 或 中 或 高") is None
    assert _severity_from_short_answer("低/中/高") is None


def test_short_answer_normalizes_zhong():
    """「重度」要归一成「高」。"""
    assert _severity_from_short_answer("重度") == "高"


# ---------- 主问题：从描述里抽取 ---------- #

def test_mentions_from_main_answer():
    assert _severity_mentions("事故严重等级为高") == ["高"]
    assert _severity_mentions("初步判断为中度") == ["中"]
    assert _severity_mentions("严重程度是低") == ["低"]
    assert _severity_mentions("重度") == ["高"]
    assert _severity_mentions("这段描述没有提到任何等级") == []
    assert _severity_mentions(None) == []


def test_extract_takes_last_mention():
    """模型重复退化时会先后给出多个等级，取最后一次判定。"""
    assert _extract_severity("等级为低……等级为中……等级为高") == "高"
    assert _extract_severity("等级为高……等级为低") == "低"
    assert _extract_severity("没有等级") is None


# ---------- 事件级聚合：取最高，且不能按 Unicode 码点比 ---------- #

def test_event_severity_takes_max_not_unicode_order():
    """核心回归：`max("低", "中")` 按码点会返回「低」。"""
    event = {
        "observations": [
            {"severity": "低", "frame_index": 101},
            {"severity": "中", "frame_index": 102},
        ]
    }
    got = _summarize_event_severity(event)
    assert got["event_severity"] == "中", (
        f"应取「中」，实际拿到「{got['event_severity']}」——中文 max 按 Unicode 码点取错了"
    )
    assert got["event_severity_frame"] == 102
    assert got["event_severity_levels"] == ["低", "中"]


def test_event_severity_max_across_all_three():
    event = {
        "observations": [
            {"severity": "中", "frame_index": 1},
            {"severity": "高", "frame_index": 2},
            {"severity": "低", "frame_index": 3},
        ]
    }
    assert _summarize_event_severity(event)["event_severity"] == "高"


def test_event_severity_handles_missing_and_single():
    empty = _summarize_event_severity({"observations": [{"severity": None, "frame_index": 1}]})
    assert empty["event_severity"] is None
    assert empty["event_severity_frame"] is None
    assert empty["event_severity_levels"] == []

    single = _summarize_event_severity({"observations": [{"severity": "低", "frame_index": 7}]})
    assert single["event_severity"] == "低"
    assert single["event_severity_frame"] == 7


# ---------- 排序表只能有一份 ---------- #

def test_severity_order_table_matches_alerting_module():
    """等级排序表必须与告警模块用的是同一张。

    pipeline 里的 `_SEVERITY_ORDER` 是从 `alerting` 导入的（不是各写一份）。
    这条用例守的是"有人图省事又在 pipeline 里写了个字面量"的情况——
    一旦两份表不一致，就会出现「报告算出高等级、告警却因为阈值表不同而没推」，
    正好落在本文件顶部那三次静默失效的同一条链上，而且它同样不会抛异常。
    """
    import pipeline
    from alerting import SEVERITY_ORDER

    assert pipeline._SEVERITY_ORDER == {"低": 0, "中": 1, "高": 2}
    assert pipeline._SEVERITY_ORDER is SEVERITY_ORDER, "应当就是同一个对象，而不是内容相同的另一份"


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
    print()
    print(f"{len(fns) - failed}/{len(fns)} 通过")
    raise SystemExit(1 if failed else 0)
