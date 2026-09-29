"""sky_eyes 主流程：把「事故检测」与「多模态报告输出」串成一条链路。

流程：
    监控视频
      └─ detector.detect_video()      逐帧 YOLO 检测 -> 事故帧 ROI + 元数据
           └─ detector.group_events() 连号帧聚类 -> 事故事件 + 代表帧
                └─ JanusClient         代表帧送远程多模态模型 -> 事故描述
                     └─ 本脚本         汇总 -> accident_report.json / .md

用法：
    # 完整流程（检测 + 理解 + 报告）
    python pipeline.py

    # 只做检测与聚类，不调多模态（服务没开时也能跑）
    python pipeline.py --stage detect

    # 复用已有的 accident_frames.json，只重跑理解与报告
    #（改了聚类参数或想重新生成描述时用，省掉重新检测的时间）
    python pipeline.py --stage understand

    # 复用已有的 accident_report.json，只按新口径重渲染报告（不重新推理）
    python pipeline.py --rerender

    # 常见调参
    python pipeline.py --conf 0.5 --gap 12 --frames-per-event 3
    python pipeline.py --no-followup          # 不做等级追问，省调用
    python pipeline.py --api-base http://127.0.0.1:8000        # 经隧道（Janus 只绑回环）

    # 告警推送（默认关闭，配了通道也只是"可选开启"）
    python pipeline.py --alert-test           # 演示前自检：往群里发一条测试消息
    python pipeline.py --alert                # 分析中对达标事件增量推送
    python pipeline.py --alert --alert-channel dingtalk

注意：远程服务默认地址为 http://127.0.0.1:8000，需要先执行
      ./scripts/janus_up.sh（启服务 + 建隧道）或 ./scripts/janus_tunnel.sh（仅建隧道）。
      告警通道的目标地址与密钥只从环境变量或 alert.config.json 读，**不接受命令行参数**
      （会被 ps 和落盘的 job.log 记录）。通道名不是密钥，所以可以用 --alert-channel。
      详见 alerting.py 顶部。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

# ---------- 结构化进度（供实时模式的后端转成 SSE，见 server/app.py）---------- #
#
# 只在 --progress-json 时启用。人读的中文日志保持不变，两种输出混在同一个
# stdout 里，后端按 "@@PROGRESS " 前缀过滤。这样本脚本独立运行时的输出
# 一个字都不用改，也不会因为加了进度上报而变得难读。
#
# 进度分四段，与前端的三段式进度条对应：
#   detect（逐帧 YOLO，CPU 上约占全程 90%）→ cluster → understand → done
_PROGRESS_ENABLED = False


def _emit(stage: str, **fields) -> None:
    if _PROGRESS_ENABLED:
        payload = json.dumps({"stage": stage, **fields}, ensure_ascii=False)
        print(f"@@PROGRESS {payload}", flush=True)


def _pct(done: int, total: int) -> float:
    return round(done / total * 100, 1) if total else 0.0


from detector import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_VIDEO,
    DEFAULT_WEIGHTS,
    detect_video,
    group_events,
    source_display_name,
)
from multimodal_understanding import (
    DEFAULT_API_BASE,
    DEFAULT_QUESTION,
    DEFAULT_TEMPERATURE,
    JanusClient,
)
from accident_type import (
    TYPE_FOLLOWUP_QUESTION,
    TYPE_QUESTION,
    build_type_stats,
    extract_type,
    normalize_type,
    summarize_event_type,
)
from alerting import (
    AlertLedger as AlertLedger,
    SEVERITY_ORDER as _SEVERITY_ORDER,
    load_config as load_alert_config,
    push_events as push_alerts,
    report_summary as alert_report_summary,
    send_test as send_alert_test,
    write_ledger as write_alert_ledger,
)

# 从模型回答里抽取严重等级，兼容两种常见表述：
#   「事故严重等级：高」「严重程度为低」「判断为中度」
# 注意 重 -> 高 需归一化（模型爱说「重度」）
SEVERITY_RE = re.compile(
    r"(?:严重)?(?:等级|程度)\s*(?:为|是|：|:)?\s*([低轻中高重])"  # 事故严重等级：高
    r"|([低轻中重])度"  # 初步判断为中度 / 轻度
)
# 「重度」归一成「高」、「轻度」归一成「低」；低/中/高 保持原样。
#
# ⚠ 「高」必须**显式列出来**。这张表曾经只有 低/中/重 三个键，而追问抽取用的是
#   `_DEGREE_MAP.get(x)`（无默认值）——于是模型回答「高」时取到 None，
#   被当成「未判定」。这正是我们最不能接受的「漏报高等级」失效模式。
#   本次数据没暴露纯属运气：5 个追问答案碰巧全是「低」。
_DEGREE_MAP = {"低": "低", "轻": "低", "中": "中", "高": "高", "重": "高"}

# 追问的回答应当就是孤零零一个等级字（「低」「中档」「是：高」）。
#
# ⚠ 这里**必须**严格整串匹配，不能用裸字符类去 search。
#   曾经的写法是 re.compile(r"[低中高重]")，而「严重」里含有「重」——
#   于是「无法判断严重程度」被判定成「高」、「这是一起严重的事故」也被判定成「高」。
#   当前数据恰好没触发（模型每次都老实回单字），但「高」在我们的处置规则里对应
#   「立即派警 + 通知急救」，把一句「无法判断」读成「高」是会让系统做出错误动作的。
#   拿不准就返回 None 走「未判定」，交给人工复核——绝不用宽松匹配去猜。
_STRICT_SEVERITY_RE = re.compile(
    r"^\s*"
    r"(?:答案|严重等级|严重程度|等级)?"  # 可选的前缀词
    r"\s*[:：是为、,，]*\s*"  # 可选的分隔/系词
    r"([低轻中高重])"  # 等级字本身
    r"\s*[等档级度]?\s*"  # 可选的后缀（「中档」「轻度」）
    r"[。.！!？?、,，]*\s*$"  # 可选的收尾标点
)

# 严重等级排序，用于事件级聚合（取最高）。
#
# ⚠ 定义在 `alerting.py` 里，这里只是改名导入——**刻意不在这里再写一份**。
#   告警推送的门控也要用同一张表。两处各留一份的话，会出现
#   「报告算出高等级、告警却因为阈值表不一致而没推」这种安静的分歧，
#   而它恰好落在这个项目最不能接受的失效模式上（漏报高等级）。
#
#   `tests/test_severity.py` 会核对这张表与等级链的口径一致。
_SEVERITY_ORDER_NOTE = (
    "低/中/高 的排序必须查表，不能比较中文字符串："
    "码点序是 中(4E2D) < 低(4F4E) < 高(9AD8)，`max(\"低\",\"中\")` 会返回「低」。"
)

# 主问题没判出等级时的追问：让模型只回一个字，避免它含糊其辞
FOLLOWUP_QUESTION = (
    "综合图中车辆的受损程度和现场情况，这起交通事故的严重等级属于哪一档？"
    "请只回答「低」「中」「高」中的一个字。"
)


def _fmt_time(seconds: float) -> str:
    """把秒数格式化成 mm:ss.s，方便看视频时间点。"""
    minutes, rest = divmod(float(seconds), 60)
    return f"{int(minutes):02d}:{rest:04.1f}"


def _severity_mentions(text: str | None) -> list[str]:
    """列出文中所有严重等级判定（按出现顺序，已把「重度」归一为「高」）。"""
    if not text:
        return []
    out = []
    for match in SEVERITY_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if raw:
            out.append(_DEGREE_MAP.get(raw, raw))
    return out


def _extract_severity(text: str | None, mentions: list[str] | None = None) -> str | None:
    """取严重等级。

    模型偶尔会重复输出、甚至在同一段话里给出不同等级（低→中→高 循环）。
    这里以**最后一次判定**为准，因为那通常才是模型的最终结论；
    同时把文中出现的所有等级记录到 severity_mentions，便于人工复核。
    """
    mentions = _severity_mentions(text) if mentions is None else mentions
    return mentions[-1] if mentions else None


def _severity_from_short_answer(text: str | None) -> str | None:
    """从追问的短答里取等级。

    只接受「整句就是一个等级」的回答（如「低」「中档」「是：高」）。
    任何带解释、带否定、或根本没说等级的长文本一律返回 None，由上层标成「未判定」
    交人工复核。理由见 _STRICT_SEVERITY_RE 上方的注释——这个函数宁可不判，不可误判。
    """
    if not text:
        return None
    match = _STRICT_SEVERITY_RE.match(text)
    if not match:
        return None
    raw = match.group(1)
    # 用带默认值的 get：即便 _DEGREE_MAP 将来漏了某个键，也让它原样通过，
    # 而不是静默变成 None（那正是「漏报高等级」的成因）。
    return _DEGREE_MAP.get(raw, raw)


def _summarize_event_severity(event: dict) -> dict:
    """事件级严重等级 = 该事件所辖代表帧中的**最高**等级。

    为什么取最高，而不是首个代表帧、也不是多数票：

    事故预警场景下最严重的失效是**漏报高级别事件**——一帧已经看到「车顶塌陷」，
    另一帧说「轻微刮擦」，整起事件就绝不能定性为轻微。宁可高报（人工复核时可下调），
    不可低报（下游派警/定损都拿不到正确优先级）。

    返回：
        event_severity        事件级等级（低/中/高，无有效帧时为 None）
        event_severity_frame  决定该等级的代表帧号（便于追溯）
        event_severity_levels 各代表帧等级序列（便于查看分歧）
    """
    pairs = [
        (o.get("severity"), o.get("frame_index"))
        for o in event.get("observations", [])
        if o.get("severity")
    ]
    if not pairs:
        return {
            "event_severity": None,
            "event_severity_frame": None,
            "event_severity_levels": [],
        }
    # 注意：不能直接 max 中文字符串——按 Unicode 码点是 中(4E2D) < 低(4F4E) < 高(9AD8)，
    # 会把「低」当成最大。必须查显式排序表 _SEVERITY_ORDER。
    top, frame = max(pairs, key=lambda p: _SEVERITY_ORDER.get(p[0], -1))
    return {
        "event_severity": top,
        "event_severity_frame": frame,
        "event_severity_levels": [sev for sev, _ in pairs],
    }


def _pick_event_lead_observation(event: dict) -> dict | None:
    """挑出代表该事件的观察记录：优先取决定事件等级的那一帧，否则取首个成功的。"""
    frame = event.get("event_severity_frame")
    for obs in event.get("observations", []):
        if obs.get("ok") and obs.get("frame_index") == frame:
            return obs
    for obs in event.get("observations", []):
        if obs.get("ok"):
            return obs
    return None


def _understand_with_retry(
    client: JanusClient,
    image: Path,
    question: str,
    temperature: float,
    attempts: int = 3,
    delay: float = 3.0,
) -> str:
    """带重试的推理调用。

    这台服务器是共享的，别人的任务可能瞬间抢走显存导致 500 / 连接中断，
    这类瞬时故障重试通常就能过。
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.understand_image(image, question, temperature=temperature)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                print(f"      第 {attempt} 次失败（{exc}），{delay:.0f}s 后重试", file=sys.stderr)
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _is_degenerate(text: str | None, min_len: int = 30, repeat: int = 3) -> bool:
    """粗略检测模型重复退化：同一段较长文字（按空行切块）出现 >= repeat 次。"""
    if not text:
        return False
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
    blocks = [b for b in blocks if len(b) >= min_len]
    if not blocks:
        return False
    return max(Counter(blocks).values()) >= repeat


def _dedupe_blocks(text: str) -> str:
    """折叠重复段落（保留首次出现顺序），用于压缩模型重复退化的输出。"""
    seen = set()
    out = []
    for block in re.split(r"\n\s*\n", text):
        key = block.strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(key)
    return "\n\n".join(out)


def _load_meta(output_dir: Path) -> dict:
    meta_path = output_dir / "accident_frames.json"
    if not meta_path.is_file():
        raise FileNotFoundError(
            f"找不到检测结果 {meta_path}，请先跑检测阶段：python pipeline.py --stage detect"
        )
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _understand_events(
    client: JanusClient,
    events: list[dict],
    question: str,
    output_dir: Path,
    temperature: float = DEFAULT_TEMPERATURE,
    followup: bool = True,
    retries: int = 3,
    on_progress=None,
    on_event_done=None,
) -> None:
    """就地给每个事件的代表帧补充多模态描述与严重等级。

    元数据里的 roi 是相对 output_dir 的路径（如 accident_frames/xxx_roi.jpg）。
    主问题没判出等级时（模型常说「无法判断」），追问一次单选题补上。

    `on_event_done(event)` 在每个事件的**事件级等级算完之后**回调一次。
    告警推送挂在这里而不是等报告写完：告警是"立即派警"的触发信号，
    没有理由等 markdown 渲染完再发。回调里的任何异常都被吞掉——
    推送是旁路，绝不能因为它让跑了 6 分钟的推理白跑。
    """
    total = sum(len(e["representatives"]) for e in events)
    done = 0
    for event in events:
        event["observations"] = []
        for rep in event["representatives"]:
            done += 1
            image = Path(rep["roi"])
            if not image.is_absolute():
                image = output_dir / image
            record = {
                "frame_index": rep["frame_index"],
                "roi": rep["roi"],
                "confidence": rep["confidence"],
            }
            try:
                description = _understand_with_retry(
                    client, image, question, temperature, attempts=retries
                )
                mentions = _severity_mentions(description)
                severity = _extract_severity(description, mentions)
                source = "primary" if severity else None

                if severity is None and followup:
                    answer = _understand_with_retry(
                        client, image, FOLLOWUP_QUESTION, temperature, attempts=retries
                    )
                    record["followup_answer"] = answer.strip()
                    severity = _severity_from_short_answer(answer)
                    if severity:
                        source = "followup"

                record.update(
                    description=description,
                    severity=severity,
                    severity_source=source,
                    severity_mentions=mentions,
                    ambiguous=len(set(mentions)) > 1,
                    degenerate=_is_degenerate(description),
                    ok=True,
                )
                flag = " ⚠重复" if record["degenerate"] else ""
                if record["ambiguous"]:
                    flag += " ⚠等级不一致"
                if source == "followup":
                    flag += " (追问补判)"
                elif severity is None:
                    flag += " (未判定)"
                print(f"  [{done}/{total}] OK  帧 {rep['frame_index']}{flag}")
                preview = " ".join(description.split())
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                print(f"      {preview}")
            except Exception as exc:  # 单张失败不影响整体报告
                record.update(description=None, severity=None, ok=False, error=str(exc))
                print(f"  [{done}/{total}] ERR 帧 {rep['frame_index']}  {exc}", file=sys.stderr)
            event["observations"].append(record)
            if on_progress is not None:
                on_progress(done, total, event["event_id"])
        event.update(_summarize_event_severity(event))
        print(f"  └ 事件 {event['event_id']} 事件级等级：{event['event_severity'] or '未判定'}")
        if on_event_done is not None:
            try:
                on_event_done(event)
            except Exception as exc:  # noqa: BLE001 —— 旁路功能，绝不打断推理
                print(f"      ⚠ 告警推送出现异常（已忽略）：{exc}", file=sys.stderr)


def _classify_events(
    client: JanusClient,
    events: list[dict],
    output_dir: Path,
    temperature: float = DEFAULT_TEMPERATURE,
    retries: int = 3,
    on_progress=None,
) -> None:
    """给已成功的观察记录补上事故类型，**不改动任何等级字段**。

    刻意做成独立一步（而不是塞进 _understand_events）：等级数据是逐个核对过的
    ——实时模式与静态数据逐项一致，那是验收过的结论。新增一个维度不该让那份数据
    发生一丝变化。所以这里既不重置 observations，也不重算 severity，
    只往每条已成功的记录上追加 accident_type。

    输入用的是已有的 ROI 裁剪（不读视频、不重新检测），所以整个阶段只有
    十几次多模态调用，比完整流程快两个数量级。
    """
    targets = [
        obs
        for event in events
        for obs in event.get("observations", [])
        if obs.get("ok") and obs.get("roi")
    ]
    total = len(targets)
    if not total:
        print("  [类型] 没有可分类的观察记录（需先跑理解阶段）")
        return

    for done, obs in enumerate(targets, 1):
        image = Path(obs["roi"])
        if not image.is_absolute():
            image = output_dir / image
        try:
            answer = _understand_with_retry(
                client, image, TYPE_QUESTION, temperature, attempts=retries
            )
            acc_type = extract_type(answer)
            source = "primary" if acc_type else None
            if acc_type is None:
                # 主答里没给出类型（模型常在自由叙述里只说受损部位），追问一次
                short = _understand_with_retry(
                    client, image, TYPE_FOLLOWUP_QUESTION, temperature, attempts=retries
                )
                obs["type_followup_answer"] = (short or "").strip()
                acc_type = normalize_type(short)
                if acc_type:
                    source = "followup"
            obs.update(
                accident_type=acc_type,
                type_source=source,
                # 原文留档：人工复核时要能看到模型到底说了什么，而不只是结论
                type_answer=" ".join((answer or "").split())[:400],
            )
            flag = " (追问补判)" if source == "followup" else ("" if acc_type else " (未判定)")
            print(f"  [{done}/{total}] 帧 {obs['frame_index']} -> {acc_type or '未判定'}{flag}")
        except Exception as exc:  # 单帧失败不影响其余帧，也不影响等级数据
            obs.update(accident_type=None, type_source=None, type_error=str(exc))
            print(f"  [{done}/{total}] ERR 帧 {obs['frame_index']}  {exc}", file=sys.stderr)
        if on_progress is not None:
            on_progress(done, total)

    for event in events:
        event.update(summarize_event_type(event))
        frames = event.get("event_type_frames") or []
        disagreement = "（各帧 " + "→".join(frames) + "）" if len(set(frames)) > 1 else ""
        print(
            f"  └ 事件 {event['event_id']} 事故类型：{event.get('event_type') or '未判定'}"
            f"{disagreement}"
        )


def _build_stats(meta: dict, events: list[dict]) -> dict:
    observations = [o for e in events for o in e.get("observations", [])]
    severity_count = {"低": 0, "中": 0, "高": 0, "未知": 0}
    for obs in observations:
        severity_count[obs.get("severity") or "未知"] += 1
    # 事件级分布：下游（大屏排序、看板、派警优先级）一律以事件级为准
    event_severity_count = {"低": 0, "中": 0, "高": 0, "未知": 0}
    for event in events:
        event_severity_count[event.get("event_severity") or "未知"] += 1
    return {
        "raw_accident_frames": meta.get("accident_frames", len(meta.get("frames", []))),
        "events": len(events),
        "frames_sent": len(observations),
        "understood": sum(1 for o in observations if o.get("ok")),
        "failed": sum(1 for o in observations if not o.get("ok")),
        "degenerate_outputs": sum(1 for o in observations if o.get("degenerate")),
        "ambiguous_severity": sum(1 for o in observations if o.get("ambiguous")),
        "severity_from_followup": sum(
            1 for o in observations if o.get("severity_source") == "followup"
        ),
        "severity_distribution": severity_count,
        "event_severity_distribution": event_severity_count,
        "event_severity_rule": "max_of_representative_frames",
        # 事故类型维度（类型没有好坏排序，事件级用多数票，规则见 event_type_rule）
        **build_type_stats(events),
    }


def _write_markdown(report: dict, path: Path) -> None:
    stats = report["stats"]
    lines: list[str] = []
    lines.append("# 交通事故检测与理解报告")
    lines.append("")
    lines.append(f"- **视频**：`{report['video']}`")
    lines.append(f"- **帧率**：{report['fps']} fps")
    lines.append(f"- **检测模型**：`{report['weights']}`（置信度阈值 {report['conf_threshold']}）")
    lines.append(
        f"- **多模态服务**：`{report['api_base']}`（temperature={report.get('temperature', 0)}）"
    )
    lines.append(f"- **生成时间**：{report['generated_at']}")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(
        f"原始事故帧 **{stats['raw_accident_frames']}** 张，"
        f"经去重聚类为 **{stats['events']}** 个事故事件，"
        f"送多模态模型 **{stats['frames_sent']}** 帧"
        f"（成功 {stats['understood']} / 失败 {stats['failed']}）。"
    )
    lines.append("")
    edist = stats.get("event_severity_distribution") or stats["severity_distribution"]
    fdist = stats["severity_distribution"]
    lines.append(
        "**事件级**严重等级分布（每起事件取所辖代表帧的**最高**等级）："
        f"**高 {edist['高']}** / **中 {edist['中']}** / **低 {edist['低']}**"
        + (f" / 未判定 {edist['未知']}" if edist["未知"] else "")
    )
    lines.append("")
    lines.append(
        "帧级分布（仅供参考）："
        f"高 {fdist['高']} / 中 {fdist['中']} / 低 {fdist['低']}"
        + (f" / 未判定 {fdist['未知']}" if fdist["未知"] else "")
    )
    lines.append("")
    tdist = stats.get("event_type_distribution")
    vocab = stats.get("type_vocabulary") or []
    # 只有真的跑过分类才输出类型栏目。没跑过却印一句「未判定 18 帧」，
    # 会让读者以为分类跑了却全军覆没——那和「没跑」是两件事。
    if stats.get("type_status") == "done":
        parts = [f"{t} {tdist[t]}" for t in vocab if tdist.get(t)]
        if tdist.get("未判定"):
            parts.append(f"未判定 {tdist['未判定']}")
        lines.append(
            "**事件级**事故类型分布（每起事件取所辖代表帧的**多数票**）："
            + (" / ".join(parts) or "全部未判定")
        )
        lines.append("")
        lines.append(
            "> 类型只依据**车辆受损部位**判断，输入是车辆 ROI 裁剪，"
            "不含相对运动位置与路面痕迹；代表帧判出不同类型的事件会在下方逐帧列出。"
        )
        lines.append("")
        if stats.get("type_disagreement"):
            lines.append(
                f"> ⚠ {stats['type_disagreement']} 起事件的代表帧给出了不同的事故类型，"
                "该事件类型以多数票为准，逐帧结果见事件详情。"
            )
            lines.append("")
    if stats.get("severity_from_followup"):
        lines.append(
            f"其中 {stats['severity_from_followup']} 帧的等级来自追加追问"
            "（模型在主问题里未给出明确等级）。"
        )
        lines.append("")
    if stats.get("degenerate_outputs") or stats.get("ambiguous_severity"):
        issues = []
        if stats.get("degenerate_outputs"):
            issues.append(f"{stats['degenerate_outputs']} 帧模型输出有重复")
        if stats.get("ambiguous_severity"):
            issues.append(f"{stats['ambiguous_severity']} 帧文中给出过互相矛盾的等级")
        lines.append(
            f"> ⚠ 质量提示：{'，'.join(issues)}；"
            "严重等级一律取模型**最后一次**判定，可与下方原文核对。"
        )
        lines.append("")
    with_type = stats.get("type_status") == "done"
    lines.append(
        "| 事件 | 时间段 | 帧范围 | 峰值置信度 | 严重等级 |"
        + (" 事故类型 |" if with_type else "")
        + " 描述摘要 |"
    )
    lines.append("| --- | --- | --- | --- | --- |" + (" --- |" if with_type else "") + " --- |")
    for event in report["events"]:
        obs = _pick_event_lead_observation(event)
        severity = event.get("event_severity")
        acc_type = event.get("event_type")
        summary = (obs.get("description") or "—") if obs else "—"
        summary = summary.replace("\n", " ").strip()
        if len(summary) > 60:
            summary = summary[:60] + "…"
        lines.append(
            f"| {event['event_id']} "
            f"| {_fmt_time(event['start_sec'])}-{_fmt_time(event['end_sec'])} "
            f"| {event['start_frame']}-{event['end_frame']} "
            f"| {event['peak_confidence']:.3f} "
            f"| {severity or '—'} "
            + (f"| {acc_type or '—'} " if with_type else "")
            + f"| {summary} |"
        )
    lines.append("")
    lines.append("## 事件详情")
    lines.append("")
    for event in report["events"]:
        lines.append(
            f"### 事件 {event['event_id']} · {_fmt_time(event['start_sec'])}"
            f"（帧 {event['start_frame']}-{event['end_frame']}，{event['num_frames']} 帧）"
        )
        lines.append("")
        levels = event.get("event_severity_levels") or []
        src = event.get("event_severity_frame")
        reps = event.get("representatives") or []
        first_rep = reps[0]["frame_index"] if reps else None
        detail = f"，各代表帧等级 {'→'.join(levels)}" if len(set(levels)) > 1 else ""
        src_note = f"，取自帧 {src}" if src is not None and src != first_rep else ""
        lines.append(
            f"- **事件级严重等级：{event.get('event_severity') or '未判定'}**"
            f"（取所辖代表帧最高{detail}{src_note}）"
        )
        t_frames = event.get("event_type_frames") or []
        t_note = f"，各代表帧类型 {'→'.join(t_frames)}" if len(set(t_frames)) > 1 else ""
        if with_type:
            lines.append(
                f"- **事件级事故类型：{event.get('event_type') or '未判定'}**"
                f"（取所辖代表帧**多数票**{t_note}）"
            )
        for obs in event["observations"]:
            tag = f"{obs['roi']}（帧 {obs['frame_index']}，置信度 {obs['confidence']:.3f}）"
            if obs.get("ok"):
                notes = []
                if obs.get("ambiguous"):
                    levels_txt = "→".join(dict.fromkeys(obs.get("severity_mentions") or []))
                    notes.append(f"文中先后出现 {levels_txt}，取末次")
                if obs.get("degenerate"):
                    notes.append("模型输出有重复")
                if obs.get("severity_source") == "followup":
                    notes.append("主问题未给等级，追问补判")
                if len(set(levels)) > 1 and src == obs.get("frame_index"):
                    notes.append("本帧等级决定事件等级")
                suffix = f"（{'；'.join(notes)}）" if notes else ""
                lines.append(f"- **代表帧**：{tag}")
                lines.append(f"  - 严重等级：**{obs.get('severity') or '未判定'}**{suffix}")
                if with_type:
                    lines.append(f"  - 事故类型：**{obs.get('accident_type') or '未判定'}**")
                description = obs["description"]
                if obs.get("degenerate"):
                    description = _dedupe_blocks(description)
                lines.append("  - 描述：")
                for para in description.split("\n"):
                    lines.append(f"    {para.rstrip()}")
            else:
                lines.append(f"- **代表帧**：{tag} — 推理失败：{obs.get('error')}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_type_line(stats: dict) -> None:
    """打印事件级类型分布。**只在 `type_status == "done"` 时调用。**

    没跑过分类却印一行「事件级类型：」，读者会以为分类跑过而结果是空的。
    """
    dist = stats.get("event_type_distribution") or {}
    vocab = stats.get("type_vocabulary") or []
    parts = [f"{t} {dist[t]}" for t in vocab if dist.get(t)]
    if dist.get("未判定"):
        parts.append(f"未判定 {dist['未判定']}")
    print("  事件级类型（多数票）：" + (" / ".join(parts) or "全部未判定"))


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="sky_eyes：事故检测 + 多模态事故报告")
    parser.add_argument(
        "--stage",
        choices=["detect", "understand", "classify", "all"],
        default="all",
        help=(
            "detect=只检测聚类；understand=复用已有检测结果只跑理解；"
            "classify=复用已有 ROI 只跑事故类型分类（不动等级数据）；all=全流程"
        ),
    )
    parser.add_argument(
        "-v", "--video", default=str(DEFAULT_VIDEO),
        help="输入视频路径，或视频流地址（rtsp:// / rtmp:// / http(s):// / srt://）",
    )
    parser.add_argument("-w", "--weights", default=str(DEFAULT_WEIGHTS), help="YOLO 权重路径")
    parser.add_argument("-c", "--conf", type=float, default=0.25, help="检测置信度阈值")
    parser.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--no-video", action="store_true", help="不生成标注视频")
    parser.add_argument("--gap", type=int, default=10, help="事件聚类的帧间隔容差")
    parser.add_argument("--frames-per-event", type=int, default=1, help="每个事件抽几帧送模型")
    parser.add_argument(
        "--max-frames", type=int, default=None,
        help="最多分析多少帧（接入实时流时必填：流不会自己结束）",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"多模态服务地址")
    parser.add_argument("-q", "--question", default=DEFAULT_QUESTION, help="提问内容")
    parser.add_argument(
        "-t",
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"采样温度，0 为贪心解码（默认 {DEFAULT_TEMPERATURE}，调高易出现重复输出）",
    )
    parser.add_argument(
        "--no-followup",
        action="store_true",
        help="主问题没判出严重等级时不追加追问（省一次调用，但报告里会留「未判定」）",
    )
    parser.add_argument(
        "--with-classify",
        action="store_true",
        help=(
            "all/understand 阶段之后额外跑事故类型分类。**默认关闭**："
            "该维度尚未验证，见 accident_type.py 顶部与 DEMO_PLAN.md §6.10"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="单帧推理失败的重试次数（共享服务器偶发显存被抢，默认 3）",
    )
    parser.add_argument(
        "--alert",
        action="store_true",
        help=(
            "开启告警推送：每个事件的事件级等级算出来后立即推送达标事件。"
            "**默认关闭**，必须显式开启。目标地址与密钥只从环境变量或 alert.config.json 读"
            "——刻意不做成命令行参数，否则它们会进 shell history 和落盘的 job.log。"
            "详见 alerting.py 顶部"
        ),
    )
    parser.add_argument(
        "--alert-channel",
        choices=["wecom", "dingtalk", "feishu", "smtp"],
        default=None,
        help=(
            "告警通道（默认取配置，内置默认 wecom）。通道名不是密钥，所以可以走命令行。"
            "注意钉钉/飞书的自定义机器人**发不了图**，smtp 的图走附件"
        ),
    )
    parser.add_argument(
        "--alert-min-severity",
        choices=["低", "中", "高"],
        default=None,
        help="推送的等级下限（默认取配置，内置默认「高」）。「未判定」永远不推",
    )
    parser.add_argument(
        "--no-alert-image",
        action="store_true",
        help="推送时不附代表帧 ROI 图（默认附；附图失败不影响文字卡片）",
    )
    parser.add_argument(
        "--alert-max",
        type=int,
        default=None,
        help="本次运行最多推送几条（默认取配置，内置默认 5）。撞上限时优先推最严重的",
    )
    parser.add_argument(
        "--alert-test",
        action="store_true",
        help="只往群里发一条测试消息并退出，用于演示前自检通道（不做任何检测）",
    )
    parser.add_argument(
        "--rerender",
        action="store_true",
        help="不重新推理，仅按当前聚合规则重渲染已有报告（改了统计口径时用）",
    )
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="额外输出 @@PROGRESS {...} 结构化进度行，供 server/app.py 转成 SSE（不影响原有日志）",
    )
    args = parser.parse_args(argv)

    global _PROGRESS_ENABLED
    _PROGRESS_ENABLED = args.progress_json

    # 告警配置在这里就解析出来（供自检与全流程共用）。
    # 不传地址与密钥：它们只可能来自环境变量或配置文件，见 --alert 的 help。
    alert_config = load_alert_config(
        channel=args.alert_channel,
        min_severity=args.alert_min_severity,
        with_image=False if args.no_alert_image else None,
        max_per_run=args.alert_max,
    )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 便捷路径：只做告警通道自检，不跑任何检测 ---------- #
    #
    # 这条路径的存在理由很实际：演示当天才发现 webhook key 填错，
    # 代价是整整一轮 6 分钟的检测加上一次现场尴尬。所以把它做成一条秒级命令。
    if args.alert_test:
        print("=" * 60)
        print("[告警自检] 只发一条测试消息，不做检测")
        print("=" * 60)
        print(f"  配置：{alert_config.describe()}")
        for note in alert_config.notes:
            print(f"  ⚠ {note}")
        outcome = send_alert_test(alert_config)
        if outcome.status == "sent":
            print("  ✅ 测试消息已发送，请到对应的群/邮箱里确认收到")
            return 0
        print(f"  ❌ 未发送：{outcome.reason}", file=sys.stderr)
        if not alert_config.enabled:
            print(
                "       需要先配一个通道。最省事的两种（都不需要管理员审批）：\n"
                "       · 钉钉/飞书：在群里加一个「自定义机器人」，把 Webhook 地址填进 alert.config.json\n"
                "       · 邮件：填 SMTP 主机、账号、授权码与收件人（见 alert.config.example.json）\n"
                "       配置文件已被 .gitignore 忽略；也可以用环境变量 SKYEYES_ALERT_URL 等。",
                file=sys.stderr,
            )
        return 1

    # ---------- 便捷路径：只重渲染已有报告，不调用模型 ---------- #
    if args.rerender:
        report_path = output_dir / "accident_report.json"
        if not report_path.is_file():
            print(f"[错误] 找不到 {report_path}，请先完整跑一次流程", file=sys.stderr)
            return 1
        if args.alert:
            print(
                "[告警] ⚠ --rerender 不推送告警：这一步没有产生新的判定，"
                "重推只会往群里刷重复消息（每条结论都与上次一致）"
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for event in report["events"]:
            event.update(_summarize_event_severity(event))
        report["stats"] = _build_stats(
            {"accident_frames": report["stats"].get("raw_accident_frames")},
            report["events"],
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_md = output_dir / "accident_report.md"
        _write_markdown(report, report_md)
        edist = report["stats"]["event_severity_distribution"]
        fdist = report["stats"]["severity_distribution"]
        print("[重渲染] 未调用多模态模型，仅按新口径重算统计")
        print(f"  事件级等级（取最高）：高 {edist['高']} / 中 {edist['中']} / 低 {edist['低']}")
        print(f"  帧级等级（参考）    ：高 {fdist['高']} / 中 {fdist['中']} / 低 {fdist['低']}")
        # 类型栏目只在真的跑过分类时才出现（见 accident_type.py：该维度实验性、默认不跑）
        if report["stats"].get("type_status") == "done":
            _print_type_line(report["stats"])
        print(f"  JSON -> {report_path}")
        print(f"  报告 -> {report_md}")
        return 0

    # ---------- 便捷路径：只跑事故类型分类 ---------- #
    if args.stage == "classify":
        report_path = output_dir / "accident_report.json"
        if not report_path.is_file():
            print(
                f"[错误] 找不到 {report_path}，请先跑一次理解阶段"
                "（python pipeline.py --stage understand）",
                file=sys.stderr,
            )
            return 1
        print("=" * 60)
        print("[阶段] 事故类型分类（复用已有 ROI，不重新检测、不改动等级）")
        print("=" * 60)
        print(
            "⚠ 实验性：此阶段在 2026-09-21 的 18 帧实测中给出 18 个相同的「侧碰」，\n"
            "  且部分帧的答案与画面明显不符（如摩托车倒地的画面）。\n"
            "  结论**不可用于对外展示**，除非先人工复核或换更强的模型。\n"
            "  背景与实测数据见 accident_type.py 顶部注释。"
        )
        print("=" * 60)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        events = report["events"]
        client = JanusClient(api_base=args.api_base)
        try:
            info = client.health()
            print(f"[服务] {args.api_base}  device={info.get('device')}  cuda={info.get('cuda')}")
        except Exception as exc:
            print(f"[错误] 无法连接多模态服务 {args.api_base}: {exc}", file=sys.stderr)
            print("       请先启动服务与隧道：./scripts/janus_up.sh", file=sys.stderr)
            return 1

        _classify_events(
            client,
            events,
            output_dir,
            args.temperature,
            retries=args.retries,
            on_progress=lambda done, total: _emit(
                "classify",
                status="running",
                done=done,
                total=total,
                pct=_pct(done, total),
            ),
        )
        report["type_question"] = TYPE_QUESTION
        report["classified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report["stats"] = _build_stats(
            {"accident_frames": report["stats"].get("raw_accident_frames")},
            events,
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_md = output_dir / "accident_report.md"
        _write_markdown(report, report_md)

        stats = report["stats"]
        print()
        print(f"[类型] 已分类 {stats['frames_sent']} 帧，未判定 {stats['type_unresolved']} 帧")
        _print_type_line(stats)
        if stats["type_disagreement"]:
            print(f"  ⚠ {stats['type_disagreement']} 起事件的代表帧类型不一致（取多数票）")
        print(f"  JSON -> {report_path}")
        print(f"  报告 -> {report_md}")
        _emit(
            "done",
            status="done",
            pct=100.0,
            report=str(report_path),
            stats={"event_type_distribution": tdist},
        )
        return 0

    # ---------- 阶段 1：检测 + 聚类 ---------- #
    if args.stage in ("detect", "all"):
        print("=" * 60)
        print("[阶段 1/2] 事故检测与事件聚类")
        print("=" * 60)
        _emit("detect", status="running", done=0, total=0, pct=0.0, accident_frames=0)

        def _on_detect_frame(fi: int, total: int, n_acc: int) -> None:
            _emit(
                "detect",
                status="running",
                done=fi,
                total=total,
                pct=_pct(fi, total),
                accident_frames=n_acc,
            )

        meta = detect_video(
            video_path=args.video,
            weights=args.weights,
            conf=args.conf,
            save_video=not args.no_video,
            output_dir=output_dir,
            on_progress=_on_detect_frame,
            max_frames=args.max_frames,
        )
        _emit(
            "detect",
            status="done",
            done=meta.get("total_frames") or 0,
            total=meta.get("total_frames") or 0,
            pct=100.0,
            accident_frames=meta.get("accident_frames", len(meta["frames"])),
        )
    else:
        print("=" * 60)
        print("[阶段 1/2] 跳过检测，复用已有元数据")
        print("=" * 60)
        meta = _load_meta(output_dir)

    _emit("cluster", status="running")
    events = group_events(
        meta["frames"], gap=args.gap, frames_per_event=args.frames_per_event
    )
    (output_dir / "accident_events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _emit(
        "cluster",
        status="done",
        pct=100.0,
        events=len(events),
        accident_frames=meta.get("accident_frames", len(meta["frames"])),
    )
    print(
        f"[聚类] {meta.get('accident_frames', len(meta['frames']))} 张事故帧 "
        f"-> {len(events)} 个事故事件（每事件 {args.frames_per_event} 帧）"
    )
    for event in events:
        reps = ", ".join(str(r["frame_index"]) for r in event["representatives"])
        print(
            f"  事件 {event['event_id']:>2}: 帧 {event['start_frame']}-{event['end_frame']}"
            f" ({event['num_frames']} 帧) 峰值置信度 {event['peak_confidence']:.3f} 代表帧 [{reps}]"
        )

    if args.stage == "detect":
        print(f"\n已完成检测阶段，结果在 {output_dir}")
        return 0

    # ---------- 阶段 2：多模态理解 ---------- #
    print()
    print("=" * 60)
    print("[阶段 2/3] 多模态事故描述")
    print("=" * 60)
    client = JanusClient(api_base=args.api_base)
    try:
        info = client.health()
        print(
            f"[服务] {args.api_base}  device={info.get('device')}  cuda={info.get('cuda')}"
        )
    except Exception as exc:
        print(f"[错误] 无法连接多模态服务 {args.api_base}: {exc}", file=sys.stderr)
        print(
            "       请先启动服务与隧道：./scripts/janus_up.sh\n"
            "       （只想跑检测阶段可加 --stage detect）",
            file=sys.stderr,
        )
        return 1

    # ---------- 告警推送的准备（旁路，失败绝不影响分析） ---------- #
    #
    # 推在「每个事件的等级刚算完」而不是报告写完：告警是「立即派警」的触发信号，
    # 没有理由等 markdown 渲染完。台账每条之后都落盘，进程被中途杀掉账目也不丢。
    #
    # ⚠ 台账**在没配 webhook 时也要建**。否则"开了 --alert 但没配通道"这种状态
    #   在报告和台账里一点痕迹都没有——现场表现就是"以为开了、群里静悄悄、
    #   屏上也不说"，是最难排查的一类失效。现在它会如实写 enabled=false。
    alert_ledger: AlertLedger | None = None
    if args.alert:
        alert_ledger = AlertLedger()
        for note in alert_config.notes:
            print(f"[告警] ⚠ {note}")
        if not alert_config.enabled:
            print(
                "[告警] ⚠ 指定了 --alert，但没有配置通道，本次不会推送（分析照常跑完）。\n"
                "       配一个通道：钉钉/飞书的自定义机器人、企业微信消息推送、或 SMTP 邮件，\n"
                "       都在 alert.config.json 里填（模板见 alert.config.example.json）。\n"
                "       通道自检：python pipeline.py --alert-test"
            )
        else:
            print(f"[告警] 已开启通道 → {alert_config.describe()}")

    def _alert_hook(event: dict) -> None:
        if alert_ledger is None:
            return
        push_alerts(
            [event],
            alert_config,
            alert_ledger,
            image_root=output_dir,
            video_name=source_display_name(meta.get("video") or args.video),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        outcome = alert_ledger.outcomes[-1]
        if outcome.status == "sent":
            mark = "，含现场图" if outcome.image_sent else ""
            print(f"      🔔 已推送{alert_config.label}告警（等级 {outcome.severity}{mark}）")
        elif outcome.status == "failed":
            print(f"      ⚠ 企微告警推送失败：{outcome.reason}", file=sys.stderr)
        write_alert_ledger(output_dir / "alerts.json", alert_config, alert_ledger)

    _understand_events(
        client,
        events,
        args.question,
        output_dir,
        args.temperature,
        followup=not args.no_followup,
        retries=args.retries,
        on_event_done=_alert_hook if alert_ledger is not None else None,
        on_progress=lambda done, total, eid: _emit(
            "understand",
            status="running",
            done=done,
            total=total,
            pct=_pct(done, total),
            event_id=eid,
        ),
    )

    # ---------- 阶段 3：事故类型分类（**默认不跑**） ---------- #
    # 为什么不默认跑：这个维度尚未验证。实测在 18 帧上给出 18 个「侧碰」（恒定值），
    # 而其中一帧画面就是「摩托车倒在地上」——模型是在叙事，不是在观察。
    # 详见 accident_type.py 顶部与 DEMO_PLAN.md 的 P10 记录。
    if args.with_classify:
        print()
        print("=" * 60)
        print("[阶段 3/3] 事故类型分类（⚠ 实验性，结论未经人工复核）")
        print("=" * 60)
        _classify_events(
            client,
            events,
            output_dir,
            args.temperature,
            retries=args.retries,
            on_progress=lambda done, total: _emit(
                "classify",
                status="running",
                done=done,
                total=total,
                pct=_pct(done, total),
            ),
        )

    # ---------- 汇总报告 ---------- #
    report = {
        "video": meta.get("video", str(args.video)),
        "weights": meta.get("weights", str(args.weights)),
        "fps": meta.get("fps"),
        "conf_threshold": meta.get("conf_threshold", args.conf),
        "api_base": args.api_base,
        "question": args.question,
        "temperature": args.temperature,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": _build_stats(meta, events),
        "events": events,
    }
    # 告警台账只在**真的开了告警**时才进报告。没开就不加这个键，
    # 静态 demo 的 accident_report.json 因此保持逐字节不变（与类型栏目同一套原则）。
    if alert_ledger is not None:
        write_alert_ledger(output_dir / "alerts.json", alert_config, alert_ledger)
        report["alerts"] = alert_report_summary(alert_config, alert_ledger)
    report_json = output_dir / "accident_report.json"
    report_md = output_dir / "accident_report.md"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, report_md)

    stats = report["stats"]
    edist = stats["event_severity_distribution"]
    fdist = stats["severity_distribution"]
    print()
    print("=" * 60)
    print("[完成] 报告已生成")
    print("=" * 60)
    print(f"  事故帧 {stats['raw_accident_frames']} 张 -> 事件 {stats['events']} 个")
    print(
        f"  送检 {stats['frames_sent']} 帧（成功 {stats['understood']} / 失败 {stats['failed']}）"
    )
    print(f"  事件级等级（取最高）：高 {edist['高']} / 中 {edist['中']} / 低 {edist['低']}"
          + (f" / 未判定 {edist['未知']}" if edist["未知"] else ""))
    print(f"  帧级等级（参考）    ：高 {fdist['高']} / 中 {fdist['中']} / 低 {fdist['低']}"
          + (f" / 未判定 {fdist['未知']}" if fdist["未知"] else ""))
    if stats.get("severity_from_followup"):
        print(f"  其中追问补判 {stats['severity_from_followup']} 帧")
    tdist = stats["event_type_distribution"]
    vocab = stats.get("type_vocabulary") or []
    if stats.get("type_status") == "done":
        print(
            "  事件级类型（多数票）："
            + " / ".join(f"{t} {tdist[t]}" for t in vocab if tdist.get(t))
            + (f" / 未判定 {tdist['未判定']}" if tdist["未判定"] else "")
        )
    if stats.get("type_disagreement"):
        print(f"  ⚠ {stats['type_disagreement']} 起事件的代表帧类型不一致（取多数票）")
    if alert_ledger is not None:
        print(f"  企微告警：{alert_ledger.summary_line()}")
        print(f"  告警台账 -> {output_dir / 'alerts.json'}")
    print(f"  JSON -> {report_json}")
    print(f"  报告 -> {report_md}")
    _emit(
        "done",
        status="done",
        pct=100.0,
        report=str(report_json),
        stats={
            "accident_frames": stats["raw_accident_frames"],
            "events": stats["events"],
            "frames_sent": stats["frames_sent"],
            "event_severity_distribution": edist,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
