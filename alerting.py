"""告警推送：把「达标事件」推到处置方能看见的地方。

多通道
------
最初只支持企业微信群机器人。2026-09-21 实测发现：**多数企业把「消息推送」
的创建权限收在管理员手里**（管理后台 → 应用管理 → 消息推送 → "可创建消息推送的成员"），
普通成员——**即使他是群主**——也建不出来。这条路被组织策略堵死时，不该让整条告警链失效。

所以输出通道做成可插拔，内置四个：

| channel | 形态 | 图片 | 零审批 |
| --- | --- | --- | --- |
| `wecom` | 企业微信群机器人 | ✅ base64 直发 | ❌ 常需管理员放权 |
| `dingtalk` | 钉钉自定义机器人 | ❌ 无图片类型 | ✅ 群成员即可添加 |
| `feishu` | 飞书自定义机器人 | ❌ 需先上传换 image_key | ✅ 群成员即可添加 |
| `smtp` | 邮件 | ✅ 附件 | ✅ 只要有邮箱 |

**只有企微支持"文字卡片 + 现场图"两条消息**。钉钉/飞书的自定义机器人没有图片消息类型
（飞书要先调上传接口拿 `image_key`，那需要应用凭证，不是"填个 webhook"能解决的）。
这不是省略，是平台限制——`load_config` 会就此写一条 note，产物里也会如实标注。

守着七条线
----------
每条都有对应测试（`tests/test_alerting.py`）。它们存在的理由都一样：
**这条链上的失效几乎全是静默的，而错误后果落在系统之外——发进真实工作群的消息收不回来。**

1. **只推真正达标的等级**，且**绝不能拿中文等级比大小**。
   按 Unicode 码点是 `中`(U+4E2D) < `低`(U+4F4E) < `高`(U+9AD8)，
   写 `sev >= "高"` 会让「中」通过、让「高」不通过——反了。
   等级链上这个坑已经踩过三次（见 `tests/test_severity.py` 顶部），
   所以门控一律查显式映射表 `SEVERITY_ORDER`。
   「未判定」**永远不推送**：不知道有多严重，不是"不严重"。

2. **HTTP 200 不代表发送成功**。三家平台出错时都同样返回 200，
   真正的结论在 body 里（企微/钉钉是 `errcode`，飞书是 `code`）。
   只看状态码会把「webhook 地址填错了」记成「已推送」——
   演示现场的表现就是群里一条消息没有、大屏上却写着"已推送 1 条"。
   响应里**没有**该字段时同样判失败：宁可误报失败，不可误报成功。

3. **任何失败都不许中断分析**。推送是旁路：网络不通、显存被抢、ROI 图读不到，
   都不该让已经跑了 6 分钟的检测白跑。所有对外函数都不抛异常，只返回结果对象。

4. **地址里的凭证不许落进日志、报告或产物**（它等价于"任何人拿到都能以系统名义往群里发消息"）。
   各平台藏 key 的位置不一样，这是最容易漏的一处：
   - 企微：查询参数 `?key=`
   - 钉钉：查询参数 `?access_token=`
   - 飞书：**在 URL 路径里** `.../hook/xxxxxxxx`——只查 `key=` 会把它整段明文打出来
   - 加签密钥 `secret` 同理，任何情况下都不打印
   同理**不许走命令行参数**（`ps aux` 可见，而且 `server/app.py` 会把完整命令行
   写进落盘的 `job.log`）——只能来自环境变量或 `alert.config.json`。

5. **同一个事件在一次运行里只推一次**。`AlertLedger` 记录"已考虑过"的事件，
   重复调用是幂等的。一次分析里同一处事故连推三条是最伤可信度的表现。

6. **附图失败不影响文字卡片**。文字先行、图片后补，所以图片出问题时告警本身不丢。

7. **加签要按次重算**。钉钉/飞书的签名里带时间戳，重试时必须重新签——
   拿第一次算好的 URL 去重试，第二次必然被判"签名已过期"。

已知取舍（写下来，不是漏掉）
--------------------------
- `retries` 只在**没拿到平台明确答复**时重试（连接失败 / 超时 / 响应不是 JSON）。
  拿到明确的错误码一律不重试——那是配置问题，重试只会浪费配额。
  如果某次其实投递成功、只是回执丢了，重试会造成群里重复一条。
  **取舍偏向"宁可偶尔重复"**：这条链的意义是不漏报，而重复的告警人一眼能看出来。
- smtp 通道**不做重试**：邮件失败几乎都是配置问题（授权码错、端口不对、被安全策略拦），
  而邮件本来也不是"即时告警"通道，重试的收益低于把失败如实报出来。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ───────────────────────────── 配置 ─────────────────────────────
#
# 地址与密钥只能从环境变量或配置文件来，**刻意不提供命令行参数**（见模块顶部第 4 条）。

URL_ENV = "SKYEYES_ALERT_URL"
#: 兼容旧名（最初只有企微通道时用的）。新配置请用 SKYEYES_ALERT_URL。
WEBHOOK_ENV = "SKYEYES_WECOM_WEBHOOK"
CHANNEL_ENV = "SKYEYES_ALERT_CHANNEL"
SECRET_ENV = "SKYEYES_ALERT_SECRET"
MIN_SEVERITY_ENV = "SKYEYES_ALERT_MIN_SEVERITY"
IMAGE_ENV = "SKYEYES_ALERT_IMAGE"
MAX_ENV = "SKYEYES_ALERT_MAX"
RETRIES_ENV = "SKYEYES_ALERT_RETRIES"
TIMEOUT_ENV = "SKYEYES_ALERT_TIMEOUT"
PROXY_ENV = "SKYEYES_ALERT_PROXY"
CONFIG_ENV = "SKYEYES_ALERT_CONFIG"

SMTP_HOST_ENV = "SKYEYES_ALERT_SMTP_HOST"
SMTP_PORT_ENV = "SKYEYES_ALERT_SMTP_PORT"
SMTP_USER_ENV = "SKYEYES_ALERT_SMTP_USER"
SMTP_PASSWORD_ENV = "SKYEYES_ALERT_SMTP_PASSWORD"
SMTP_TO_ENV = "SKYEYES_ALERT_SMTP_TO"
SMTP_STARTTLS_ENV = "SKYEYES_ALERT_SMTP_STARTTLS"

DEFAULT_CONFIG_FILE = ROOT / "alert.config.json"

DEFAULT_CHANNEL = "wecom"
DEFAULT_MIN_SEVERITY = "高"
DEFAULT_MAX_PER_RUN = 5
DEFAULT_RETRIES = 2
DEFAULT_TIMEOUT = 8.0
DEFAULT_SMTP_PORT = 465

#: 重试前的等待基数（秒），随尝试次数线性增长。
#: 提到模块级是为了让测试能把它设成 0——否则守"会不会重试"的用例要真等 1.5 秒。
RETRY_BACKOFF = 1.5

# ───────────────────────────── 通道 ─────────────────────────────

CHANNEL_WECOM = "wecom"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_FEISHU = "feishu"
CHANNEL_SMTP = "smtp"
CHANNELS = (CHANNEL_WECOM, CHANNEL_DINGTALK, CHANNEL_FEISHU, CHANNEL_SMTP)

CHANNEL_LABEL = {
    CHANNEL_WECOM: "企业微信",
    CHANNEL_DINGTALK: "钉钉",
    CHANNEL_FEISHU: "飞书",
    CHANNEL_SMTP: "邮件",
}

#: 能带上现场图的通道。
#: 企微：图片消息直接塞 base64（微信侧上限 2MB）。
#: 邮件：作为附件，随正文一起发。
#: 钉钉自定义机器人**没有** image 消息类型；飞书要用 image_key，得先走上传接口拿应用凭证
#: ——都不是"填个 webhook"能解决的，所以这两个通道只发文字。
IMAGE_CHANNELS = (CHANNEL_WECOM, CHANNEL_SMTP)

#: 卡片风格。三家支持的 markdown 语法不一样：
#:   rich     企微：支持 <font color>，标题用 `##`
#:   markdown 钉钉：支持标题/加粗/引用/列表，**不支持 font 颜色**，官方示例用 `###`
#:   plain    飞书 text 消息只吃纯文本；邮件纯文本正文也用它
CARD_RICH = "rich"
CARD_MARKDOWN = "markdown"
CARD_PLAIN = "plain"

CHANNEL_CARD_STYLE = {
    CHANNEL_WECOM: CARD_RICH,
    CHANNEL_DINGTALK: CARD_MARKDOWN,
    CHANNEL_FEISHU: CARD_PLAIN,
    CHANNEL_SMTP: CARD_PLAIN,
}

# ───────────────────────────── 常量与口径 ─────────────────────────────

#: 等级排序。**唯一权威定义**（`tests/test_severity.py` 会核对它与 pipeline 里那份一致）。
#: 严禁用字符串比较代替它。
SEVERITY_ORDER = {"低": 0, "中": 1, "高": 2}

#: 企微 markdown 里能用的三种 font 颜色。没有颜色时用加粗代替——
#: 刻意不给「中」上色：上色会让它和「高」在群里长得一样显眼。
_SEV_COLOR = {"高": "warning", "中": None, "低": "comment"}

#: 卡片长度上限。三家实际限额不同（企微 markdown 4096 字节、钉钉 20KB、飞书 40KB），
#: 取最严的那个做统一上限，既安全又省得按通道分支。
MAX_MARKDOWN_BYTES = 4096
#: 企微图片上限。钉钉/飞书不走这条路（见 IMAGE_CHANNELS）。
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}

#: 卡片里描述文字的截断长度。4096 字节 ≈ 1300 个汉字，留足余量。
MAX_DESC_CHARS = 400

#: 处置建议。**与 `web/src/locales/zh-CN.json` 的 `advice.*` 逐字一致**
#: （test_alerting.py 会去读那个文件核对）。群里说的和屏上说的不能是两套话。
ADVICE = {
    "高": [
        "立即派警到场处置",
        "同步通知急救（120）",
        "必要时实施现场交通管制",
        "优先调度最近警力",
    ],
    "中": [
        "派警到场核实情况",
        "引导车流绕行，避免二次事故",
        "视人员受伤情况通知急救",
    ],
    "低": [
        "记录归档，纳入事故台账",
        "调用周边视频回溯事故过程",
        "视情况通知当事人自行处理",
    ],
}
#: 群消息里不应把三条建议都铺开，前几条足够（与 `advice.note` 同源）
ADVICE_SHOWN = {"高": 3, "中": 2, "低": 2}
ADVICE_NOTE = "按严重等级规则生成，需人工确认"
#: 与 `brief.disclaimer` 一致
DISCLAIMER = "本简报由系统自动生成，事故描述来自多模态模型输出，仅供处置参考，不作为责任认定依据。"

BRAND = "Sky Eyes 事故告警"

#: 查询参数里属于"凭证"的键名，一律打码。
_SECRET_QUERY_KEYS = {"key", "access_token", "token", "secret", "sign", "api_key", "apikey"}

#: 配置模板里的占位标记。命中就把地址当"没填"处理。
#:
#: 为什么需要这个：`alert.config.example.json` 里的 url 是个非空字符串，
#: 直接复制成 `alert.config.json` 却不填真实地址时，`enabled` 会是 True——
#: 于是分析跑到一半才在日志里看到一个不知所云的连接错误。
#: 在这里拦下，报"地址还是占位符，没真填"要直白得多。
_PLACEHOLDER_MARKERS = ("在此填入", "<你的", "TODO", "your-", "xxxx")


# ───────────────────────────── 配置读取 ─────────────────────────────


def _mask_value(value: str) -> str:
    if len(value) <= 10:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


def mask_webhook(url: str | None) -> str | None:
    """把地址里的凭证打码，用于日志、报告与界面。

    ⚠ **必须按平台分别处理**，这是本模块最容易漏的一处：

    - 企微：`?key=xxx`
    - 钉钉：`?access_token=xxx`（加签时还有 `&sign=`）
    - 飞书：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`——**密钥在路径里**。
      只写 `re.sub(r"key=...")` 的话，飞书地址会被**整段明文**打进日志和产物。

    所以这里做两件事：① 打码所有凭证类查询参数；② 打码路径末尾那一段看起来像 token 的
    （长度 ≥ 20 且只含 token 字符）。短路径段（如 `send`、`hook`）保持原样，
    这样出问题时还能看出"配的是哪个平台的哪个接口"。
    """
    if not url:
        return None
    text = str(url).strip()

    pattern = r"(?i)(\b(" + "|".join(sorted(_SECRET_QUERY_KEYS)) + r")=)([^&\s]+)"
    text = re.sub(
        pattern,
        lambda m: m.group(1) + _mask_value(m.group(3)),
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1", text)  # 去掉 userinfo

    # 路径末尾的 token（飞书那种）。只认"够长且像 token"，避免误伤 /send、/hook 这类段。
    def _mask_path(match: re.Match) -> str:
        head, segment, tail = match.group(1), match.group(2), match.group(3) or ""
        if len(segment) >= 20 and re.fullmatch(r"[A-Za-z0-9_\-]+", segment):
            return f"{head}{_mask_value(segment)}{tail}"
        return match.group(0)

    text = re.sub(r"^([^?#]*/)([^/?#]+)([?#].*)?$", _mask_path, text)
    return text


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return default
    return text not in {"0", "false", "no", "off"}


def _read_config_file(path: Path) -> dict:
    """读配置文件。**读不到或格式错都不该让流程失败**，返回空 dict 即可。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"__error__": f"{path.name} 不是合法 JSON，已忽略"}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class AlertConfig:
    #: wecom | dingtalk | feishu | smtp
    channel: str = DEFAULT_CHANNEL
    #: 目标地址（wecom/dingtalk/feishu）。smtp 通道不用它。
    webhook: str | None = None
    #: 加签密钥（dingtalk / feishu 选了"加签"安全设置时需要）
    secret: str | None = None
    min_severity: str = DEFAULT_MIN_SEVERITY
    with_image: bool = True
    max_per_run: int = DEFAULT_MAX_PER_RUN
    retries: int = DEFAULT_RETRIES
    timeout: float = DEFAULT_TIMEOUT
    use_proxy: bool = True
    #: 配置从哪来（env / file / default），自检时打印出来，省得猜"为什么没生效"
    source: str = "default"
    #: 配置本身的问题或平台限制，非致命，但要让人看见
    notes: tuple[str, ...] = ()

    # ---- smtp ----
    smtp_host: str | None = None
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_to: tuple[str, ...] = ()
    smtp_starttls: bool = False

    @property
    def enabled(self) -> bool:
        """没配通道就是关闭状态。**此时不允许发起任何网络请求。**"""
        if self.channel == CHANNEL_SMTP:
            return bool(self.smtp_host and self.smtp_user and self.smtp_to)
        return bool(self.webhook)

    @property
    def image_supported(self) -> bool:
        return self.channel in IMAGE_CHANNELS

    @property
    def wants_image(self) -> bool:
        """真的会发图（配置开了**且**通道支持）。"""
        return bool(self.with_image and self.image_supported)

    @property
    def card_style(self) -> str:
        return CHANNEL_CARD_STYLE.get(self.channel, CARD_PLAIN)

    @property
    def masked(self) -> str | None:
        """对外可见的目标描述。**密钥一律打码**，smtp 只露用户名域。"""
        if self.channel == CHANNEL_SMTP:
            if not self.smtp_host:
                return None
            return f"{self.smtp_host}:{self.smtp_port} → {', '.join(self.smtp_to) or '—'}"
        return mask_webhook(self.webhook)

    @property
    def label(self) -> str:
        return CHANNEL_LABEL.get(self.channel, self.channel)

    def describe(self) -> str:
        if not self.enabled:
            return f"未配置{self.label}通道（告警推送关闭）"
        parts = [
            f"{self.label} {self.masked}",
            f"阈值 {self.min_severity}",
            f"附图 {'开' if self.wants_image else ('关' if not self.with_image else '不支持')}",
            f"每次上限 {self.max_per_run}",
            f"来源 {self.source}",
        ]
        return " ｜ ".join(parts)


def load_config(
    *,
    channel: str | None = None,
    webhook: str | None = None,
    secret: str | None = None,
    min_severity: str | None = None,
    with_image: bool | None = None,
    max_per_run: int | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    use_proxy: bool | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    smtp_to: str | list | tuple | None = None,
    smtp_starttls: bool | None = None,
    config_path: Path | str | None = None,
    env: dict | None = None,
) -> AlertConfig:
    """按 `显式参数 > 环境变量 > 配置文件 > 内置默认` 的优先级拼出配置。

    `env` 参数存在的意义是测试：传 `{}` 就能完全隔离掉跑测试那台机器的环境变量，
    否则"本机恰好设了 SKYEYES_ALERT_URL"会让用例结果不可复现。
    """
    environ = os.environ if env is None else env
    path = Path(config_path or environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE)
    file_cfg = _read_config_file(path)
    notes: list[str] = []
    if "__error__" in file_cfg:
        notes.append(file_cfg.pop("__error__"))

    def pick(explicit, env_key: str, file_key: str, default):
        if explicit is not None:
            return explicit, "arg"
        raw = environ.get(env_key)
        if raw not in (None, ""):
            return raw, "env"
        if file_key in file_cfg and file_cfg[file_key] not in (None, ""):
            return file_cfg[file_key], "file"
        return default, "default"

    raw_channel, chan_src = pick(channel, CHANNEL_ENV, "channel", DEFAULT_CHANNEL)
    chan = str(raw_channel).strip().lower()
    if chan not in CHANNELS:
        notes.append(f"通道 {chan!r} 不认识，已退回 {DEFAULT_CHANNEL}（可选：{'/'.join(CHANNELS)}）")
        chan = DEFAULT_CHANNEL

    # 地址：新名优先，旧名兜底（最初只有企微通道，文档与脚本里可能还写着旧名）
    raw_url, url_src = pick(webhook, URL_ENV, "url", None)
    if url_src == "default":
        legacy = environ.get(WEBHOOK_ENV) or file_cfg.get("webhook")
        if legacy:
            raw_url, url_src = legacy, "env" if environ.get(WEBHOOK_ENV) else "file"

    raw_secret, _ = pick(secret, SECRET_ENV, "secret", None)
    raw_min, min_src = pick(min_severity, MIN_SEVERITY_ENV, "min_severity", DEFAULT_MIN_SEVERITY)
    raw_image, _ = pick(with_image, IMAGE_ENV, "with_image", True)
    raw_max, _ = pick(max_per_run, MAX_ENV, "max_per_run", DEFAULT_MAX_PER_RUN)
    raw_retries, _ = pick(retries, RETRIES_ENV, "retries", DEFAULT_RETRIES)
    raw_timeout, _ = pick(timeout, TIMEOUT_ENV, "timeout", DEFAULT_TIMEOUT)
    raw_proxy, _ = pick(use_proxy, PROXY_ENV, "use_proxy", True)

    min_sev = str(raw_min).strip()
    if min_sev not in SEVERITY_ORDER:
        notes.append(f"阈值 {min_sev!r} 不是合法等级，已退回 {DEFAULT_MIN_SEVERITY}")
        min_sev = DEFAULT_MIN_SEVERITY

    def _int(value, default: int, lowest: int = 0) -> int:
        try:
            return max(lowest, int(value))
        except (TypeError, ValueError):
            return default

    def _float(value, default: float) -> float:
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return default

    with_image_bool = _as_bool(raw_image, True)
    if with_image_bool and chan not in IMAGE_CHANNELS:
        notes.append(
            f"{CHANNEL_LABEL.get(chan, chan)}的自定义机器人不支持直接发图"
            "（图片消息类型不可用），本次只发文字卡片"
        )

    # 占位符还是占位符 → 当没填（见 _PLACEHOLDER_MARKERS 的说明）
    resolved_url = (str(raw_url).strip() or None) if raw_url else None
    if resolved_url and any(marker in resolved_url for marker in _PLACEHOLDER_MARKERS):
        notes.append(
            "配置里的目标是模板占位符，还没填真实地址——已按「未配置」处理。"
            "请把 alert.config.json 里的 url 换成群设置里复制出来的那个"
        )
        resolved_url = None

    # ---- smtp ----
    smtp_host, smtp_src = pick(smtp_host, SMTP_HOST_ENV, "smtp_host", None)
    smtp_user, _ = pick(smtp_user, SMTP_USER_ENV, "smtp_user", None)
    smtp_password, _ = pick(smtp_password, SMTP_PASSWORD_ENV, "smtp_password", None)
    smtp_port_raw, _ = pick(smtp_port, SMTP_PORT_ENV, "smtp_port", DEFAULT_SMTP_PORT)
    smtp_to_raw, _ = pick(smtp_to, SMTP_TO_ENV, "smtp_to", None)
    smtp_tls_raw, _ = pick(smtp_starttls, SMTP_STARTTLS_ENV, "smtp_starttls", False)

    if isinstance(smtp_to_raw, str):
        smtp_to = tuple(x.strip() for x in re.split(r"[,;\s]+", smtp_to_raw) if x.strip())
    elif isinstance(smtp_to_raw, (list, tuple)):
        smtp_to = tuple(str(x).strip() for x in smtp_to_raw if str(x).strip())
    else:
        smtp_to = ()

    if chan == CHANNEL_SMTP:
        if not smtp_host:
            notes.append("smtp 通道缺少 smtp_host，无法发送")
        if not smtp_to:
            notes.append("smtp 通道缺少收件人 smtp_to，无法发送")
        if not smtp_password:
            notes.append("smtp 通道没有密码/授权码，多数邮箱会拒绝登录")

    # 来源只作诊断用（"为什么它没生效"）。优先报通道/地址的来源。
    source = chan_src if chan_src != "default" else (url_src if url_src != "default" else (smtp_src if smtp_src != "default" else min_src))

    return AlertConfig(
        channel=chan,
        webhook=resolved_url,
        secret=(str(raw_secret).strip() or None) if raw_secret else None,
        min_severity=min_sev,
        with_image=with_image_bool,
        max_per_run=_int(raw_max, DEFAULT_MAX_PER_RUN, lowest=1),
        retries=_int(raw_retries, DEFAULT_RETRIES, lowest=0),
        timeout=_float(raw_timeout, DEFAULT_TIMEOUT),
        use_proxy=_as_bool(raw_proxy, True),
        source=source,
        notes=tuple(notes),
        smtp_host=(str(smtp_host).strip() or None) if smtp_host else None,
        smtp_port=_int(smtp_port_raw, DEFAULT_SMTP_PORT, lowest=1),
        smtp_user=(str(smtp_user).strip() or None) if smtp_user else None,
        smtp_password=(str(smtp_password) or None) if smtp_password else None,
        smtp_to=smtp_to,
        smtp_starttls=_as_bool(smtp_tls_raw, False),
    )


# ───────────────────────────── 加签 ─────────────────────────────


def _dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    """钉钉加签：以 secret 为密钥，对 `timestamp\\nsecret` 做 HMAC-SHA256 → Base64 → urlencode。

    与飞书的算法**不一样**（见下），两者不能互相套用。时间戳是**毫秒**。
    """
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def _feishu_sign(secret: str, timestamp_s: str) -> str:
    """飞书加签：以 `timestamp\\nsecret` 为密钥，对**空串**做 HMAC-SHA256 → Base64。

    与钉钉的关键差异：密钥与消息体互换了位置，且时间戳是**秒**。
    飞书把 sign 放在请求 body 里（钉钉放在 URL 查询参数里）。
    """
    key = f"{timestamp_s}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def target_url(config: AlertConfig) -> str | None:
    """算出发送目标地址（含按当前时间生成的签名）。

    ⚠ 每次发送都要重新调用：加签里带时间戳，平台只接受 1 小时内的签名。
    把第一次的结果缓存起来重试，第二次必然被判"签名已过期"。
    """
    if config.channel == CHANNEL_SMTP or not config.webhook:
        return None
    if config.channel != CHANNEL_DINGTALK or not config.secret:
        return config.webhook
    timestamp = str(round(time.time() * 1000))
    sign = _dingtalk_sign(config.secret, timestamp)
    joiner = "&" if "?" in config.webhook else "?"
    return f"{config.webhook}{joiner}timestamp={timestamp}&sign={sign}"


def build_payload(config: AlertConfig, card: str) -> dict:
    """按通道拼请求体。**四个平台的形状都不一样**，别想着一份 body 打天下。"""
    card = _truncate_bytes(card, MAX_MARKDOWN_BYTES)
    if config.channel == CHANNEL_DINGTALK:
        return {
            "msgtype": "markdown",
            "markdown": {"title": BRAND, "text": card},
        }
    if config.channel == CHANNEL_FEISHU:
        payload: dict = {"msg_type": "text", "content": {"text": card}}
        if config.secret:
            # 飞书的签名放在 body 里，不在 URL 上
            timestamp = str(round(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _feishu_sign(config.secret, timestamp)
        return payload
    # wecom（默认）
    return {"msgtype": "markdown", "markdown": {"content": card}}


# ───────────────────────────── 传输层 ─────────────────────────────


def _default_transport(config: AlertConfig):
    """构造一个「把 payload POST 到目标地址」的可调用对象。

    用标准库 urllib 而不是 requests/httpx：这个模块要能被两套环境同时导入——
    `pipeline.py` 跑的 visual_search 环境（有 requests）与 `server/.venv`
    （只有 fastapi/uvicorn/httpx，**不装 torch、也不装 requests**）。
    引第三方库就会把两边都绑死。

    代理行为跟随环境变量（`HTTPS_PROXY` 等），可用 `SKYEYES_ALERT_PROXY=0` 关掉。
    这里**刻意不默认关代理**——与探测 Janus 时相反：Janus 是本机环回，
    走代理必错；而这几家都是外网地址，公司网络里往往正需要代理。

    ⚠ URL 在**每次调用时**现算（见 `target_url`），因为钉钉的签名带时间戳。
    """

    def post(payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            target_url(config),
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        if config.use_proxy:
            opener = urllib.request.build_opener()
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=config.timeout) as response:
            body = response.read().decode("utf-8", "replace")
        return json.loads(body)

    return post


def _verdict_of(body, channel: str) -> tuple[bool, str]:
    """从平台的响应里读出结论。

    ⚠ 这是本模块最容易写错的一处：**HTTP 200 不等于发送成功**。
    三家平台在地址无效、频率超限、内容为空、关键词不匹配时都照样返回 200，
    真正的结论在 body 里：企微/钉钉看 `errcode`，飞书看 `code`（兼容旧的 `StatusCode`）。

    响应里**没有**该字段时判失败——宁可误报失败，不可误报成功。
    只判断 HTTP 状态码的话，"地址填错了"会被记成"已推送"。
    """
    if not isinstance(body, dict):
        return False, f"响应不是 JSON 对象（{type(body).__name__}）"

    if channel == CHANNEL_FEISHU:
        for key in ("code", "StatusCode"):
            if key in body:
                code = body[key]
                if code in (0, "0"):
                    return True, "ok"
                message = str(body.get("msg") or body.get("StatusMessage") or "").strip()
                return False, f"{key}={code} {message}".strip()
        preview = json.dumps(body, ensure_ascii=False)[:120]
        return False, f"响应里没有 code：{preview}"

    code = body.get("errcode")
    if code in (0, "0"):
        return True, "ok"
    if code is None:
        preview = json.dumps(body, ensure_ascii=False)[:120]
        return False, f"响应里没有 errcode：{preview}"
    errmsg = str(body.get("errmsg") or "").strip()
    return False, f"errcode={code} {errmsg}".strip()


def _call(config: AlertConfig, payload: dict, transport) -> tuple[bool, str]:
    """发一次消息。**永不抛异常**，只返回 (是否成功, 说明)。

    重试策略见模块顶部「已知取舍」：只有拿不到明确答复（连接失败/超时/非 JSON）
    才重试；明确的错误码是配置问题，重试没有意义。
    """
    attempts = max(1, int(config.retries) + 1)
    detail = "未发起请求"
    for attempt in range(attempts):
        try:
            body = transport(payload)
        except Exception as exc:  # noqa: BLE001 —— 旁路功能，任何异常都不该外泄
            detail = f"请求失败：{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(min(RETRY_BACKOFF * (attempt + 1), 4.0))
                continue
            return False, detail
        return _verdict_of(body, config.channel)
    return False, detail


def _truncate_bytes(text: str, limit: int) -> str:
    """按**字符**边界把字符串截到不超过 limit 字节。

    直接 `raw[:limit]` 会切碎多字节汉字，群里显示成乱码问号。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    budget = max(0, limit - len("…".encode("utf-8")))
    out: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out) + "…"


def _encode_image(path: Path) -> tuple[str, str]:
    """返回 (base64, md5)。

    ⚠ `md5` 必须是**原始字节**的 md5，不是 base64 字符串的。
    企微会用它在服务端校验，传错的表现是"文字到了、图没到"。
    """
    raw = path.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"图片 {len(raw)} 字节，超过企微 2MB 上限")
    return base64.b64encode(raw).decode("ascii"), hashlib.md5(raw).hexdigest()


def _send_smtp(
    config: AlertConfig,
    subject: str,
    body: str,
    attachment: Path | None = None,
) -> tuple[bool, str]:
    """发一封邮件。**永不抛异常。**

    刻意不做重试（见模块顶部「已知取舍」）：邮件失败几乎都是配置问题
    （授权码错、端口不对、被安全策略拦），而邮件本来也不是即时告警通道。

    附件就是现场图——这是 smtp 相比钉钉/飞书的一个便宜优势：
    它们的自定义机器人发不了图，这里白送。
    """
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.smtp_user
    message["To"] = ", ".join(config.smtp_to)
    message.set_content(body)

    if attachment is not None:
        try:
            data = Path(attachment).read_bytes()
            message.add_attachment(
                data,
                maintype="image",
                subtype=Path(attachment).suffix.lstrip(".").lower() or "jpeg",
                filename=Path(attachment).name,
            )
        except Exception:  # noqa: BLE001 —— 附件失败不该拦住正文
            pass

    server = None
    try:
        if config.smtp_starttls:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=config.timeout)
        if config.smtp_user and config.smtp_password:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(message)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def send_card(
    config: AlertConfig,
    card: str,
    *,
    subject: str | None = None,
    transport=None,
    attachment: Path | None = None,
) -> tuple[bool, str]:
    """把一张卡片发出去。**永不抛异常。**"""
    card = (card or "").strip()
    if not card:
        return False, "内容为空，未发送"
    if config.channel == CHANNEL_SMTP:
        return _send_smtp(config, subject or BRAND, card, attachment=attachment)
    transport = transport or _default_transport(config)
    return _call(config, build_payload(config, card), transport)


def send_image(config: AlertConfig, path: Path, transport=None) -> tuple[bool, str]:
    """发一张图（仅企微）。失败**不影响**已经发出去的文字卡片。"""
    if config.channel != CHANNEL_WECOM:
        return False, f"{config.label}的自定义机器人不支持图片消息"
    try:
        encoded, digest = _encode_image(Path(path))
    except Exception as exc:  # noqa: BLE001
        return False, f"读取图片失败：{exc}"
    transport = transport or _default_transport(config)
    return _call(config, {"msgtype": "image", "image": {"base64": encoded, "md5": digest}}, transport)


# ───────────────────────────── 卡片渲染 ─────────────────────────────


def _fmt_time(seconds) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "—"
    minutes, rest = divmod(value, 60)
    return f"{int(minutes):02d}:{rest:04.1f}"


def _one_line(text, limit: int = MAX_DESC_CHARS) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _sev_text(severity: str, style: str) -> str:
    """按风格给等级上样式。

    只有企微支持 `<font color>`；钉钉的 markdown 不支持颜色；纯文本什么都没有。
    刻意不给「中」上色：上色会让它和「高」在群里长得一样显眼。
    """
    if style == CARD_RICH:
        color = _SEV_COLOR.get(severity)
        return f'<font color="{color}">{severity}</font>' if color else f"**{severity}**"
    if style == CARD_MARKDOWN:
        return f"**{severity}**"
    return severity


def _lead_observation(event: dict) -> dict | None:
    """挑出代表这个事件的观察记录：优先取决定等级的那一帧，否则第一个成功的。"""
    frame = event.get("event_severity_frame")
    for obs in event.get("observations", []):
        if obs.get("ok") and obs.get("frame_index") == frame:
            return obs
    for obs in event.get("observations", []):
        if obs.get("ok"):
            return obs
    return None


def roi_path(event: dict, image_root: Path | str | None) -> Path | None:
    """解析事件代表帧的 ROI 图片路径。找不到就返回 None（不是异常）。"""
    obs = _lead_observation(event)
    if not obs:
        return None
    roi = obs.get("roi")
    if not roi:
        return None
    path = Path(str(roi))
    if not path.is_absolute():
        if image_root is None:
            return None
        path = Path(image_root) / path
    if path.suffix.lower() not in MAX_IMAGE_SUFFIXES:
        return None
    return path if path.is_file() else None


def _event_facts(event: dict) -> dict:
    """把事件里要展示的字段抽出来，三种风格共用，免得三个分支各算一遍、算得不一样。"""
    severity = event.get("event_severity") or "未判定"
    lead = _lead_observation(event) or {}
    levels = [x for x in (event.get("event_severity_levels") or []) if x]
    confidence = event.get("peak_confidence")
    return {
        "severity": severity,
        "divergence": f"（各代表帧 {' → '.join(levels)}）" if len(set(levels)) > 1 else "",
        "event_id": event.get("event_id"),
        "window": (
            f"{_fmt_time(event.get('start_sec'))} – {_fmt_time(event.get('end_sec'))}"
            f" ｜ 帧 {event.get('start_frame')}–{event.get('end_frame')}"
        ),
        "confidence": f"{float(confidence):.3f}" if isinstance(confidence, (int, float)) else "—",
        "frame": event.get("event_severity_frame"),
        "desc": _one_line(lead.get("description")) or "—",
        "advice": ADVICE.get(severity, [])[: ADVICE_SHOWN.get(severity, 2)],
    }


def render_event_card(
    event: dict,
    *,
    video_name: str = "",
    generated_at: str | None = None,
    style: str = CARD_RICH,
) -> str:
    """把一起事故渲染成告警卡片。

    ⚠ 三家平台的语法能力不同，所以分成三种风格（见本轮改动说明）：
      - `rich`：企微。标题 / 引用 / 加粗 / 列表 / `<font color>`。**不支持表格**
      - `markdown`：钉钉。标题用 `###`，**没有 font 颜色**
      - `plain`：飞书 text 消息与邮件正文。**只吃纯文本**
    分支之间共享 `_event_facts()`，避免"同一份数据在三条路径上算得不一样"。
    """
    f = _event_facts(event)
    sev = _sev_text(f["severity"], style)
    advice = f["advice"]

    if style == CARD_PLAIN:
        lines = [
            f"【{BRAND} · {f['severity']}】",
            f"自动检测 ｜ {generated_at or ''}",
            f"视频源 {video_name or '—'}",
            "",
            f"事件 {f['event_id']} ｜ 严重等级 {f['severity']}{f['divergence']}",
            f"时间段 {f['window']} ｜ 峰值置信度 {f['confidence']}",
        ]
        if f["frame"] is not None:
            lines.append(f"判定帧 {f['frame']}（事件级等级取自该帧）")
        lines += ["", "现场描述", f["desc"], "", f"处置建议（{ADVICE_NOTE}）"]
        lines += [f"{i}. {a}" for i, a in enumerate(advice, 1)]
        lines += ["", DISCLAIMER]
        return "\n".join(lines)

    head = "##" if style == CARD_RICH else "###"
    lines = [
        f"{head} {BRAND} · {f['severity']}",
        f"> 自动检测 ｜ {generated_at or ''}",
        f"> 视频源 {video_name or '—'}",
        "",
        f"**事件 {f['event_id']}** ｜ **严重等级 {sev}**{f['divergence']}",
        f"**时间段** {f['window']} ｜ 峰值置信度 {f['confidence']}",
    ]
    if f["frame"] is not None:
        lines.append(f"**判定帧** {f['frame']}（事件级等级取自该帧）")
    lines += ["", "**现场描述**", f["desc"], "", f"**处置建议**（{ADVICE_NOTE}）"]
    lines += [f"- {a}" for a in advice]
    lines += ["", f"> {DISCLAIMER}"]
    return "\n".join(lines)


def render_test_card(config: AlertConfig, *, at: str | None = None) -> str:
    """通道自检卡片。**演示前必看**——不要等到现场才发现地址填错了。"""
    style = config.card_style
    stamp = at or time.strftime("%Y-%m-%d %H:%M:%S")
    facts = [
        f"阈值 {config.min_severity} ｜ 附图 {'开' if config.wants_image else '关'}"
        f" ｜ 每次上限 {config.max_per_run}",
    ]
    if style == CARD_PLAIN:
        return "\n".join(
            [
                f"【{BRAND} · 通道自检】",
                stamp,
                "",
                "这是一条测试消息，用来确认告警通道配置正确。",
                "如果你看到它，说明通道已经打通；分析中触发高等级事件时会推送同类卡片。",
                "",
                f"通道 {config.label}",
                f"目标 {config.masked or '未配置'}",
                *facts,
                "",
                DISCLAIMER,
            ]
        )
    head = "##" if style == CARD_RICH else "###"
    return "\n".join(
        [
            f"{head} {BRAND} · 通道自检",
            f"> {stamp}",
            "",
            "这是一条**测试消息**，用来确认告警通道配置正确。",
            "如果你看到它，说明通道已经打通；分析中触发高等级事件时会推送同类卡片。",
            "",
            f"**通道** {config.label}",
            f"**目标** {config.masked or '未配置'}",
            f"**{facts[0]}**",
            "",
            f"> {DISCLAIMER}",
        ]
    )


# ───────────────────────────── 推送编排 ─────────────────────────────


@dataclass
class AlertOutcome:
    """一次推送尝试（或一次"为什么不推"）的结果。会写进 `alerts.json`。"""

    kind: str  # event | test | config
    status: str  # sent | failed | skipped
    reason: str
    event_id: int | None = None
    severity: str | None = None
    text_sent: bool = False
    image_sent: bool | None = None
    at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlertLedger:
    """一次运行内的推送台账。

    `_considered` 记录**已经处理过**的事件 id（无论成功、失败还是跳过），
    因此对同一批事件重复调用 `push_events()` 是幂等的——
    一次分析里同一处事故连推两条是最伤可信度的表现。
    """

    outcomes: list[AlertOutcome] = field(default_factory=list)
    _considered: set = field(default_factory=set, repr=False)

    def considered(self, event_id) -> bool:
        return event_id in self._considered

    def add(self, outcome: AlertOutcome) -> AlertOutcome:
        if outcome.event_id is not None:
            self._considered.add(outcome.event_id)
        self.outcomes.append(outcome)
        return outcome

    def counts(self) -> dict:
        sent = sum(1 for o in self.outcomes if o.status == "sent")
        failed = sum(1 for o in self.outcomes if o.status == "failed")
        skipped = sum(1 for o in self.outcomes if o.status == "skipped")
        reasons: dict[str, int] = {}
        for o in self.outcomes:
            if o.status == "skipped":
                reasons[o.reason] = reasons.get(o.reason, 0) + 1
        return {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "images_sent": sum(1 for o in self.outcomes if o.image_sent),
            "skip_reasons": reasons,
        }

    def summary_line(self) -> str:
        c = self.counts()
        parts = [f"已推送 {c['sent']} 条"]
        if c["images_sent"]:
            parts[0] += f"（含图 {c['images_sent']}）"
        if c["failed"]:
            parts.append(f"失败 {c['failed']}")
        if c["skipped"]:
            detail = "、".join(f"{k} {v}" for k, v in c["skip_reasons"].items())
            parts.append(f"跳过 {c['skipped']}（{detail}）")
        return " ｜ ".join(parts)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def push_events(
    events: list[dict],
    config: AlertConfig,
    ledger: AlertLedger | None = None,
    *,
    transport=None,
    image_root: Path | str | None = None,
    video_name: str = "",
    generated_at: str | None = None,
    at: str | None = None,
) -> AlertLedger:
    """按事件级等级推送告警。返回台账（含每一起的结论）。

    ⚠ **未配置通道时直接返回，一次网络请求都不发**（有测试守着）。
    同时它也不是静默的：会往台账里记一条「未配置」。
    """
    ledger = ledger if ledger is not None else AlertLedger()
    stamp = at or _now()

    if not config.enabled:
        # 只记一条。这条是"解释为什么群里没消息"，不是每个事件各记一条。
        if not any(o.kind == "config" for o in ledger.outcomes):
            ledger.add(
                AlertOutcome(
                    kind="config",
                    status="skipped",
                    reason=f"未配置{config.label}通道",
                    at=stamp,
                )
            )
        return ledger

    threshold = SEVERITY_ORDER[config.min_severity]

    # 先推最严重的：万一撞上每次上限，被挡掉的是优先级最低的那些。
    # 注意排序键查的是 SEVERITY_ORDER，不是中文字符串本身。
    pending = [e for e in events if not ledger.considered(e.get("event_id"))]
    pending.sort(
        key=lambda e: (
            -SEVERITY_ORDER.get(e.get("event_severity"), -1),
            e.get("start_sec") or 0,
        )
    )

    for event in pending:
        event_id = event.get("event_id")
        severity = event.get("event_severity")

        if severity not in SEVERITY_ORDER:
            # 「未判定」不是「不严重」。不知道有多严重就不该惊动处置人员。
            ledger.add(
                AlertOutcome(
                    kind="event",
                    status="skipped",
                    reason="等级未判定",
                    event_id=event_id,
                    severity=severity,
                    at=stamp,
                )
            )
            continue

        if SEVERITY_ORDER[severity] < threshold:
            ledger.add(
                AlertOutcome(
                    kind="event",
                    status="skipped",
                    reason="等级低于阈值",
                    event_id=event_id,
                    severity=severity,
                    at=stamp,
                )
            )
            continue

        if ledger.counts()["sent"] >= config.max_per_run:
            ledger.add(
                AlertOutcome(
                    kind="event",
                    status="skipped",
                    reason="已达本次上限",
                    event_id=event_id,
                    severity=severity,
                    at=stamp,
                )
            )
            continue

        card = render_event_card(
            event, video_name=video_name, generated_at=generated_at, style=config.card_style
        )
        path = roi_path(event, image_root) if config.wants_image else None
        text_ok, text_detail = send_card(
            config,
            card,
            subject=f"交通事故告警 · {severity}（事件 {event_id}）",
            transport=transport,
            # smtp 通道把现场图当附件，白送一个"带图"
            attachment=path if config.channel == CHANNEL_SMTP else None,
        )

        if not text_ok:
            ledger.add(
                AlertOutcome(
                    kind="event",
                    status="failed",
                    reason=text_detail,
                    event_id=event_id,
                    severity=severity,
                    at=stamp,
                )
            )
            continue

        image_sent: bool | None = None
        note = ""
        if config.channel == CHANNEL_SMTP:
            # 邮件的图在附件里，已经跟着正文一起发出去了（见上面的 attachment= 参数）
            image_sent = path is not None
            if config.with_image and path is None:
                note = "；附图缺失，仅发文字"
        elif config.with_image:
            if not config.image_supported:
                # 平台限制，不是失败。如实标出来，别让人以为"带了图"。
                image_sent = False
                note = f"；{config.label}通道不支持附图"
            elif path is None:
                image_sent = False
                note = "；附图缺失，仅发文字"
            else:
                image_sent, image_detail = send_image(config, path, transport)
                if not image_sent:
                    note = f"；附图失败（{image_detail}）"

        ledger.add(
            AlertOutcome(
                kind="event",
                status="sent",
                reason="ok" + note,
                event_id=event_id,
                severity=severity,
                text_sent=True,
                image_sent=image_sent,
                at=stamp,
            )
        )

    return ledger


def send_test(config: AlertConfig, *, transport=None, at: str | None = None) -> AlertOutcome:
    """发一条通道自检消息。**演示前必跑**——不要等到现场才发现地址填错了。"""
    stamp = at or _now()
    if not config.enabled:
        return AlertOutcome(
            kind="config",
            status="skipped",
            reason=(
                f"未配置{config.label}通道"
                f"（请设 SKYEYES_ALERT_URL / SKYEYES_ALERT_SMTP_* 或写 alert.config.json）"
            ),
            at=stamp,
        )
    ok, detail = send_card(
        config,
        render_test_card(config, at=stamp),
        subject=f"{BRAND} · 通道自检",
        transport=transport,
    )
    return AlertOutcome(
        kind="test",
        status="sent" if ok else "failed",
        reason=detail,
        text_sent=ok,
        at=stamp,
    )


def channel_view(config: AlertConfig) -> dict:
    """配置的对外视图（已脱敏）。供 `/api/health` 与前端使用。

    ⚠ 这里**绝不能**出现 `config.webhook` 或 `config.secret` 原文。
    """
    return {
        "channel": config.channel,
        "label": config.label,
        "configured": config.enabled,
        "target": config.masked,
        "min_severity": config.min_severity,
        "with_image": config.with_image,
        "image_supported": config.image_supported,
        "max_per_run": config.max_per_run,
        "source": config.source,
        "notes": list(config.notes),
    }


def report_summary(config: AlertConfig, ledger: AlertLedger, *, at: str | None = None) -> dict:
    """写进 `accident_report.json` 的 `alerts` 键。**只在真的开了告警时才调用**。"""
    return {
        "enabled": config.enabled,
        "channel": config.channel,
        "webhook": config.masked,
        "min_severity": config.min_severity,
        "with_image": config.wants_image,
        "max_per_run": config.max_per_run,
        "at": at or _now(),
        **ledger.counts(),
    }


def write_ledger(
    path: Path | str,
    config: AlertConfig,
    ledger: AlertLedger,
    *,
    at: str | None = None,
) -> Path:
    """把台账落盘。原子写（tmp + os.replace），与 job.json 同一套做法。"""
    path = Path(path)
    payload = {
        "version": 2,
        "at": at or _now(),
        "enabled": config.enabled,
        "channel": config.channel,
        "channel_label": config.label,
        "webhook": config.masked,
        "min_severity": config.min_severity,
        "with_image": config.wants_image,
        "max_per_run": config.max_per_run,
        "counts": ledger.counts(),
        "items": [o.to_dict() for o in ledger.outcomes],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # 台账写不成不该影响分析本身
        print(f"[警告] 写告警台账失败 {path}: {exc}")
    return path
