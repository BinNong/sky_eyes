"""企微告警推送的回归测试。

跑法（本模块只用标准库，**两套环境都能跑**，用后端 venv 最快）：

    server/.venv/bin/python tests/test_alerting.py

不联网、不需要 GPU、不需要多模态服务：真实的企业微信请求被一个假的
`transport` 换掉了，这样既能测到请求体长什么样，又不用真往群里发消息。

为什么专门给这块写测试
----------------------
告警推送的错误后果落在系统之外——**发进真实工作群的消息收不回来**。
而且这条链上几乎每个失效模式都是"静默"的：不抛异常，只是安静地做了错事。

守的七件事：

1. **门控不能拿中文等级比大小**。码点序是 中(4E2D) < 低(4F4E) < 高(9AD8)，
   所以 `"低" >= "中"` 为真（错的），`"高" >= "低"` 也为真（碰巧对）。
   判别性的用例是**阈值「中」时「低」必须被拦下**——按字符串比它会漏过去。
2. **「未判定」永远不推**。不知道有多严重 ≠ 不严重。
3. **HTTP 200 不等于发送成功**。企微出错时同样返回 200，结论在 `errcode` 里。
   把 errcode≠0 记成"已推送"，现场表现就是群里静悄悄、大屏上写着"已推送 1 条"。
4. **webhook 的 key 不许出现在任何对外字符串里**（日志、报告、台账、界面）。
5. **任何失败都不许让分析中断**（连接失败、超时、图读不到、图超大）。
6. **幂等 + 上限**：同一次运行里同一事件只推一次；撞击上限时优先推最严重的。
7. **卡片里的话和界面上的一致**：处置建议与免责声明直接读前端词条核对，
   不允许"群里说一套、屏上说一套"。
"""

import base64
import hashlib
import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import alerting  # noqa: E402
from alerting import (  # noqa: E402
    CHANNEL_DINGTALK,
    CHANNEL_FEISHU,
    CHANNEL_SMTP,
    CHANNEL_WECOM,
    AlertLedger,
    SEVERITY_ORDER,
    build_payload,
    channel_view,
    load_config,
    mask_webhook,
    push_events,
    render_event_card,
    roi_path,
    send_test,
    target_url,
    write_ledger,
)

# 测试期间不真等退避，否则守"会不会重试"的用例要慢好几秒
alerting.RETRY_BACKOFF = 0

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=0123456789abcdef0123456789abcdef"
KEY_FULL = "0123456789abcdef0123456789abcdef"
KEY_MIDDLE = "56789abcdef012"  # 打码后不该出现的中间片段


def cfg(**kwargs):
    """默认用「隔离了本机环境变量 + 不存在的配置文件」的配置。

    ⚠ 必须显式传 env={} 与 config_path：否则跑测试那台机器上恰好设了
    SKYEYES_WECOM_WEBHOOK（或项目根有 alert.config.json）就会污染结果，
    测试变成"在我机器上是过的"。
    """
    defaults = {
        "webhook": WEBHOOK,
        "env": {},
        "config_path": Path(tempfile.gettempdir()) / "skyeyes-no-such-alert-config.json",
    }
    defaults.update(kwargs)
    return load_config(**defaults)


def event(event_id=1, severity="高", start_sec=10.0, rois=()):
    observations = [
        {
            "frame_index": 100 + i,
            "roi": roi,
            "ok": True,
            "severity": severity,
            "description": f"第 {i} 帧描述：车头明显变形，安全气囊弹出。",
        }
        for i, roi in enumerate(rois)
    ]
    if not observations:
        observations = [
            {
                "frame_index": 100,
                "roi": None,
                "ok": True,
                "severity": severity,
                "description": "车头明显变形，安全气囊弹出，路面散落碎片。",
            }
        ]
    return {
        "event_id": event_id,
        "start_frame": 100,
        "end_frame": 120,
        "start_sec": start_sec,
        "end_sec": start_sec + 0.6,
        "peak_confidence": 0.517,
        "event_severity": severity,
        "event_severity_frame": observations[0]["frame_index"] if severity else None,
        "event_severity_levels": [severity] if severity else [],
        "observations": observations,
    }


class FakeTransport:
    """假的企微端点。记录每次请求体，按设定回一个 body。"""

    def __init__(self, body=None, exc=None):
        self.body = {"errcode": 0, "errmsg": "ok"} if body is None else body
        self.exc = exc
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if self.exc is not None:
            raise self.exc
        return self.body


# ───────────────────────── 1. 等级门控 ─────────────────────────


def test_severity_order_literal_is_explicit():
    """等级顺序必须是写死的映射，不是靠中文串的码点。"""
    assert SEVERITY_ORDER == {"低": 0, "中": 1, "高": 2}


def test_low_is_blocked_when_threshold_is_medium():
    """判别性用例：阈值「中」时「低」必须被拦下。

    如果谁把门控改成 `sev >= config.min_severity`，这条会失败——
    因为 `"低" >= "中"` 按码点是 True（低 U+4F4E > 中 U+4E2D）。
    """
    t = FakeTransport()
    ledger = push_events(
        [event(1, "低"), event(2, "中"), event(3, "高")],
        cfg(min_severity="中"),
        AlertLedger(),
        transport=t,
    )
    sent = [o.event_id for o in ledger.outcomes if o.status == "sent"]
    assert sent == [3, 2], f"应只推 高/中，实际 {sent}"
    assert t.calls, "至少应发生一次请求"


def test_threshold_high_only_sends_high():
    t = FakeTransport()
    ledger = push_events(
        [event(1, "低"), event(2, "中"), event(3, "高"), event(4, "高")],
        cfg(),
        AlertLedger(),
        transport=t,
    )
    sent = sorted(o.event_id for o in ledger.outcomes if o.status == "sent")
    assert sent == [3, 4]
    skipped = [o for o in ledger.outcomes if o.status == "skipped"]
    assert len(skipped) == 2
    assert all(o.reason == "等级低于阈值" for o in skipped)


def test_threshold_low_sends_everything_determined():
    t = FakeTransport()
    ledger = push_events(
        [event(1, "低"), event(2, "中"), event(3, "高")],
        cfg(min_severity="低"),
        AlertLedger(),
        transport=t,
    )
    assert ledger.counts()["sent"] == 3


def test_undetermined_severity_is_never_sent():
    """「未判定」是"不知道"，不是"不严重"——绝不能惊动处置人员。"""
    t = FakeTransport()
    events = [
        event(1, None),
        event(2, "未知"),
    ]
    events[0]["event_severity_levels"] = []
    events[1]["event_severity_levels"] = []
    ledger = push_events(events, cfg(min_severity="低"), AlertLedger(), transport=t)
    assert ledger.counts()["sent"] == 0
    assert t.calls == [], "等级未判定时不应发出任何请求"
    assert all(o.reason == "等级未判定" for o in ledger.outcomes)


# ───────────────────────── 2. 成败判定 ─────────────────────────


def test_errcode_nonzero_is_failure_not_success():
    """HTTP 200 + errcode≠0 = 失败。这是本模块最容易写错的一处。"""
    t = FakeTransport(body={"errcode": 93000, "errmsg": "invalid webhook url"})
    ledger = push_events([event(1, "高")], cfg(max_per_run=1), AlertLedger(), transport=t)
    outcome = ledger.outcomes[0]
    assert outcome.status == "failed", outcome
    assert outcome.text_sent is False
    assert "93000" in outcome.reason
    assert ledger.counts()["sent"] == 0


def test_response_without_errcode_is_failure():
    """响应里没有 errcode 时判失败：宁可误报失败，不可误报成功。"""
    t = FakeTransport(body={"errmsg": "ok"})
    ledger = push_events([event(1, "高")], cfg(), AlertLedger(), transport=t)
    assert ledger.outcomes[0].status == "failed"
    assert "errcode" in ledger.outcomes[0].reason


def test_non_dict_response_is_failure():
    t = FakeTransport(body=["not", "a", "dict"])
    ledger = push_events([event(1, "高")], cfg(), AlertLedger(), transport=t)
    assert ledger.outcomes[0].status == "failed"


def test_transport_exception_does_not_propagate():
    t = FakeTransport(exc=OSError("Network is unreachable"))
    ledger = push_events([event(1, "高")], cfg(), AlertLedger(), transport=t)  # 不抛即通过
    assert ledger.outcomes[0].status == "failed"
    assert "Network is unreachable" in ledger.outcomes[0].reason


def test_errcode_failure_is_not_retried():
    """errcode≠0 是配置问题，重试只浪费配额。"""
    t = FakeTransport(body={"errcode": 93000, "errmsg": "invalid webhook url"})
    push_events([event(1, "高")], cfg(retries=3), AlertLedger(), transport=t)
    assert len(t.calls) == 1, f"不该重试，实际请求 {len(t.calls)} 次"


def test_transport_exception_is_retried():
    """拿不到明确答复（连接失败）才重试。retries=1 → 总共 2 次。"""
    t = FakeTransport(exc=OSError("boom"))
    push_events([event(1, "高")], cfg(retries=1), AlertLedger(), transport=t)
    assert len(t.calls) == 2


def test_retry_then_success_is_reported_as_sent():
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return {"errcode": 0, "errmsg": "ok"}

    ledger = push_events([event(1, "高")], cfg(retries=2), AlertLedger(), transport=flaky)
    assert ledger.outcomes[0].status == "sent"
    assert calls["n"] == 2


# ───────────────────────── 3. 密钥不外泄 ─────────────────────────


def test_webhook_key_never_leaks_into_outcomes():
    t = FakeTransport()
    ledger = push_events([event(1, "高")], cfg(with_image=False), AlertLedger(), transport=t)
    blob = json.dumps(
        {
            "outcomes": [o.to_dict() for o in ledger.outcomes],
            "summary": alerting.report_summary(cfg(), ledger),
        },
        ensure_ascii=False,
    )
    assert KEY_FULL not in blob
    assert KEY_MIDDLE not in blob


def test_webhook_key_never_leaks_into_ledger_file():
    t = FakeTransport()
    config = cfg(with_image=False)
    ledger = push_events([event(1, "高")], config, AlertLedger(), transport=t)
    with tempfile.TemporaryDirectory() as tmp:
        path = write_ledger(Path(tmp) / "alerts.json", config, ledger)
        text = path.read_text(encoding="utf-8")
    assert KEY_FULL not in text
    assert KEY_MIDDLE not in text
    assert "qyapi.weixin.qq.com" in text, "主机与路径要保留，便于排查配错了哪个 webhook"


def test_mask_keeps_host_and_tail():
    masked = mask_webhook(WEBHOOK)
    assert masked.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=0123")
    assert masked.endswith("cdef")
    assert KEY_MIDDLE not in masked


def test_mask_handles_short_and_empty():
    assert mask_webhook(None) is None
    assert mask_webhook("") is None
    assert mask_webhook("https://x/y?key=abc") == "https://x/y?key=…"


# ───────────────────────── 4. 未配置 / 幂等 / 上限 ─────────────────────────


def test_no_webhook_means_zero_requests():
    """没配通道时一次网络请求都不许发。"""
    t = FakeTransport()
    config = load_config(env={}, config_path=Path("/nonexistent/alert.json"))
    assert config.enabled is False
    ledger = push_events([event(1, "高")], config, AlertLedger(), transport=t)
    assert t.calls == []
    assert ledger.counts()["sent"] == 0
    assert "未配置" in ledger.outcomes[0].reason
    # 重复调用不再堆一条"未配置"
    push_events([event(2, "高")], config, ledger, transport=t)
    assert len(ledger.outcomes) == 1
    assert t.calls == []


def test_send_test_without_webhook_does_not_call_transport():
    t = FakeTransport()
    config = load_config(env={}, config_path=Path("/nonexistent/alert.json"))
    outcome = send_test(config, transport=t)
    assert outcome.status == "skipped"
    assert t.calls == []


def test_second_push_of_same_events_is_idempotent():
    t = FakeTransport()
    config = cfg(with_image=False)
    ledger = AlertLedger()
    push_events([event(1, "高"), event(2, "高")], config, ledger, transport=t)
    first_calls = len(t.calls)
    assert first_calls == 2
    push_events([event(1, "高"), event(2, "高")], config, ledger, transport=t)
    assert len(t.calls) == first_calls, "重复推送同一批事件不该再发"
    assert ledger.counts()["sent"] == 2


def test_max_per_run_caps_and_prioritises_highest():
    t = FakeTransport()
    events = [
        event(1, "中"),
        event(2, "高"),
        event(3, "中"),
        event(4, "高"),
        event(5, "高"),
    ]
    # 阈值放到「低」，让五个事件都在争上限——否则「中」会先被阈值挡掉，
    # 测到的就不是上限逻辑了
    ledger = push_events(
        events,
        cfg(min_severity="低", max_per_run=2, with_image=False),
        AlertLedger(),
        transport=t,
    )
    counts = ledger.counts()
    assert counts["sent"] == 2
    assert counts["skip_reasons"].get("已达本次上限") == 3
    sent_sev = [o.severity for o in ledger.outcomes if o.status == "sent"]
    assert sent_sev == ["高", "高"], "撞上限时应优先推最严重的"


def test_up_limit_is_counted_after_success_only():
    """失败的推送不该占用上限名额——否则一条失败就把后面的挤掉了。"""
    calls = {"n": 0}

    def transport(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"errcode": 93000, "errmsg": "boom"}
        return {"errcode": 0, "errmsg": "ok"}

    events = [event(1, "高"), event(2, "高")]
    ledger = push_events(
        events, cfg(max_per_run=1, with_image=False), AlertLedger(), transport=transport
    )
    counts = ledger.counts()
    assert counts["failed"] == 1
    assert counts["sent"] == 1, "第一失败后，第二条仍应有机会推送"


# ───────────────────────── 5. 卡片内容 ─────────────────────────


def test_card_contains_required_fields():
    card = render_event_card(
        event(7, "高"),
        video_name="burstling_street.mp4",
        generated_at="2026-09-21 15:00:00",
    )
    for needle in [
        "事件 7",
        "高",
        "burstling_street.mp4",
        "00:10.0",
        "100–120",
        "0.517",
        "现场描述",
        "处置建议",
        "立即派警到场处置",
    ]:
        assert needle in card, f"卡片缺少 {needle!r}"
    assert "|---" not in card, "企微 markdown 不支持表格，不许出现表头分隔行"
    assert card.count("\n") >= 6


def test_card_uses_warning_color_for_high():
    assert '<font color="warning">高</font>' in render_event_card(event(1, "高"))
    assert "font" not in render_event_card(event(1, "中")).split("**现场描述**")[0]


def test_card_without_observations_does_not_crash():
    broken = {
        "event_id": 3,
        "start_sec": 1.0,
        "end_sec": 2.0,
        "start_frame": 1,
        "end_frame": 2,
        "event_severity": "高",
        "event_severity_levels": ["高"],
        "event_severity_frame": 5,
    }
    card = render_event_card(broken)
    assert "事件 3" in card
    assert "—" in card


def test_card_shows_divergence_when_levels_differ():
    ev = event(1, "高")
    ev["event_severity_levels"] = ["中", "高"]
    assert "中 → 高" in render_event_card(ev)


def test_advice_and_disclaimer_match_frontend_locale():
    """群里说的一套、屏上说的一套是最容易被忽略的不一致。"""
    locale = json.loads(
        (ROOT / "web" / "src" / "locales" / "zh-CN.json").read_text(encoding="utf-8")
    )
    assert alerting.ADVICE_NOTE == locale["advice"]["note"]
    assert alerting.DISCLAIMER == locale["brief"]["disclaimer"]
    for sev, key in (("高", "high"), ("中", "medium"), ("低", "low")):
        assert alerting.ADVICE[sev] == locale["advice"][key], f"{sev} 的处置建议与前端不一致"


def test_advice_shown_never_exceeds_available():
    for sev, items in alerting.ADVICE.items():
        assert alerting.ADVICE_SHOWN[sev] <= len(items)


# ───────────────────────── 6. 内容长度与多字节 ─────────────────────────


def test_truncate_respects_byte_limit_without_splitting_chars():
    text = "汉" * 5000
    out = alerting._truncate_bytes(text, 100)
    assert len(out.encode("utf-8")) <= 100
    assert out.endswith("…")
    assert "\ufffd" not in out, "不能切碎多字节字符"


def test_truncate_leaves_short_text_untouched():
    assert alerting._truncate_bytes("短文本", 100) == "短文本"


def test_long_description_still_fits_markdown_limit():
    ev = event(1, "高")
    ev["observations"][0]["description"] = "很长的描述。" * 2000
    t = FakeTransport()
    push_events([ev], cfg(with_image=False), AlertLedger(), transport=t)
    content = t.calls[0]["markdown"]["content"]
    assert len(content.encode("utf-8")) <= alerting.MAX_MARKDOWN_BYTES
    assert "\ufffd" not in content


def test_payload_shape_is_wecom_markdown():
    t = FakeTransport()
    push_events([event(1, "高")], cfg(with_image=False), AlertLedger(), transport=t)
    payload = t.calls[0]
    assert payload["msgtype"] == "markdown"
    assert set(payload["markdown"]) == {"content"}
    assert payload["markdown"]["content"].strip()


# ───────────────────────── 7. 附图 ─────────────────────────


def _fake_png_bytes() -> bytes:
    # 内容是什么不重要：只走 base64/md5 与长度分支，不做图像解码
    return b"\x89PNG\r\n\x1a\n" + b"sky-eyes-test-payload" * 8


def test_image_md5_is_over_raw_bytes_not_base64():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roi.jpg"
        raw = _fake_png_bytes()
        path.write_bytes(raw)
        encoded, digest = alerting._encode_image(path)
        assert digest == hashlib.md5(raw).hexdigest()
        assert digest != hashlib.md5(encoded.encode()).hexdigest()
        assert base64.b64decode(encoded) == raw


def test_image_message_is_sent_after_text():
    with tempfile.TemporaryDirectory() as tmp:
        roi = Path(tmp) / "accident_frames"
        roi.mkdir()
        img = roi / "accident_frame_0100_roi.jpg"
        img.write_bytes(_fake_png_bytes())
        t = FakeTransport()
        ledger = push_events(
            [event(1, "高", rois=["accident_frames/accident_frame_0100_roi.jpg"])],
            cfg(with_image=True),
            AlertLedger(),
            transport=t,
            image_root=tmp,
        )
    assert [c["msgtype"] for c in t.calls] == ["markdown", "image"], "文字必须先发"
    assert ledger.outcomes[0].image_sent is True


def test_oversize_image_degrades_to_text_only():
    with tempfile.TemporaryDirectory() as tmp:
        roi = Path(tmp) / "accident_frames"
        roi.mkdir()
        (roi / "big.jpg").write_bytes(b"x" * (alerting.MAX_IMAGE_BYTES + 10))
        t = FakeTransport()
        ledger = push_events(
            [event(1, "高", rois=["accident_frames/big.jpg"])],
            cfg(),
            AlertLedger(),
            transport=t,
            image_root=tmp,
        )
    assert [c["msgtype"] for c in t.calls] == ["markdown"], "图超大时不该发出图片请求"
    outcome = ledger.outcomes[0]
    assert outcome.status == "sent", "文字卡片必须照发"
    assert outcome.image_sent is False
    assert "附图失败" in outcome.reason


def test_missing_roi_degrades_to_text_only():
    t = FakeTransport()
    ledger = push_events(
        [event(1, "高", rois=["accident_frames/not-there.jpg"])],
        cfg(),
        AlertLedger(),
        transport=t,
        image_root="/nonexistent",
    )
    assert [c["msgtype"] for c in t.calls] == ["markdown"]
    assert ledger.outcomes[0].status == "sent"
    assert "附图缺失" in ledger.outcomes[0].reason


def test_roi_path_rejects_non_image_suffix():
    with tempfile.TemporaryDirectory() as tmp:
        rogue = Path(tmp) / "accident_frames"
        rogue.mkdir()
        (rogue / "notes.txt").write_text("not an image", encoding="utf-8")
        ev = event(1, "高", rois=["accident_frames/notes.txt"])
        assert roi_path(ev, tmp) is None


# ───────────────────────── 8. 配置加载 ─────────────────────────


def test_config_file_is_read_and_env_overrides_it():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "alert.config.json"
        path.write_text(
            json.dumps({"webhook": "https://example.com/hook?key=filekey123456", "min_severity": "中"}),
            encoding="utf-8",
        )
        from_file = load_config(env={}, config_path=path)
        assert from_file.webhook.endswith("filekey123456")
        assert from_file.min_severity == "中"
        assert from_file.source == "file"

        from_env = load_config(
            env={"SKYEYES_WECOM_WEBHOOK": WEBHOOK, "SKYEYES_ALERT_MIN_SEVERITY": "低"},
            config_path=path,
        )
        assert from_env.webhook == WEBHOOK
        assert from_env.min_severity == "低"
        assert from_env.source == "env"

        from_arg = load_config(webhook="https://arg/hook", env={"SKYEYES_WECOM_WEBHOOK": WEBHOOK}, config_path=path)
        assert from_arg.webhook == "https://arg/hook"
        assert from_arg.source == "arg"


def test_broken_config_file_is_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "alert.config.json"
        path.write_text("{ this is not json", encoding="utf-8")
        config = load_config(env={}, config_path=path)
        assert config.enabled is False
        assert any("合法 JSON" in n for n in config.notes)


def test_invalid_threshold_falls_back_to_high():
    config = load_config(env={"SKYEYES_ALERT_MIN_SEVERITY": "严重"})
    assert config.min_severity == "高"
    assert any("合法等级" in n for n in config.notes)


def test_bool_and_int_parsing():
    assert load_config(env={"SKYEYES_ALERT_IMAGE": "0"}).with_image is False
    assert load_config(env={"SKYEYES_ALERT_IMAGE": "false"}).with_image is False
    assert load_config(env={"SKYEYES_ALERT_IMAGE": "1"}).with_image is True
    assert load_config(env={"SKYEYES_ALERT_MAX": "2"}).max_per_run == 2
    assert load_config(env={"SKYEYES_ALERT_MAX": "abc"}).max_per_run == alerting.DEFAULT_MAX_PER_RUN
    assert load_config(env={"SKYEYES_ALERT_MAX": "0"}).max_per_run == 1, "0 没有意义，下限为 1"
    assert load_config(env={"SKYEYES_ALERT_PROXY": "0"}).use_proxy is False


def test_describe_never_contains_full_key():
    assert KEY_FULL not in cfg().describe()
    assert "未配置" in load_config(env={}, config_path=Path("/nonexistent/x.json")).describe()


def test_report_summary_shape():
    t = FakeTransport()
    config = cfg(with_image=False)
    ledger = push_events([event(1, "高"), event(2, "低")], config, AlertLedger(), transport=t)
    summary = alerting.report_summary(config, ledger)
    assert summary["enabled"] is True
    assert summary["sent"] == 1
    assert summary["skipped"] == 1
    assert summary["min_severity"] == "高"
    assert "at" in summary


def test_render_test_card_has_no_secret_and_mentions_threshold():
    card = alerting.render_test_card(cfg(), at="2026-09-21 15:00:00")
    assert KEY_FULL not in card
    assert KEY_MIDDLE not in card
    assert "测试消息" in card
    assert "高" in card


# ───────────────────────── 9. 多通道（钉钉 / 飞书 / 邮件） ─────────────────────────
#
# 为什么要多通道：2026-09-21 实测发现，多数企业把「消息推送」的创建权限收在管理员手里，
# **即使你是群主也建不出来**。这条路被组织策略堵死时，不该让整条告警链失效。


def cfg_ch(channel, **kwargs):
    """指定通道的配置。默认带上一个像样的目标地址。"""
    urls = {
        CHANNEL_WECOM: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + KEY_FULL,
        CHANNEL_DINGTALK: "https://oapi.dingtalk.com/robot/send?access_token=" + KEY_FULL,
        CHANNEL_FEISHU: "https://open.feishu.cn/open-apis/bot/v2/hook/" + KEY_FULL,
        CHANNEL_SMTP: None,
    }
    defaults = {
        "webhook": urls[channel],
        "env": {},
        "config_path": Path(tempfile.gettempdir()) / "skyeyes-no-such-alert-config.json",
    }
    if channel == CHANNEL_SMTP:
        defaults.update(
            smtp_host="smtp.example.com",
            smtp_user="bot@example.com",
            smtp_password="app-password",
            smtp_to=("oncall@example.com",),
        )
    defaults.update(kwargs)
    return load_config(channel=channel, **defaults)


def test_dingtalk_payload_shape():
    payload = build_payload(cfg_ch(CHANNEL_DINGTALK), "## 标题\n正文")
    assert payload["msgtype"] == "markdown"
    assert set(payload["markdown"]) == {"title", "text"}, "钉钉的 markdown 必须带 title"
    assert payload["markdown"]["title"] == alerting.BRAND
    assert "正文" in payload["markdown"]["text"]


def test_feishu_payload_shape_is_text_message():
    payload = build_payload(cfg_ch(CHANNEL_FEISHU), "标题\n正文")
    assert payload["msg_type"] == "text"
    assert set(payload["content"]) == {"text"}
    assert "正文" in payload["content"]["text"]
    assert "sign" not in payload, "没配密钥时不该带签名字段"


def test_feishu_sign_goes_into_body_and_matches_algorithm():
    """飞书把签名放 **body**（钉钉放 URL），而且算法与钉钉不同——两者不能互相套用。"""
    import base64 as _b64
    import hashlib as _hash
    import hmac as _hmac

    secret = "feishu-secret-value"
    payload = build_payload(cfg_ch(CHANNEL_FEISHU, secret=secret), "x")
    assert "timestamp" in payload and "sign" in payload
    ts = payload["timestamp"]
    assert len(ts) == 10, "飞书时间戳是**秒**"
    expected = _b64.b64encode(
        _hmac.new(f"{ts}\n{secret}".encode(), b"", digestmod=_hash.sha256).digest()
    ).decode()
    assert payload["sign"] == expected


def test_dingtalk_sign_goes_into_url_and_matches_algorithm():
    """钉钉把签名放 **URL**，时间戳是**毫秒**，且签名内容与飞书不同。"""
    import base64 as _b64
    import hashlib as _hash
    import hmac as _hmac
    import urllib.parse as _url

    secret = "SEC-dingtalk-secret"
    config = cfg_ch(CHANNEL_DINGTALK, secret=secret)
    url = target_url(config)
    query = dict(_url.parse_qsl(_url.urlparse(url).query))
    assert "timestamp" in query and "sign" in query
    assert len(query["timestamp"]) == 13, "钉钉时间戳是**毫秒**"
    # 注意：签名进 URL 前做过 quote_plus，而 parse_qsl 会把它解回来，
    # 所以这里比对的是**未编码**的 base64 原文。
    expected = _b64.b64encode(
        _hmac.new(
            secret.encode(),
            f"{query['timestamp']}\n{secret}".encode(),
            digestmod=_hash.sha256,
        ).digest()
    ).decode()
    assert query["sign"] == expected
    # 顺带确认它确实进过 URL 编码（base64 里的 + / = 都该被转义）
    assert "%" in url or "+" not in query["sign"]


def test_dingtalk_sign_is_recomputed_each_call():
    """签名带时间戳，**必须每次现算**。缓存第一次的结果去重试，第二次必被判"签名过期"。"""
    config = cfg_ch(CHANNEL_DINGTALK, secret="SEC-abc")
    real_time = alerting.time.time
    try:
        alerting.time.time = lambda: 1_700_000_000.000
        first = target_url(config)
        alerting.time.time = lambda: 1_700_000_999.000
        second = target_url(config)
    finally:
        alerting.time.time = real_time
    assert first != second, "两次的 timestamp/sign 应当不同"


def test_dingtalk_without_secret_keeps_url_untouched():
    config = cfg_ch(CHANNEL_DINGTALK)
    assert target_url(config) == config.webhook


def test_feishu_errcode_is_not_accepted_as_success():
    """飞书看 `code`。给它一个只有 errcode 的响应**必须判失败**——否则会误报成功。"""
    t = FakeTransport(body={"errcode": 0, "errmsg": "ok"})
    ledger = push_events([event(1, "高")], cfg_ch(CHANNEL_FEISHU), AlertLedger(), transport=t)
    assert ledger.outcomes[0].status == "failed"
    assert "code" in ledger.outcomes[0].reason


def test_feishu_accepts_legacy_statuscode():
    t = FakeTransport(body={"StatusCode": 0, "StatusMessage": "success"})
    ledger = push_events([event(1, "高")], cfg_ch(CHANNEL_FEISHU), AlertLedger(), transport=t)
    assert ledger.outcomes[0].status == "sent"


def test_feishu_error_code_is_surfaced_with_message():
    t = FakeTransport(body={"code": 19024, "msg": "key Words Not Found"})
    ledger = push_events([event(1, "高")], cfg_ch(CHANNEL_FEISHU), AlertLedger(), transport=t)
    outcome = ledger.outcomes[0]
    assert outcome.status == "failed"
    assert "19024" in outcome.reason and "key Words" in outcome.reason


def test_wecom_body_with_only_code_is_not_success():
    """反向也要守：企微看 `errcode`，只给 `code` 同样判失败。"""
    t = FakeTransport(body={"code": 0, "msg": "success"})
    ledger = push_events([event(1, "高")], cfg_ch(CHANNEL_WECOM), AlertLedger(), transport=t)
    assert ledger.outcomes[0].status == "failed"


def test_image_only_sent_on_channels_that_support_it():
    """钉钉/飞书的自定义机器人发不了图。**只发一条文字，并如实标注**，
    不能让人以为"带了图"。"""
    # 飞书的成功响应长在 `code` 上，用企微那套 {"errcode":0} 会被正确判成失败——
    # 这里按通道给对应的响应体，顺带证明"判据是分通道的"这件事没写反。
    bodies = {
        CHANNEL_DINGTALK: {"errcode": 0, "errmsg": "ok"},
        CHANNEL_FEISHU: {"code": 0, "msg": "success"},
    }
    keys = {CHANNEL_DINGTALK: "msgtype", CHANNEL_FEISHU: "msg_type"}
    for channel in (CHANNEL_DINGTALK, CHANNEL_FEISHU):
        t = FakeTransport(body=bodies[channel])
        ledger = push_events(
            [event(1, "高", rois=["x.jpg"])], cfg_ch(channel), AlertLedger(), transport=t
        )
        assert len(t.calls) == 1, "发不了图的通道只该发一条文字消息"
        assert keys[channel] in t.calls[0]
        outcome = ledger.outcomes[0]
        assert outcome.status == "sent", outcome.reason
        assert outcome.image_sent is False
        assert "不支持附图" in outcome.reason


def test_non_image_channel_gets_a_note_at_config_time():
    """平台限制要让**配置阶段**就说清楚，别等人去猜为什么群里没图。"""
    config = cfg_ch(CHANNEL_FEISHU)
    assert config.image_supported is False
    assert config.wants_image is False
    assert any("不支持直接发图" in n for n in config.notes)


def test_unknown_channel_falls_back_with_note():
    config = load_config(channel="telegram", env={}, config_path=Path("/nonexistent/x.json"))
    assert config.channel == CHANNEL_WECOM
    assert any("不认识" in n for n in config.notes)


def test_channel_comes_from_env_and_file():
    from_file = load_config(env={}, config_path=_write_cfg({"channel": "dingtalk", "url": "https://d/x"}))
    assert from_file.channel == CHANNEL_DINGTALK
    assert from_file.webhook == "https://d/x"

    from_env = load_config(
        env={"SKYEYES_ALERT_CHANNEL": "feishu", "SKYEYES_ALERT_URL": "https://f/x"},
        config_path=Path("/nonexistent/x.json"),
    )
    assert from_env.channel == CHANNEL_FEISHU
    assert from_env.webhook == "https://f/x"


def test_legacy_wecom_env_still_works():
    """最初只有企微通道，文档里写的是 SKYEYES_WECOM_WEBHOOK。旧配置不能被改坏。"""
    config = load_config(env={"SKYEYES_WECOM_WEBHOOK": WEBHOOK}, config_path=Path("/nonexistent/x.json"))
    assert config.webhook == WEBHOOK
    assert config.channel == CHANNEL_WECOM


def test_mask_covers_feishu_path_token():
    """⚠ 飞书的密钥在 **URL 路径**里。只打码 `key=` 的话它会整段明文进日志。"""
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + KEY_FULL
    masked = mask_webhook(url)
    assert KEY_FULL not in masked
    assert KEY_MIDDLE not in masked
    assert masked.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/")


def test_mask_covers_dingtalk_access_token():
    url = f"https://oapi.dingtalk.com/robot/send?access_token={KEY_FULL}"
    masked = mask_webhook(url)
    assert KEY_FULL not in masked
    assert KEY_MIDDLE not in masked


def test_mask_keeps_short_path_segments_readable():
    """短路径段（send / hook）不该被误伤——出问题时要能看出配的是哪个接口。"""
    assert mask_webhook("https://example.com/plain/path") == "https://example.com/plain/path"
    assert "webhook/send" in mask_webhook(WEBHOOK)


def test_channel_view_and_ledger_never_expose_secrets():
    config = cfg_ch(CHANNEL_DINGTALK, secret="SEC-toplevel-secret")
    view = channel_view(config)
    blob = json.dumps(view, ensure_ascii=False)
    assert KEY_FULL not in blob
    assert "SEC-toplevel-secret" not in blob
    assert view["channel"] == CHANNEL_DINGTALK
    assert view["configured"] is True

    t = FakeTransport()
    ledger = push_events([event(1, "高")], config, AlertLedger(), transport=t)
    with tempfile.TemporaryDirectory() as tmp:
        path = write_ledger(Path(tmp) / "alerts.json", config, ledger)
        text = path.read_text(encoding="utf-8")
    assert KEY_FULL not in text
    assert "SEC-toplevel-secret" not in text
    assert json.loads(text)["channel"] == CHANNEL_DINGTALK


def test_plain_card_has_no_markdown_markers():
    card = render_event_card(
        event(7, "高"), video_name="v.mp4", generated_at="2026-09-21 22:00:00", style=alerting.CARD_PLAIN
    )
    assert "**" not in card
    assert "<font" not in card
    assert not any(line.startswith("#") for line in card.splitlines())
    assert "【" in card and "事件 7" in card


def test_dingtalk_card_has_no_font_color():
    """钉钉 markdown 不支持 font 颜色，带上会原样显示成标签文字。"""
    card = render_event_card(event(1, "高"), style=alerting.CARD_MARKDOWN)
    assert "<font" not in card
    assert "**高**" in card
    assert card.startswith("### "), "钉钉官方示例用 ### 作标题"


def test_smtp_not_enabled_without_host_or_recipients():
    assert load_config(channel=CHANNEL_SMTP, env={}, config_path=Path("/nonexistent/x.json")).enabled is False
    only_host = load_config(
        channel=CHANNEL_SMTP,
        smtp_host="smtp.example.com",
        env={},
        config_path=Path("/nonexistent/x.json"),
    )
    assert only_host.enabled is False
    assert any("收件人" in n for n in only_host.notes)


def test_smtp_uses_ssl_by_default_and_logs_in():
    config = cfg_ch(CHANNEL_SMTP)
    fake, module = _install_fake_smtplib()
    try:
        ok, detail = alerting._send_smtp(config, "主题", "正文")
    finally:
        _restore_smtplib(module)
    assert ok is True, detail
    sent = fake.instances[0]
    assert sent.host == "smtp.example.com"
    assert sent.creds == ("bot@example.com", "app-password")
    assert sent.starttls_used is False
    assert sent.messages[0]["Subject"] == "主题"
    assert sent.messages[0]["To"] == "oncall@example.com"


def test_smtp_starttls_path_is_used_when_configured():
    config = cfg_ch(CHANNEL_SMTP, smtp_starttls=True)
    fake, module = _install_fake_smtplib()
    try:
        alerting._send_smtp(config, "主题", "正文")
    finally:
        _restore_smtplib(module)
    assert fake.instances[0].starttls_used is True


def test_smtp_failure_is_reported_not_raised():
    config = cfg_ch(CHANNEL_SMTP)
    fake, module = _install_fake_smtplib(raise_on_connect=OSError("connection refused"))
    try:
        ok, detail = alerting._send_smtp(config, "主题", "正文")
    finally:
        _restore_smtplib(module)
    assert ok is False
    assert "connection refused" in detail


def test_smtp_attaches_scene_image():
    """邮件通道相比钉钉/飞书的一个便宜优势：图片可以当附件，白送一个"带图"。"""
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "roi.jpg"
        img.write_bytes(b"\xff\xd8\xff" + b"jpeg" * 4)
        config = cfg_ch(CHANNEL_SMTP)
        fake, module = _install_fake_smtplib()
        try:
            ok, detail = alerting._send_smtp(config, "主题", "正文", attachment=img)
        finally:
            _restore_smtplib(module)
    assert ok is True, detail
    message = fake.instances[0].messages[0]
    assert list(message.iter_attachments()), "现场图应当作为附件带上"
    assert message.get_content_type() == "multipart/mixed"


def test_smtp_masked_never_exposes_password():
    config = cfg_ch(CHANNEL_SMTP)
    described = config.describe()
    assert "app-password" not in described
    assert "oncall@example.com" in described
    assert KEY_FULL not in json.dumps(channel_view(config), ensure_ascii=False)


def test_smtp_push_flow_reports_image_as_attachment():
    with tempfile.TemporaryDirectory() as tmp:
        roi = Path(tmp) / "accident_frames"
        roi.mkdir()
        (roi / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 16)
        config = cfg_ch(CHANNEL_SMTP)
        fake, module = _install_fake_smtplib()
        try:
            ledger = push_events(
                [event(1, "高", rois=["accident_frames/a.jpg"])],
                config,
                AlertLedger(),
                image_root=tmp,
            )
        finally:
            _restore_smtplib(module)
    outcome = ledger.outcomes[0]
    assert outcome.status == "sent"
    assert outcome.image_sent is True
    assert len(fake.instances[0].messages) == 1, "邮件通道只发一封（图在附件里）"


def test_template_placeholder_is_treated_as_unconfigured():
    """直接复制模板却忘了填地址时，要在**配置阶段**就当成没配。

    否则 enabled 会是 True，等分析跑到一半才在日志里看到一个莫名其妙的连接错误——
    报错位置离真正的原因（模板没填）差着十万八千里。
    """
    config = load_config(env={}, config_path=ROOT / "alert.config.example.json")
    assert config.enabled is False
    assert any("占位符" in n for n in config.notes)


def test_report_summary_records_channel():
    t = FakeTransport()
    config = cfg_ch(CHANNEL_DINGTALK)
    ledger = push_events([event(1, "高")], config, AlertLedger(), transport=t)
    summary = alerting.report_summary(config, ledger)
    assert summary["channel"] == CHANNEL_DINGTALK
    assert summary["sent"] == 1


def _write_cfg(data: dict) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="skyeyes-alert-cfg-")) / "alert.config.json"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp


def _install_fake_smtplib(raise_on_connect=None):
    """把 smtplib 换成假的。

    `_send_smtp` 是在函数内部 `import smtplib` 的——所以替换 `sys.modules` 就能生效，
    不用改被测代码的打桩口。这样这个通道也能在**不联网**的情况下被完整验证。
    """
    import sys as _sys
    import types as _types

    class FakeSMTP:
        instances: list = []

        def __init__(self, host, port, timeout=None):
            if raise_on_connect is not None:
                raise raise_on_connect
            self.host, self.port, self.timeout = host, port, timeout
            self.messages: list = []
            self.creds = None
            self.starttls_used = False
            FakeSMTP.instances.append(self)

        def ehlo(self):
            pass

        def starttls(self):
            self.starttls_used = True

        def login(self, user, password):
            self.creds = (user, password)

        def send_message(self, message):
            self.messages.append(message)

        def quit(self):
            pass

    FakeSMTP.instances = []
    module = _types.ModuleType("smtplib")
    module.SMTP = FakeSMTP
    module.SMTP_SSL = FakeSMTP
    previous = _sys.modules.get("smtplib")
    _sys.modules["smtplib"] = module
    return FakeSMTP, previous


def _restore_smtplib(previous):
    import sys as _sys

    if previous is None:
        _sys.modules.pop("smtplib", None)
    else:
        _sys.modules["smtplib"] = previous


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}")
            print("     " + traceback.format_exc().replace("\n", "\n     ").strip())
    print()
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
