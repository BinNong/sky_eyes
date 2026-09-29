"""事故类型抽取与聚合的回归测试。

不需要 pytest 也能跑：

    /path/to/visual_search/bin/python tests/test_accident_type.py

纯字符串处理，不需要 GPU、网络或多模态服务。

为什么专门给这一块写测试：类型抽取和严重等级是**同一类东西**——
写松一点就会静默算错，不抛异常，只是安静地给出错的结果。
等级链上已经出过三次（中文 max 按码点取错、裸字符类误判、映射表缺键），
这里把同样的坑在类型上一次性堵掉，而不是等它在真实数据里显形。

守的四件事：

1. **「无法判断事故类型」不能读成任何类型**。这是裸关键词匹配的经典失效：
   一段说明「没能判断」的文字里往往含有类型词，一旦用 search 就会命中。
2. **别名表必须自洽**：`TYPES` 里的每个名字都要能查到自身。
   教训是 `pipeline._DEGREE_MAP` 漏了「高」这个键，导致模型答「高」时被当成「未判定」——
   一次纯粹的漏报，而且靠运气才没在数据里暴露。
3. **事件级必须用多数票，不能用等级那套「取最高」**。类型之间没有好坏排序。
4. **无有效帧时返回 None，不能填「其他」**。「其他」是一个**判定结果**，
   和「未判定」含义完全不同；混起来会让「其他」这一类虚高。
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from accident_type import (  # noqa: E402
    TYPES,
    TYPE_ALIASES,
    build_type_stats,
    extract_type,
    normalize_type,
    summarize_event_type,
    type_mentions,
)


# ---------- 直接短答：必须严格 ---------- #

def test_bare_type_names_all_resolve():
    """词表里的每个名字都必须能查到自身，且归一后仍是自己。"""
    for name in TYPES:
        assert name in TYPE_ALIASES, f"别名表漏了词表里的「{name}」（对应 _DEGREE_MAP 缺键那次事故）"
        assert TYPE_ALIASES[name] == name, f"「{name}」被映射成了别的类型：{TYPE_ALIASES[name]}"
        assert normalize_type(name) == name, f"短答「{name}」归一失败"


def test_whitespace_and_punctuation_tolerated():
    assert normalize_type(" 追尾 ") == "追尾"
    assert normalize_type("追尾。") == "追尾"
    assert normalize_type("「侧碰」") == "侧碰"
    assert normalize_type("翻车！") == "翻车"


def test_common_phrasings_normalize():
    assert normalize_type("事故类型：追尾") == "追尾"
    assert normalize_type("类型为侧碰") == "侧碰"
    assert normalize_type("这起追尾事故") == "追尾"
    assert normalize_type("应该是正面碰撞") == "正面碰撞"
    assert normalize_type("属于刮擦") == "刮擦"
    assert normalize_type("答案是翻车。") == "翻车"


def test_cannot_judge_returns_none_not_a_type():
    """核心防线：说「无法判断」的时候，绝不能给出一个类型。

    这几条正是裸关键词匹配会翻车的地方——文字里含有类型词，但结论是「不知道」。
    """
    for text in (
        "无法判断事故类型",
        "无法判断",
        "无法确定事故类型",
        "暂时无法判断事故类型",
        "不能确定",
        "不确定",
        "没有",
        "看不到明显的碰撞，无法判断类型",
        "图中没有车辆",
    ):
        assert normalize_type(text) is None, f"「{text}」不该被判成类型，却得到 {normalize_type(text)!r}"


def test_severity_only_text_is_not_a_type():
    """只说严重程度、没说类型的回答不能凭空变成类型。"""
    for text in (
        "这是一起严重的事故",
        "事故很严重",
        "事故严重等级：高",
        "这是一起交通事故的现场",
        "车辆受损严重",
    ):
        assert normalize_type(text) is None, f"「{text}」不该被判成类型，却得到 {normalize_type(text)!r}"


def test_negated_type_is_not_positive():
    """否定式回答不能被当成肯定。"""
    for text in ("不是追尾", "并非侧碰", "不属于刮擦", "没有翻车"):
        assert normalize_type(text) is None, f"「{text}」是否定式，不该判成类型，却得到 {normalize_type(text)!r}"


# ---------- 主答：只认带标记的判定 ---------- #

def test_marked_type_in_description():
    assert extract_type("从图中看，车尾受到明显撞击。事故类型：追尾。") == "追尾"
    assert extract_type("侧面有明显凹陷，类型为侧碰。") == "侧碰"
    assert type_mentions("类型：追尾，依据是车尾变形") == ["追尾"]


def test_description_without_marker_yields_nothing():
    """只描述受损部位、不给类型的回答，必须留空让上层去追问，而不是从描述里猜。"""
    for text in (
        "车尾受到明显撞击，后备箱严重变形。",
        "这是一起交通事故的现场画面，可以看到车辆受损。",
        "车头局部凹进去一块，旁边有散落物。",
    ):
        assert extract_type(text) is None, f"「{text}」没有类型标记，不该抽出类型"


def test_marked_but_unresolvable_is_none():
    """标记后面跟的不是已知类型时，返回 None，不硬套一个。"""
    for text in (
        "事故类型：追尾或者侧碰",
        "事故类型：无法判断",
        "事故类型：不明",
        "事故类型：碰撞",
    ):
        assert extract_type(text) is None, f"「{text}」类型不可解析，不该给出结果"


def test_last_marked_mention_wins():
    """同段多次给出类型时取最后一次（模型的最终结论），与等级链保持一致。"""
    assert extract_type("一开始像是刮擦，事故类型：刮擦。再仔细看车尾凹陷，类型为追尾。") == "追尾"


def test_marker_does_not_fire_on_question_echo():
    """问题被复读时不该误抽。

    TYPE_QUESTION 里写了「请根据车辆受损部位判断事故类型：车尾受损多见于追尾……」，
    模型若原样复读，「事故类型：」后面跟着的是说明文字而非类型名，必须解析失败。
    """
    echo = (
        "这是一起交通事故中受损车辆的局部画面。请根据车辆受损部位判断事故类型："
        "车尾受损多见于追尾，车头受损多见于正面碰撞。"
    )
    # 复读里「事故类型：」后面是「车尾受损多见于追尾」，不是类型名
    assert extract_type(echo) is None, f"问题复读不该抽出类型，却得到 {extract_type(echo)!r}"


# ---------- 事件级聚合：多数票，不是取最高 ---------- #

def test_event_type_majority_vote():
    event = {
        "observations": [
            {"accident_type": "追尾", "confidence": 0.9, "frame_index": 1},
            {"accident_type": "追尾", "confidence": 0.5, "frame_index": 2},
            {"accident_type": "刮擦", "confidence": 0.99, "frame_index": 3},
        ]
    }
    out = summarize_event_type(event)
    # 追尾 2 票 > 刮擦 1 票，即便刮擦那帧置信度最高
    assert out["event_type"] == "追尾", "多数票必须胜过单帧高置信度"
    assert out["event_type_agreement"] is False
    assert out["event_type_frames"] == ["追尾", "追尾", "刮擦"]


def test_event_type_tie_breaks_by_confidence():
    event = {
        "observations": [
            {"accident_type": "侧碰", "confidence": 0.41, "frame_index": 11},
            {"accident_type": "翻车", "confidence": 0.88, "frame_index": 12},
        ]
    }
    out = summarize_event_type(event)
    assert out["event_type"] == "翻车", "平票时取置信度最高的那一帧"
    assert out["event_type_frame"] == 12
    assert out["event_type_agreement"] is False


def test_event_type_ignores_unresolved_frames():
    """未判定的帧不参与投票，也不该把结果拉成 None。"""
    event = {
        "observations": [
            {"accident_type": None, "confidence": 0.99, "frame_index": 1},
            {"accident_type": "刮擦", "confidence": 0.3, "frame_index": 2},
        ]
    }
    out = summarize_event_type(event)
    assert out["event_type"] == "刮擦"
    assert out["event_type_agreement"] is True  # 已判定的只有一帧，自然一致


def test_event_type_all_unresolved_is_none_not_other():
    """全部未判定时必须返回 None——「其他」是一个判定结果，和「未判定」含义不同。"""
    for obs in (
        [],
        [{"accident_type": None, "confidence": 0.9, "frame_index": 1}],
        [{"accident_type": None, "frame_index": 1}, {"accident_type": None, "frame_index": 2}],
    ):
        out = summarize_event_type({"observations": obs})
        assert out["event_type"] is None, f"{obs} 应返回 None，却得到 {out['event_type']!r}"
        assert out["event_type"] != "其他"


def test_event_type_without_confidence_field_does_not_crash():
    """confidence 缺失时平票裁决不能崩——真实数据里字段可能不全。"""
    event = {
        "observations": [
            {"accident_type": "追尾", "frame_index": 1},
            {"accident_type": "侧碰", "frame_index": 2},
        ]
    }
    out = summarize_event_type(event)
    assert out["event_type"] in {"追尾", "侧碰"}


# ---------- 统计 ---------- #

def test_stats_distributions_and_rule_string():
    events = [
        {
            "event_type": "追尾",
            "event_type_frames": ["追尾", "追尾"],
            "event_type_agreement": True,
            "observations": [
                {"accident_type": "追尾", "type_source": "primary"},
                {"accident_type": "追尾", "type_source": "followup"},
            ],
        },
        {
            "event_type": None,
            "event_type_frames": [],
            "event_type_agreement": False,
            "observations": [{"accident_type": None, "type_source": None}],
        },
    ]
    s = build_type_stats(events)
    assert s["type_distribution"]["追尾"] == 2
    assert s["type_distribution"]["未判定"] == 1
    assert s["event_type_distribution"]["追尾"] == 1
    assert s["event_type_distribution"]["未判定"] == 1
    assert s["type_unresolved"] == 1
    assert s["type_from_followup"] == 1
    assert s["type_disagreement"] == 0
    # 词表随产物下发，前端不该自己再写一份
    assert s["type_vocabulary"] == list(TYPES)
    # 聚合规则必须显式写出来，前端读取而不猜（等级链的教训）
    assert "majority" in s["event_type_rule"]


def test_stats_count_disagreement_events():
    events = [
        {
            "event_type": "追尾",
            "event_type_frames": ["追尾", "刮擦"],
            "event_type_agreement": False,
            "observations": [{"accident_type": "追尾"}, {"accident_type": "刮擦"}],
        }
    ]
    s = build_type_stats(events)
    assert s["type_disagreement"] == 1, "代表帧判出不同类型的事件必须被计数"


def test_stats_empty_events_do_not_crash():
    s = build_type_stats([])
    assert s["type_unresolved"] == 0
    assert s["type_disagreement"] == 0
    assert all(v == 0 for k, v in s["event_type_distribution"].items())


def test_stats_type_status_distinguishes_not_run_from_all_unresolved():
    """「没跑过分类」和「跑了但全未判定」必须在产物里长得不一样。

    前者不该在报告里出现任何类型栏目；后者应显示「未判定 N 帧」并提示人工复核。
    混起来会让读者以为分类跑了却全军覆没，或者反过来以为数据里真有很多「未判定」。
    """
    # 没有任何 accident_type 键 = 压根没跑过分类
    not_run = build_type_stats(
        [{"observations": [{"severity": "低"}], "event_type": None, "event_type_frames": []}]
    )
    assert not_run["type_status"] == "not_run", "没跑过分类时不能报成 done"

    # 键存在但值为 None = 跑过了、只是没判出来
    ran = build_type_stats(
        [{"observations": [{"accident_type": None}], "event_type": None, "event_type_frames": []}]
    )
    assert ran["type_status"] == "done", "跑过但全未判定时不能报成 not_run"
    assert ran["type_unresolved"] == 1


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
