# sky_eyes · 交通事故检测与理解

用自训 YOLO 从监控视频里抓事故帧，再把关键帧送多模态模型（DeepSeek Janus）生成事故描述与严重等级，输出结构化报告。

> ### ⚠ 关于本文档中的占位符
>
> 本仓库已剔除全部真实基础设施信息（IP、账号、邮箱、绝对路径），文档与脚本中出现的
> 下列名字都是**占位符，请替换为你自己的值**：
>
> | 占位符 | 含义 |
> | --- | --- |
> | `SERVER_IP` | 你的远程服务器地址 |
> | `USER` | 服务器登录账号 |
> | `USER_EMAIL` | 你的邮箱 |
> | `/remote/...` | 服务器上的项目路径（如 `/remote/Janus`） |
> | `/path/to/...` | 本机的项目路径与 Python 解释器路径 |
>
> 脚本都支持用**环境变量覆盖**这些默认值（`JANUS_SSH_HOST` / `JANUS_SSH_USER` /
> `SKYEYES_REMOTE` / `SKYEYES_DIR` 等，见各脚本头部注释），所以通常不必改动文件本身。

## 流程

```
监控视频
  │
  ├─ detector.py        逐帧 YOLO 检测 ──► output/accident_frames/*.jpg  + accident_frames.json
  │                                        （帧号 / 时间戳 / 置信度 / bbox）
  ├─ group_events()     连号帧聚类     ──► accident_events.json
  │                                        （事故事件 + 每个事件挑代表帧）
  ├─ JanusClient        代表帧送多模态 ──► 事故描述 + 严重等级
  │
  └─ pipeline.py        汇总           ──► accident_report.json / accident_report.md
```

> 检测与理解是**可分离**的两段：检测跑一次要几分钟（本地 CPU），理解很快（服务器 GPU）。
> 所以检测结果会落盘，改聚类参数或想重新生成描述时用 `--stage understand` 复用即可。

## 模型权重（不入库）

本仓库**不含模型权重**。`models/trained_model.pt`（YOLO，19 MB）已在 `.gitignore`
中排除，clone 下来是空的。

它是检测阶段的必需品，`detector.py` 默认从这里加载：

```python
DEFAULT_WEIGHTS = ROOT / "models" / "trained_model.pt"
```

把权重放回 `models/trained_model.pt` 即可；也可以放在别处用参数指定：

```bash
$PY detector.py --video source/raw_videos/xxx.mp4 -w /path/to/trained_model.pt
```

**缺权重不会静默降级**——`detector.py` 会直接抛 `FileNotFoundError: YOLO 权重不存在`，
避免在"没有检测结果"的状态下一路跑完还输出一份空报告。

同理不入库的还有原始素材、前端演示数据与运行产物，见 `.gitignore` 的说明。
需要权重或训练代码请联系作者。

## 环境

本地用 conda 环境 `visual_search`（Python 3.11.9，依赖与 `requirements.txt` 一致，含 torch 2.0.1 / ultralytics）：

```bash
/path/to/visual_search/bin/python
```

**推理服务在远程服务器上，本机只是 HTTP 客户端**，所以本机不需要装 torch 之外的东西。
服务部署与启停见 `remote_server.md`。

### 两种跑法

| | 应用跑在哪 | 怎么用 | 适合 |
| --- | --- | --- | --- |
| **本地开发** | 本机（conda `visual_search` + `server/.venv` + `npm run dev`） | 前端 `:5180`，后端 `:8787` | 改代码 |
| **整站部署** | 服务器 `/remote/sky_eyes` | 本机执行 `./scripts/remote_access.sh --daemon`，再开 `http://127.0.0.1:8888` | 演示 / 给别人看 |

整站部署的目录布局、依赖来源、启停命令与踩到的坑，见 `remote_server.md` 的
「**整站部署**」一节。⚠ 那条网络**没有端口白名单**——能不能直连只取决于服务
绑在哪个地址（绑 `0.0.0.0` 就可达、绑 `127.0.0.1` 就不通，已实测）。三个服务
（Janus:8000 / 检索:8001 / 后端:8787）自 **2026-09-29 起都只绑 `127.0.0.1`**
（它们都没有认证，暴露给同网段等于把"触发 GPU 任务、删产物"的接口交出去），
所以**必须走 SSH 隧道**——隧道只需一条，前端与 `/api` 同源都在 8787。
确实要临时对同网段开放后端时：`SKYEYES_HOST=0.0.0.0 ./deploy/skyeyes.sh restart`。

证据检索另有一个**编码服务**（中文 CLIP），也跑在远端、复用 Janus 那个 venv 的 torch
（`cn_clip` 用 `pip --target` 装到独立目录，**不往 Janus 的环境里装东西**），
部署脚本是 `scripts/search_deploy.sh`。详见下面「证据检索」一节。

## 用法

### 0. 先启动多模态服务（必须）

```bash
cd /path/to/sky_eyes
./scripts/janus_up.sh        # 开远程服务 + 建 SSH 隧道 + 验证
```

服务器 8000 端口无法直连，客户端固定访问 `http://127.0.0.1:8000`（走隧道）。
用完 `./scripts/janus_down.sh` 释放资源。

### 1. 完整流程（检测 + 理解 + 报告）

```bash
PY=/path/to/visual_search/bin/python
$PY pipeline.py
```

### 2. 只跑检测（不占服务器资源）

```bash
$PY pipeline.py --stage detect
```

### 3. 复用已有检测结果，只重跑理解与报告

```bash
$PY pipeline.py --stage understand
```

### 4. 常用调参

```bash
$PY pipeline.py --conf 0.5 --gap 12 --frames-per-event 3   # 置信度阈值 / 聚类容差 / 每事件抽帧数
$PY pipeline.py --no-followup                              # 不做等级追问，省调用
$PY pipeline.py --no-video                                 # 不生成标注视频
```

检测端也可单独用：

```bash
$PY detector.py --help          # 视频 → 事故帧 ROI + 元数据 + 事件聚类
$PY multimodal_understanding.py output/accident_frames/accident_frame_0083_roi.jpg
```

### 5. 告警推送（默认关闭，支持四个通道）

把判定出的**高等级事件**推到处置方能看见的地方。**默认关闭，必须显式开启**——
它会往一个真实的工作群/邮箱发消息，不能有"跑了才发现推了"这种事。

**为什么有四个通道**：最初只支持企业微信群机器人。实测发现，多数企业把「群机器人/消息推送」
的**创建权限收在管理员手里**（管理后台 → 应用管理 → 消息推送 → "可创建消息推送的成员"），
**即使你是群主也建不出来**。这条路被组织策略堵死时，不该让整条告警链失效。

| `channel` | 形态 | 现场图 | 需要管理员审批 |
| --- | --- | --- | --- |
| `wecom` | 企业微信群机器人（现名「消息推送」） | ✅ 图文两条 | ❌ 常见 |
| `dingtalk` | 钉钉自定义机器人 | ❌ 无图片消息类型 | ✅ 不用，群成员即可 |
| `feishu` | 飞书自定义机器人 | ❌ 需先上传换 image_key | ✅ 不用，群成员即可 |
| `smtp` | 邮件 | ✅ 作为附件 | ✅ 不用，有邮箱即可 |

> 只有企业微信能"文字卡片 + 现场图"两条一起发。钉钉/飞书的自定义机器人**没有图片消息类型**，
> 这不是省略，是平台限制——程序会如实标注"该通道不支持附图"，而不是让你以为带了图。

**先在项目根建配置文件**（已被 `.gitignore` 忽略，模板见 `alert.config.example.json`）：

```bash
cp alert.config.example.json alert.config.json
# 填 channel 与 url：
#   钉钉：群设置 → 智能群助手 → 添加机器人 → 自定义机器人 → 复制 Webhook
#   飞书：群设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook
#   企微：内部群 → 群上方「⋯」→ 消息推送 → 自定义消息推送（⚠ 新版没有"新建"这个按钮）
```

也可以走环境变量（优先级更高）：

```bash
export SKYEYES_ALERT_CHANNEL=dingtalk
export SKYEYES_ALERT_URL='https://oapi.dingtalk.com/robot/send?access_token=xxx'
export SKYEYES_ALERT_SECRET='SECxxxx'    # 安全设置选了「加签」时才有
```

**演示前先自检通道**，别等现场才发现地址填错：

```bash
$PY pipeline.py --alert-test          # 发一条测试消息就退出，不做任何检测
```

自检通过后再跑带推送的完整流程：

```bash
$PY pipeline.py --alert                                # 达标事件在判定出来时立即推送
$PY pipeline.py --alert --alert-channel feishu          # 换通道（通道名不是密钥，可以走命令行）
$PY pipeline.py --alert --alert-min-severity 中         # 放宽到「中」（默认只推「高」）
$PY pipeline.py --alert --no-alert-image                # 不带现场图，只发文字卡片
$PY pipeline.py --alert --alert-max 3                   # 本次最多推 3 条
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `SKYEYES_ALERT_CHANNEL` | `wecom` | `wecom` / `dingtalk` / `feishu` / `smtp` |
| `SKYEYES_ALERT_URL` | 无 | 目标地址。**没配就是关闭状态，一次请求都不发** |
| `SKYEYES_ALERT_SECRET` | 无 | 加签密钥（钉钉/飞书选了「加签」时） |
| `SKYEYES_ALERT_MIN_SEVERITY` | `高` | 推送的等级下限 |
| `SKYEYES_ALERT_IMAGE` | `1` | 是否附带代表帧 ROI 图（通道支持时） |
| `SKYEYES_ALERT_MAX` | `5` | 一次分析最多推几条 |
| `SKYEYES_ALERT_PROXY` | `1` | 是否跟随 `HTTPS_PROXY`；本机自测可设 `0` |
| `SKYEYES_ALERT_CONFIG` | `alert.config.json` | 配置文件路径 |
| `SKYEYES_WECOM_WEBHOOK` | — | **旧名，仍兼容**（最初只有企微通道时用的） |

`smtp` 通道另有 `SKYEYES_ALERT_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_TO` / `_STARTTLS`。
⚠ `_PASSWORD` 要填**授权码**，不是邮箱登录密码——QQ/163 都要在邮箱设置里单独开启并生成。

几条**写死在代码里**的行为（都由 `tests/test_alerting.py` 守着，73 条）：

- **只推达标等级，且「未判定」永远不推**。不知道有多严重 ≠ 不严重。
  门控查显式映射表，不拿中文等级比大小——码点序是 `中` < `低` < `高`，字符串比较是反的。
- **HTTP 200 不算发送成功**。三家平台出错时都返回 200，结论在 body 里
  （企微/钉钉看 `errcode`，飞书看 `code`）。只看状态码会把"地址填错了"记成"已推送"。
  **响应里没有该字段时同样判失败**——宁可误报失败，不可误报成功。
- **推送失败不会中断分析**。推送是旁路：网络不通、图读不到，都不该让跑了 6 分钟的检测白跑。
  失败条数与原因会如实写进 `alerts.json`。
- **凭证不落进任何日志或产物**。各平台藏 key 的位置不一样，这是最容易漏的一处：
  企微 `?key=`、钉钉 `?access_token=`、**飞书在 URL 路径里** `.../hook/xxxxxxxx`。
  加签密钥同理。也**刻意不做成命令行参数**——`ps aux` 看得见，
  而且后端会把完整命令行写进落盘的 `job.log`。
- **加签每次现算**：签名里带时间戳，拿第一次的结果去重试必被判"签名过期"。
- 一次分析里同一个事件只推一次；撞上限时优先推最严重的。

### 6. 证据检索：用一句话在录像里找帧

输入「一辆黄色的小汽车」，返回这段录像里最像的若干帧（帧号 / 时间点 / 缩略图），
点一下就把播放器跳到那一刻。用于查资料、找证据。

**模型不在本地跑。** 编码服务部署在有 GPU 的机器上（远端 RTX 4060），
本地经 SSH 隧道访问；索引与排序留在本地。理由：

| | 本地 CPU | 远端 GPU |
| --- | --- | --- |
| 编码一帧 | **890 ms** | **17 ms**（纯算力；实测含传输约 76 ms） |

38 秒的演示视频在本地 CPU 上要 14 分钟，在远端是十几秒。而且本地后端
`server/.venv` 刻意不装 torch，不该为一个旁路功能破这条规矩。

```bash
# ① 部署并启动远端编码服务（首次会自动拉模型并校验哈希）
./scripts/search_deploy.sh

# ② 建本地隧道：127.0.0.1:8001 → 远端 :8001
./scripts/search_tunnel.sh --daemon

# ③ 体检（不建索引）
$PY scripts/build_search_index.py --check

# ④ 建索引（192 个候选点约 8 秒，产物 2.9 MB）
$PY scripts/build_search_index.py                 # 默认 stride=5，即每 0.2 秒一个候选点
$PY scripts/build_search_index.py --stride 1      # 逐帧，索引更大
```

产物落在 `web/public/data/search/`（前端静态目录，缩略图由 vite 直接发），
界面在 **`/evidence`**。

几条写死的设计（由 `tests/test_video_search.py` 的 22 条测试守着）：

- **分数只用于排序，没有"相似度 87%"这回事。** 实测所有帧的余弦都挤在
  0.34~0.41 的窄带里，同一查询前四名常常只差 0.002。窄不代表没用——对着帧
  逐一核对过，排序是准的——但**绝对分数不可解释**：0.35 既可能是最好的结果，
  也可能是最差的。所以界面上只给名次与相对差距，并把这句说明原样写给使用者。
- **「编码服务不可用」和「没搜到」必须分开。** 两者在界面上都表现为"没有结果"，
  但一个要去查隧道与远端服务，一个要去改查询词。后端因此对前者返回 **503**，
  前端据此显示不同的提示——不能压回同一句"无结果"。
- **预置查询随索引离线保存。** 建索引时把几条示例查询的向量一起算好存进
  `chips.json`，所以**隧道断了那几条仍能搜**（现场兜底）。自由输入则如实报错。
- **索引与当前视频对不上要警告。** 索引记着自己的 fps / 时长 / 帧数，
  与页面上加载的录像对不上时给出提示——否则点击结果会跳到错误的时间点，
  而使用者只会觉得"这个检索不准"。
- **索引写盘后回读校验。** 向量是裸 float32 二进制，写错不会有任何提示；
  `build_index` 落盘后会自己读回来验一遍（含"L2 范数必须 ≈ 1"这道闸，
  它同时能抓住字节序反转、dtype 读错、行列错位、写盘截断四种静默损坏）。

服务端另有 `/api/search/status`（索引 + 编码服务自检）与 `POST /api/search`；
系统状态页会显示编码服务是否可达、GPU 显存与索引规模。

## 产物

| 文件 | 内容 |
| --- | --- |
| `output/accident_frames/` | 事故帧 ROI 裁剪图（命名 `accident_frame_XXXX_roi.jpg`，XXXX 为帧号） |
| `output/accident_frames.json` | 原始检测元数据：帧号、时间戳、置信度、bbox |
| `output/accident_events.json` | 去重聚类后的事故事件与代表帧 |
| `output/accident_report.json` | 完整报告（含每帧描述原文、等级、质量标记） |
| `output/accident_report.md` | 可读报告：汇总表 + 事件详情 |
| `output/detections.mp4` | 画了检测框的标注视频 |
| `web/public/data/search/` | 证据检索索引：`index.json`（元数据）+ `vectors.f32`（裸 float32 向量）+ `thumbs/`（缩略图）+ `chips.json`（预置查询向量） |
| `output/alerts.json` | 企微告警台账（**只有加了 `--alert` 才生成**）：每条推送的成败、跳过原因、打码后的 webhook |

## 关于帧去重

原始检测每帧只要命中 `accident` 就存一张图，实测 959 帧的视频存了 **296 张**，
其中大量是连号重复画面（例如帧 709–783 连续 75 帧）。

`group_events()` 把帧号间隔 ≤ `--gap`（默认 10）的帧聚成**一次事故事件**，
每个事件按置信度从高到低挑 `--frames-per-event` 张代表帧送模型。
默认参数下 296 张 → **9 个事件 → 18 帧**，多模态调用量降到 1/16。
想保留更多细节就调大 `--frames-per-event`。

## 关于严重等级

- 模型只认「低/中/高」，但常写成「中度/重度」，抽取时会归一化（重度 → 高）
- 模型偶尔在同一段话里给出不同等级，此时取**最后一次**判定，并在报告里标注原文出现过的等级
- 主问题没判出等级时（模型爱说「无法判断」），会自动**追问一次**「只回一个字」，报告里标记来源
- `temperature` 必须为 0：实测 0.1 时模型会陷入重复输出的退化状态

---

## 远期规划（尚未实现）

本项目提出了一个交通事故监测预警评估系统，目标是提供全面的紧急响应和损害评估方案：

* 结合视觉-语言模型（多模态）与目标检测 YOLO 技术，实时检测事故并评估严重程度 👀 ✅ 已实现
* 一旦检测到紧急情况，通过卫星定位识别最近的医院，并向紧急服务发送警报 🚑
  —— 「向处置方发送警报」这一半**已实现**：达标事件会实时推到企业微信群
  （见上方「用法 5. 企业微信告警推送」，含现场图与处置建议）。
  未做的部分：卫星定位找最近医院、对接 120 调度系统。
* 估算损害成本，并将用户与维修服务提供商联系起来 💰
* 通过结合实时检测、严重评估和端到端服务便利，提高紧急响应效率 🔄
