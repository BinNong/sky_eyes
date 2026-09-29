"""事故类型分类：类型词表、提问、抽取、事件级聚合。

════════════════════════════════════════════════════════════════════════
⚠ 当前状态：**实验性，未采用**（2026-09-21 实测后停用）
════════════════════════════════════════════════════════════════════════

抽取逻辑本身是好的（19 条回归测试全过），但**提问这一环对 1.3B 模型不可行**。
实测记录如下，请不要再照原样重跑一遍：

在 18 个代表帧上跑模板式提问（`请描述…同时给出事故类型（追尾，侧碰，…）`）：

- **18/18 都判成「侧碰」**，一个恒定值，信息量为零。
- 其中帧 803 的画面是**一辆倒地的摩托车**，模型自己的描述也写着
  「摩托车和骑手都侧向翻倒在地」——但它给出的类型仍是「侧碰」。它在叙事，不在观察。
- 帧 443 是**一辆完好的白色两厢车**（车顶可见、无明显损伤），同样判成「侧碰」。

另外测过三种替代问法，都不行：

| 问法 | 结果 |
| --- | --- |
| 列出 6 个选项（模板句） | 18/18 「侧碰」 |
| 列出 7 个选项、要求只回一个词 | 4/4 「侧碰」——**恰好是第一项**（后经排列实验确认为选项表主导，见下） |
| 不给选项、只要求回一个词 | `FWD` / `Taxi` / `Accident` ——纯噪声 |
| 问「哪个部位受损」 | 4/4 干净短答（车头/车顶/车门），但答的是**部位**不是类型 |

**根因：已用排列实验确认——是选项列表在主导答案，模型没有在为图像作答。**

把「侧碰」放在列表的第 2 / 第 5 / 第 6 位，另加一组**完全不含「侧碰」**的选项，
同样 4 帧重跑（2026-09-21，远端恢复后补做，脚本 `/tmp/probe_perm.py` 的等价逻辑见下）：

| 选项顺序 | 帧 83 | 帧 600 | 帧 803 | 帧 553 |
| --- | --- | --- | --- | --- |
| A「侧碰」在第 2 位 | 侧碰 | 侧碰 | 侧碰 | — |
| B「翻车」在第 1 位 | **翻车** | **翻车** | — | — |
| C「刮擦」在第 1 位 | **刮擦** | — | — | — |
| D 不含「侧碰」（「追尾」在第 1 位） | **追尾** | **追尾** | **翻车** | — |
| E 只给两项（追尾 / 侧碰） | 侧碰 | 侧碰 | 侧碰 | 侧碰 |

**同一张图，只改选项顺序，就从「侧碰」变成「翻车」「刮擦」「追尾」。**
判据不必是「答案恰好落在某个固定位置」——**只要同一帧在不同顺序下答案不同，
就说明答案是问题（选项表）的函数，而不是图像的函数**。（注意 A 组答第 2 项、
B/C/D 组答第 1 项、E 组 4/4 答第 2 项：连"固定第几位"都不稳定，是更彻底的不可靠。）

另一条假设（**输入本身不含判据**）也成立且独立：ROI 是车辆紧致裁剪、多为俯拍侧面视角，
常常看不出撞击在车头还是车尾——已通过直接查看帧 365 / 443 / 803 的裁剪图确认。
两条叠加的效果是：**换问法救不了，得换输入 + 换更强的模型。**

**结论：这个维度在 1.3B 模型 + 车辆 ROI 输入上不可做。** 一个恒定值配 9 起事件，
客户一句「为什么全是侧碰」就会击穿整个 demo 的可信度——比"没有这个功能"伤害大得多。

**将来若要做，前置条件（缺一不可）**：
1. 更宽的输入——事故框外扩约 60% 的上下文裁剪，或整帧并放大（要能看到相对位置与路面痕迹）
2. 更强的多模态模型（1.3B 量级在开放式分类上不足以产生图像条件化的答案）
3. **与人工复核绑定**（复用 P8 的 `feedback.py` 机制），且上线前必须重跑上面的排列实验，
   确认「同一帧换选项顺序答案稳定」——这是唯一的准入判据。

════════════════════════════════════════════════════════════════════════

为什么抽取部分仍然保留：类型抽取和严重等级是同一类东西——**写松一点就会静默算错**。
它不抛异常，只是安静地把「无法判断事故类型」读成某一类，或者把「正面碰撞」读成「撞固定物」。
所以这里沿用等级链上那套已经被验证过的防御：

1. 抽取必须命中**显式标记**（「事故类型：追尾」）或**整串就是类型名**（「追尾」），
   绝不做裸关键词 search——那正是把「无法判断严重程度」读成「高」的成因。
2. 认不出来就返回 None（未判定），交人工复核，**不猜**。
3. 主答抽不到就**追问一次「只回一个词」**——模型在自由叙述里经常不给结论。
4. 事件级聚合规则显式写进产物（`event_type_rule`），前端读取而不猜。

⚠ 与严重等级最关键的一处差异：**类型没有好坏排序**，所以事件级不能取「最高」，
   只能用多数票。这是刻意的——别再按等级的思路去「取最严重的那个类型」。
"""

from __future__ import annotations

import re
from collections import Counter

# 规范类型。前端 i18n key 形如 accidentType.追尾，直接以中文为 key。
TYPES: tuple[str, ...] = ("追尾", "侧碰", "刮擦", "正面碰撞", "翻车", "撞固定物", "其他")

# 别名 → 规范类型。
#
# ⚠ 这张表**必须是自洽的**：每个 TYPES 里的名字都要能在表里查到自身。
#   教训来自 pipeline._DEGREE_MAP —— 那张表漏了「高」这个键，而取值用的是
#   `.get(raw)` 无默认值，于是模型回答「高」时拿到 None，被当成「未判定」。
#   那是一次纯粹的**漏报高等级**，且因为样本里恰好全是「低」而没被暴露。
#   tests/test_accident_type.py 里有专门守这条的用例。
TYPE_ALIASES: dict[str, str] = {
    # 追尾
    "追尾": "追尾",
    "追尾碰撞": "追尾",
    "后方追尾": "追尾",
    "被追尾": "追尾",
    "后车追尾": "追尾",
    "尾随碰撞": "追尾",
    "追尾事故": "追尾",
    # 侧碰
    "侧碰": "侧碰",
    "侧面碰撞": "侧碰",
    "侧撞": "侧碰",
    "侧面相撞": "侧碰",
    "侧面撞击": "侧碰",
    "侧向碰撞": "侧碰",
    # 刮擦
    "刮擦": "刮擦",
    "剐蹭": "刮擦",
    "刮蹭": "刮擦",
    "擦碰": "刮擦",
    "轻微刮擦": "刮擦",
    "侧面刮擦": "刮擦",
    "刮擦碰撞": "刮擦",
    # 正面碰撞
    "正面碰撞": "正面碰撞",
    "迎面碰撞": "正面碰撞",
    "正面相撞": "正面碰撞",
    "对向碰撞": "正面碰撞",
    "正面撞击": "正面碰撞",
    "车头碰撞": "正面碰撞",
    # 翻车
    "翻车": "翻车",
    "侧翻": "翻车",
    "车辆侧翻": "翻车",
    "翻滚": "翻车",
    "翻覆": "翻车",
    # 撞固定物
    "撞固定物": "撞固定物",
    "撞击固定物": "撞固定物",
    "撞护栏": "撞固定物",
    "撞墙": "撞固定物",
    "撞树": "撞固定物",
    "撞电线杆": "撞固定物",
    "撞隔离带": "撞固定物",
    # 其他
    "其他": "其他",
    "其它": "其他",
    "其他类型": "其他",
    "无法归类": "其他",
}

# 主问题：**只描述**，不要求格式。自由叙述里模型更愿意给出判断依据，
# 结论文本由 TYPE_MARKED_RE 去抓带标记的那句（等级链用的就是这个套路）。
TYPE_QUESTION = (
    "这是一起交通事故中受损车辆的局部画面。请根据车辆**受损部位**判断事故类型："
    "车尾受损多见于追尾，车头受损多见于正面碰撞，侧面凹陷多见于侧碰，"
    "侧面条状划痕多见于刮擦，车顶或整车变形多见于翻车，车头局部严重变形可能是撞固定物。"
    "请用一句话说明你看到的受损部位和判断依据。"
)

# 追问：让模型只回一个词，避免它含糊其辞。措辞与等级追问保持一致的风格。
TYPE_FOLLOWUP_QUESTION = (
    "综合图中车辆的受损部位，这起事故属于哪种类型？"
    "请只回答一个词：追尾、侧碰、刮擦、正面碰撞、翻车、撞固定物、其他。"
)

# 主答里的**带标记**类型判定：「事故类型：追尾」「类型为侧碰」。
# 必须有「类型/类别/形态/方式」这类标记词，不允许裸关键词命中。
TYPE_MARKED_RE = re.compile(
    r"(?:事故)?(?:类型|类别|形态|方式)\s*(?:为|是|：|:)?\s*"
    r"([^\s，。；、,.;:!?！？（）()\[\]【】]{1,10})"
)

# 短答归一化时剥掉的噪音。顺序无关，会反复剥到不再变化。
_NOISE = " \t\r\n「」『』\"'“”‘’《》()（）[]【】:：、,，.。!！?？-—~"
_PREFIXES = (
    "事故类型",
    "事故类别",
    "类型",
    "类别",
    "形态",
    "这起事故属于",
    "这起事故是",
    "这属于",
    "属于",
    "应该是",
    "应该属于",
    "判定为",
    "判断为",
    "答案是",
    "答案",
    "我认为是",
    "我认为",
    "这是",
    "这起",
    "一起",
    "是",
    "为",
)
_SUFFIXES = (
    "事故类型",
    "事故类别",
    "的类型",
    "类型",
    "类别",
    "事故",
    "车祸",
    "现场画面",
)


def normalize_type(text: str | None) -> str | None:
    """把模型的短答归一成规范类型；认不出来返回 None。

    「认不出来就 None」是刻意的：宁可显示「未判定」并让界面提示去人工复核，
    也不能用一个宽松匹配去猜——猜错的类型会让「哪类事故高发」这个结论整体失真。
    """
    if not text:
        return None
    out = text.strip().strip(_NOISE)
    for _ in range(4):  # 最多剥 4 轮，足以处理「事故类型为追尾事故」这类叠加
        before = out
        for p in _PREFIXES:
            if out.startswith(p) and len(out) > len(p):
                out = out[len(p) :].strip().strip(_NOISE)
        for s in _SUFFIXES:
            if out.endswith(s) and len(out) > len(s):
                out = out[: -len(s)].strip().strip(_NOISE)
        if out == before:
            break
    return TYPE_ALIASES.get(out)


def type_mentions(text: str | None) -> list[str]:
    """列出主答里所有带显式标记的类型判定（按出现顺序）。

    只认「类型：X」这种有标记的写法。模型如果通篇在描述受损部位却没给类型，
    这里就返回空列表 —— 上层会去追问一次，而不是从描述里猜。
    """
    if not text:
        return []
    out: list[str] = []
    for match in TYPE_MARKED_RE.finditer(text):
        resolved = normalize_type(match.group(1))
        if resolved:
            out.append(resolved)
    return out


def extract_type(text: str | None) -> str | None:
    """从主答里取类型。与等级一致：同段多判定时取**最后一次**（那是模型的最终结论）。"""
    mentions = type_mentions(text)
    return mentions[-1] if mentions else None


def summarize_event_type(event: dict) -> dict:
    """事件级事故类型 = 所辖代表帧类型里的**多数票**。

    为什么不是「取最高」：类型之间没有严重程度排序，等级那套在这里没有意义。
    为什么平票取置信度最高的那帧：置信度是唯一可用的质量信号，
    比「取第一帧」有依据，也比随便挑一个可解释。

    返回：
        event_type            事件级类型（无有效帧时为 None，不填「其他」）
        event_type_frame      决定该类型的代表帧号
        event_type_frames     各代表帧类型序列（含未判定帧的贡献，便于看分歧）
        event_type_agreement  是否所有已判定帧都一致
    """
    pairs = [
        (o.get("accident_type"), float(o.get("confidence") or 0.0), o.get("frame_index"))
        for o in event.get("observations", [])
        if o.get("accident_type")
    ]
    labels = [t for t, _, _ in pairs]
    if not pairs:
        return {
            "event_type": None,
            "event_type_frame": None,
            "event_type_frames": [],
            "event_type_agreement": False,
        }

    counts = Counter(labels)
    top_n = max(counts.values())
    tied = {t for t, n in counts.items() if n == top_n}
    if len(tied) == 1:
        top = next(iter(tied))
        frame = next(f for t, _, f in pairs if t == top)
    else:
        # 平票：只在打平的候选里挑置信度最高的那一帧，不要把落选类型带进来
        top, _, frame = max(
            (p for p in pairs if p[0] in tied), key=lambda p: p[1]
        )

    return {
        "event_type": top,
        "event_type_frame": frame,
        "event_type_frames": labels,
        "event_type_agreement": len(set(labels)) == 1,
    }


def build_type_stats(events: list[dict]) -> dict:
    """类型维度的统计。键名与 _build_stats 里的等级统计保持同一套命名习惯。

    `type_status` 区分「**没跑过分类**」与「跑了但都没判出来」——
    这两件事在报告里必须长得不一样：前者不该出现任何类型栏目，
    后者应该显示「未判定 N 帧」并提示人工复核。把两者混起来会让读者以为
    分类跑过了却全军覆没，或者反过来以为数据里真的有很多「未判定」。
    """
    observations = [o for e in events for o in e.get("observations", [])]
    classified = any("accident_type" in o for o in observations)

    frame_dist = {t: 0 for t in TYPES}
    frame_dist["未判定"] = 0
    event_dist = {t: 0 for t in TYPES}
    event_dist["未判定"] = 0
    if classified:
        for obs in observations:
            frame_dist[obs.get("accident_type") or "未判定"] += 1
        for event in events:
            event_dist[event.get("event_type") or "未判定"] += 1
    # 没跑过分类时，分布全留 0 —— 由 type_status 单独说明原因。
    # 否则「未判定 18 帧」会被读成「跑了但全都没判出来」，那是另一件事。

    return {
        "type_status": "done" if classified else "not_run",
        "type_distribution": frame_dist,
        "event_type_distribution": event_dist,
        "type_unresolved": frame_dist["未判定"] if classified else 0,
        "type_from_followup": sum(
            1 for o in observations if o.get("type_source") == "followup"
        ),
        # 代表帧之间判出不同类型的事件数：这是类型可靠性的直接体现，
        # 比任何"准确率"都诚实——因为还没人工复核过。
        "type_disagreement": sum(
            1
            for e in events
            if e.get("event_type_frames") and not e.get("event_type_agreement")
        ),
        "event_type_rule": "majority_of_representative_frames_ties_by_confidence",
        # 词表随产物下发：前端渲染与人工复核都读它，不在两边各写一份
        "type_vocabulary": list(TYPES),
    }
