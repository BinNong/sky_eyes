> **本文件中的服务器地址、账号、路径均为占位符**（`SERVER_IP` / `USER` / `/remote/...` /
> `/path/to/...`），真实值已从版本库移除。请替换为你自己的环境；脚本可用对应的
> 环境变量覆盖，见各脚本头部。

ssh -p 3009 USER@SERVER_IP
# ⚠ 登录密码不写入版本库（本文件会进公开仓库）。密码请单独保管；
#   该账号在 sudo 组但无免密 sudo，需要提权的操作要人工执行。

多模态项目路径:/remote/Janus
注意，要在自己项目里创建python虚拟环境，该服务器有gpu算力

---

# 部署记录

## 服务器环境
- 系统 Ubuntu 24.04 / 驱动 575.57.08 (CUDA 12.9) / 单卡 NVIDIA RTX 4060 8GB
- 项目路径：`/remote/Janus`

## 虚拟环境
- 路径：`/remote/Janus/.venv`
- 由 conda `sparktts` 环境 virtualenv 而来（`--system-site-packages`），继承其 fastapi 0.115 / uvicorn 0.34 / transformers 4.46.2 / python-multipart 等
- **必须覆盖 torch**：sparktts 自带的 `torch 2.14.0+cu130` 在 575 驱动上 `cuda.is_available()=False`（cu130 需驱动 580+），
  已改装 `torch 2.6.0+cu124` 并将 `huggingface_hub` 固定为 `0.31.2`、`numpy` 固定为 `1.26.4`
- 额外安装：`timm`、`attrdict3`（提供 `attrdict` 模块，兼容 py3.12）

## 模型权重
- `models/Janus-1.3B`（`deepseek-ai/Janus-1.3B`，约 3.9GB，11 个文件）
- 因 `huggingface.co` 不可达，经 `HF_ENDPOINT=https://hf-mirror.com` 下载；
  新版 huggingface_hub 的 xet 后端在镜像站会报 401，下载时需 `HF_HUB_DISABLE_XET=1`

## 服务
- 代码：`fastapi_app.py`（原文件已备份为 `fastapi_app.py.orig`）
  - 模型路径改为本地目录，可用环境变量 `JANUS_MODEL_PATH` 覆盖
  - 新增 `GET /health` 健康检查接口
- 监听：`127.0.0.1:8000`（2026-09-29 起只绑回环。本服务无认证，绑 `0.0.0.0`
  会让同网段任何机器都能调用并占用 GPU；临时对外用 `JANUS_HOST=0.0.0.0 ./run_server.sh`）
- 管理脚本：`./start.sh` 后台启动 / `./stop.sh` 停止 / `./run_server.sh` 前台运行
- 日志：`logs/server.log`，PID：`logs/server.pid`

## 网络可达性（2026-09-23 修正，此前这里是错的）

**这条网络没有端口白名单。能不能从本机直连，只取决于服务 bind 在哪个地址。**

早先这里写的是"仅放通 3009 端口"，那是**错的**。决定性实验（在服务器上起一个
临时服务，再分别绑两个地址）：

```bash
ssh -p 3009 ... 'setsid nohup timeout 40 python3 -m http.server 45678 --bind 0.0.0.0 &'
nc -z SERVER_IP 45678          # → 通，而且 curl 拿到 HTTP 200

ssh -p 3009 ... 'setsid nohup timeout 25 python3 -m http.server 45679 --bind 127.0.0.1 &'
nc -z SERVER_IP 45679          # → 不通
```

结论：**绑 `0.0.0.0` 就可达，绑 `127.0.0.1` 就不可达**，网络层不做端口过滤。

> 当时为什么会扫出"只有 3009 通"？因为服务器上绑 `0.0.0.0` 的只有 sshd:3009、
> Janus:8000 和两个**别人的**服务（9200 Cpolar / 11434 Ollama），
> 而 8787 是**我们自己绑在了 `127.0.0.1`**——拿自己的配置去"验证"了网络的限制。
> **扫端口只能证明"有服务在听且可达"，无法区分"没服务"和"被拦"。**

**所以 Janus 本来就能直连**（实测往返 1.3s 完成一次图像理解）：

```bash
curl http://SERVER_IP:8000/health
# → {"status":"ok","model_path":"...Janus-1.3B","cuda":true,"device":"NVIDIA GeForce RTX 4060"}

python pipeline.py --api-base http://SERVER_IP:8000     # 不必建隧道
```

`./scripts/janus_tunnel.sh` 仍然可用（隧道当然也通），但不再是唯一路径。

## 验证结果
- `/health` 返回 `cuda=true`、`device=NVIDIA GeForce RTX 4060`，显存占用约 4.3G / 8G
- 单张事故帧推理耗时约 1.5 秒，返回中文事故描述与严重等级
- 批量模式（目录 + JSON 输出）已验证
- 端到端已跑通：`pipeline.py` 检测 296 张事故帧 → 聚类 9 个事件 → 18 帧送模型 → 生成报告

## 推理参数经验（重要）
- **`temperature` 必须用 0（贪心解码）**。实测 `temperature=0.1` 时 Janus 会陷入重复退化：
  同一段描述带不同等级反复出现（如「事故严重等级：低 / 中 / 高 / 高…」），
  809 字符里同一段话重复 4 次；`temperature=0` 时回答干净、长度合理。
  `multimodal_understanding.py` 与 `pipeline.py` 的默认值已改为 0。
- 让模型「按固定格式一行给等级」的强指令**会失效**：模型会把选项原样复读
  （输出「事故严重等级：低 或 事故严重等级：中 或 事故严重等级：高」并循环）。
  正确做法是主问题照常问，没判出等级时**再单独追问一次**「只回一个字」——
  实测 100% 得到单字答案。该逻辑已内置在 `pipeline.py`。
- Janus 只认「低/中/高」，但常写成「中度/重度」，抽取时需把 `重度 → 高` 归一化。

## ⚠️ 共享服务器会抢显存
这台机器是多人共用的。实测运行中曾出现服务端 500：
`torch.OutOfMemoryError: CUDA out of memory ... Process 3297938 has 3.31 GiB memory in use`，
即**别人的训练任务临时抢走了 3.3GB**，导致 Janus 无内存可用（它自己占 4.2GB）。
- 跑批量前先 `nvidia-smi` 看剩余显存，不足 5GB 就先等等
- `pipeline.py` 已内置失败重试（`--retries`，默认 3 次），这类瞬时故障重试即可通过
- `run_server.sh` 里已设 `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128` 减少碎片

---

# 启停操作

## 方式一：一键脚本（推荐）

```bash
cd /path/to/sky_eyes

# 启动：开远程服务 + 建隧道 + 自动等待就绪 + 本机验证
./scripts/janus_up.sh

# 关闭：关隧道 + 停远程服务 + 确认 GPU 释放
./scripts/janus_down.sh
```

若未配置 SSH 密钥，需带上密码环境变量：

```bash
JANUS_SSH_PASS='<密码>' ./scripts/janus_up.sh
JANUS_SSH_PASS='<密码>' ./scripts/janus_down.sh
```

> 隧道是在后台（nohup）运行的，无法交互式输入密码，
> 所以**强烈建议配置一次 SSH 密钥**，之后所有脚本都能免密运行：
> ```bash
> ssh-copy-id -p 3009 USER@SERVER_IP
> ```

## 方式二：手动两步

```bash
# 1) 服务器端启动（首次加载模型约 30~60 秒）
ssh -p 3009 USER@SERVER_IP
cd /remote/Janus && ./start.sh

# 2) 本地开隧道（另开一个终端，保持不关）
cd /path/to/sky_eyes && ./scripts/janus_tunnel.sh

# 3) 验证
curl --noproxy '*' http://127.0.0.1:8000/health
```

## 注意事项
- **服务只监听 `127.0.0.1:8000`，必须走隧道**：先 `./scripts/janus_tunnel.sh`，
  再用 `http://127.0.0.1:8000`（2026-09-29 起不再绑 `0.0.0.0`，本机无法直连
  `http://SERVER_IP:8000`）
- 这是**共享服务器**，启动前先看一眼 GPU 是否被占用：
  `nvidia-smi`（RTX 4060 共 8GB，Janus 需约 4.4GB）
- 用完请执行 `./scripts/janus_down.sh` 释放资源
- 脚本可用环境变量覆盖默认值：`JANUS_SSH_HOST` / `JANUS_SSH_PORT` / `JANUS_SSH_USER` /  `JANUS_REMOTE_DIR` / `LOCAL_PORT` / `REMOTE_PORT`

---

# 第二个服务：证据检索的编码服务（:8001）

「用文字在录像里找帧」需要一个图文编码模型（Chinese-CLIP ViT-B/16）。
它和 Janus 是**两个独立服务**，互不影响，可以各自启停：

| | Janus（多模态理解） | 检索编码服务 |
| --- | --- | --- |
| 端口 | 8000 | **8001** |
| 目录 | `/remote/Janus` | `/remote/sky_eyes_retrieval` |
| 进程管理 | `./start.sh` / `./stop.sh` | `./start.sh` / `./stop.sh` |
| 本地隧道 | `scripts/janus_tunnel.sh` | **`scripts/search_tunnel.sh`** |
| 显存 | 约 4.4 GB | 约 1.4 GB |

两者**同时跑得下**（4060 共 8 GB，实测合计约 5.8 GB）。

## 部署（一条命令，幂等）

```bash
./scripts/search_deploy.sh
```

它做的事：推服务代码 → 校验模型权重哈希（不符就从 modelscope 重下）→
装 `cn_clip` 到独立目录 → 重启服务 → 健康检查。

## ⚠ 三件必须记住的事

1. **不要把依赖装进 Janus 的 venv。** 编码服务**复用**它的 torch/torchvision/fastapi，
   但 `cn_clip` 是用 `pip install --target vendor/` 装到自己的目录、靠 `PYTHONPATH` 引入的。
   Janus 是演示主线，不该被一个旁路功能影响。
2. **模型权重在远端直接下载，不从本地推。** 本地→远端的 SSH 隧道要跟 Janus 抢带宽，
   而远端到 modelscope 是直连（实测 ~7.6 MB/s）。
   代价是多一步校验：`search_deploy.sh` 里的 `EXPECT_WEIGHTS` 是本地那份的 sha256，
   **哈希对上才说明远端跑的与本地做基准的是同一份权重**（`/health` 里的
   `weights_sha256` 就是它，换权重忘了同步时会立刻现形）。
3. **不用 modelscope 的 pipeline 加载模型。** 它的 `preprocessors.multi_modal` 在模块级
   `import decord` 并 `from .ofa import *`（后者又 import librosa）——
   为一条永远走不到的语音/视频路径拖一堆包。改用 **`cn_clip`**（Chinese-CLIP 官方实现），
   推理路径只需要 torch + torchvision，而且它的 `create_model` 恰好能直接吃下这份权重
   （自动剥 `module.` 前缀、过滤 `bert.pooler`，实测 353/353 张量全命中）。

## 性能实测（用来判断瓶颈在哪）

| 送出的内容 | 每帧耗时 |
| --- | --- |
| 224px 纯色（传输可忽略） | **17 ms** ← 纯 GPU 算力，比本地 CPU（890 ms）快 **52×** |
| 320px 缩略图（建索引实际用的） | 76 ms |
| 1280px 原图 | 278 ms |

**瓶颈是隧道带宽（实测约 0.5 MB/s），不是 GPU。** 所以送缩略图而不是原帧：
模型反正要压到 224，送 320 宽既省带宽又不影响结果。查询时只送一句文字，
走完整往返（隧道 + GPU + 排序）约 **55~86 ms**。

## 状态自检

```bash
curl --noproxy '*' http://127.0.0.1:8001/health   # 经隧道
./scripts/remote_search/start.sh                  # 远端侧，带就绪等待
```

---

# 整站部署：整个项目跑在服务器上（2026-09-23）

前面两节讲的是"把两个模型服务放到远端、本地仍是最完整的应用"。
这一节是另一种形态：**整个项目（前端 + 后端 + 两条模型链）都跑在服务器上**，
本地只要一条隧道就能用浏览器打开。

```
本地浏览器
   │  http://127.0.0.1:8888        ← 唯一入口
   ▼
[SSH 隧道 :3009]
   ▼
服务器 127.0.0.1:8787  ──  后端(server/app.py) + 前端静态(web/dist)  同源
   ├── 127.0.0.1:8000  Janus 多模态（pipeline 的"理解"阶段）
   ├── 127.0.0.1:8001  检索编码服务（"文字找帧"）
   └── 子进程 pipeline.py（YOLO 检测 / 聚类 / 多模态 / 报告 / 导出）
```

项目路径：`/remote/sky_eyes`

## 访问方式：SSH 隧道（2026-09-29 起改为只绑回环）

后端只绑 `127.0.0.1:8787`，**在服务器本机之外无法直连**。访问步骤：

```bash
# 在本机（不是服务器）执行
cd /path/to/sky_eyes
./scripts/remote_access.sh --daemon     # 建隧道：本机 8888 → 服务器 8787
open http://127.0.0.1:8888              # 然后浏览器打开这个
./scripts/remote_access.sh --stop       # 用完停掉
```

隧道只需一条：前端静态与 `/api` 同源，都由服务器上的 `:8787` 提供；
Janus(`:8000`) 与检索编码(`:8001`) 是后端在服务器内部**通过环回自己调的**，
本地不需要可达。

### 为什么改回环

这三个服务**都没有认证**。2026-09-29 查后端日志，发现服务绑 `0.0.0.0` 期间
被 **10 个不同来源**访问过（`grep -oE '^INFO: +[0-9.]+' logs/backend.log | sort | uniq -c`）：

| 来源 | 次数 | 说明 |
| --- | --- | --- |
| `127.0.0.1` | 456 | 服务器本机 |
| `LAN_IP_A` / `LAN_IP_B` / `LAN_IP_C` / `LAN_IP_D` | 246/215/79/40 | 内网其它网段 |
| `LAN_IP_E` / `LAN_IP_F` / `LAN_IP_G` | 236/46/11 | 与本机 SSH 登录源同段 |
| `CLIENT_PUBLIC_IP` | 59 | 本机公网出口（本身就是 NAT 出去访问的） |
| `UNKNOWN_PUBLIC_IP` | 29 | **非本人的公网 IP** |

也就是说，任何能路由到的机器都能触发 GPU 任务、读产物、删产物。所以收敛成
只绑回环。

### 临时对同网段开放（仅在需要演示时）

```bash
SKYEYES_HOST=0.0.0.0 ./deploy/skyeyes.sh restart     # 后端开全网卡
# Janus 同理：JANUS_HOST=0.0.0.0 ~/Janus/run_server.sh
```

⚠ 开放期间**没有任何认证**，谁都能用。演示完请改回默认。

## 三个进程与端口

| 服务 | 端口 | 由谁管 |
| --- | --- | --- |
| 后端 + 前端静态 | **8787**（唯一入口） | `deploy/skyeyes.sh` |
| Janus 多模态 | 8000 | `Janus/start.sh`（由 skyeyes.sh 调用） |
| 检索编码 | 8001 | `sky_eyes_retrieval/start.sh`（同上） |

前端与 `/api` **同源**：后端在 `SKYEYES_STATIC_DIR` 指向 `web/dist` 时顺带托管前端，
所以只需要**一条隧道**、不需要 vite 代理、也不存在跨域。
（`server/app.py` 里那段是**可选**的，不设那个环境变量时行为与从前完全一致，
本地 `npm run dev` 的开发流程不受影响。）

## 依赖从哪来（这里有个刻意的取舍）

| 需要 | 来源 | 理由 |
| --- | --- | --- |
| torch / torchvision / numpy / pillow / requests | **复用 `Janus/.venv`** | 远端已有一份 `torch 2.6.0+cu124`，另装要重下约 2.5GB |
| ultralytics / opencv | `pip install --target` 到项目 `vendor/` | `--target` **不写任何 site-packages**，Janus 环境零改动 |
| 后端（fastapi/uvicorn/httpx） | 项目自己的 `server/.venv` | 与本地一致，且不装 torch |
| 前端构建链 | 项目自己的 `.node/`（Node 20） | 系统 node 是 18，不满足要求 |

运行时是 `PYTHONPATH=<项目>/vendor <Janus venv>/bin/python`。
后端用 `subprocess.Popen` 拉 pipeline 且**没传 `env=`**，所以启动后端时设一次
`PYTHONPATH` 就会被子进程继承，不必改任何调度代码。

> **代价（明写出来）**：pipeline 因此依赖 Janus 的 venv 继续存在。
> 那份 venv 被重建或删掉时这里会失效——重跑 `deploy/setup_env.sh` 即可，但 torch 得跟着它。

版本策略：**影响检测结果的**（`ultralytics==8.3.76` / `opencv-python-headless==4.8.0.74`）
按本地钉死，让服务器算出的框与本地生成的演示数据可比；纯胶水依赖只给包名不钉版本。

## 常用命令

```bash
# 服务器上
cd /remote/sky_eyes
./deploy/setup_env.sh            # 建/校验 pipeline 环境（--check 只自检）
./deploy/setup_env.sh --check    #   自检会真的跑一次整帧检测 + 探测编码能力
./deploy/skyeyes.sh venv         # 建 server/.venv
./deploy/skyeyes.sh frontend     # 构建 web/dist（会按需下载 Node 20）
./deploy/skyeyes.sh start        # Janus → 检索编码 → 后端
./deploy/skyeyes.sh status       # 逐项探活 + GPU 余量
./deploy/skyeyes.sh stop
./deploy/skyeyes.sh logs backend

# 本机（另开一个终端）
./scripts/remote_access.sh --daemon     # 然后浏览器开 http://127.0.0.1:8888
./scripts/remote_access.sh --stop
```

## 部署过程中踩到并修掉的坑

这几条都是"会静默出错、不报错"的类型，值得留档：

1. **`python3 -m venv` 在 Ubuntu 上直接失败**——系统 python 没装 `python3-venv`（缺 `ensurepip`），
   报 "You may need to use sudo with that command"。`deploy/skyeyes.sh venv` 现在会自动
   挑一个能建 venv 的解释器（conda base 的 python 可以）。

2. **Node 版本不够会导致前端构建出"残废"依赖**。系统 node 18，而 Tailwind v4 的
   `@tailwindcss/oxide` 声明 `node: ">= 20"`；npm 9 只警告 `EBADENGINE` 然后装出
   没有原生绑定的半成品，报错是 `Cannot find native binding`。
   现在会把 Node 20 下到项目 `.node/`（不需要 sudo），并用 `npm ci`。

3. **跨平台的 `package-lock.json` 需要 npm ≥ 10**。用 macOS 上生成的 lockfile 配 npm 9，
   linux 的 optionalDependencies（oxide 原生包）会被漏掉；npm 10 能正确处理。
   所以不必为服务器单独维护一份 lockfile。

4. **pip 装的 opencv wheel 不含 H.264 编码器**，`cv2.VideoWriter(..., "avc1")` 打不开，
   报 `Could not find encoder for codec_id=27`。原实现会退回 `mp4v` 并打一行警告——
   那等于把浏览器播不了的文件当成交付物。现在 `detector.py` 会在写完 mp4v 之后
   **用 ffmpeg 转成 H.264**（`-pix_fmt yuv420p -movflags +faststart`），
   并把实际编码方式记进 `accident_frames.json` 的 `video_encode_mode`。
   服务器上实测 `codec_name=h264`。

5. **SPA 兜底路由会吃掉 API**。把 `/{full_path:path}` 注册在 API 路由之前，
   所有接口都会返回 index.html；而 `/api/typo` 这种**路径写错**的情况也会拿到
   200 + HTML，前端拿去 `JSON.parse` 报一个语法错误，完全指不到真正原因。
   现在兜底里显式排除 `api` 命名空间（返回 404 JSON），并且注册顺序有测试守着。

6. **`nohup ... &` 在 SSH 会话结束时会被带走**。`deploy/skyeyes.sh` 里用 `setsid` 起进程，
   并且 pid 由**被启动的进程自己写**（`echo $$` 后再 `exec`）——`setsid` 可能 fork，
   用 shell 的 `$!` 会记到一个短命的中间进程。

## 验证结果（部署当次）

- **端到端真跑一次分析**：通过 `POST /api/jobs` 起任务，**47.5 秒完成**
  （本地 CPU 同样流程约 6.3 分钟），产物齐全，标注视频 `codec_name=h264`
- **结果与本地一致**：事件级等级分布 `低4 / 中4 / 高1`、9 起事件，
  与 `web/public/data/demo.json` **完全相同**——这是"服务器算得对不对"最直接的证据
- **静态演示数据未被污染**：跑完真实任务后 `demo.json` 的 sha256 与本地逐字节一致
- **测试全绿**：11 + 20 + 5 + 73 + 22 + 13 + 22 + 25 + 18 = **209 条**
- **浏览器实测**（经隧道）：首页/事件库/检索/系统页均正常，命中测试通过，
  3 档视口无横向溢出；检索页搜「蓝色的公交车」返回 4 张蓝巴士、点击后人跳转到 23.4s

## 显存预算（跑之前先看）

4060 共 8 GB。实测常驻占用：Janus 约 4.4 GB + 检索编码约 1.4 GB ≈ **5.8 GB**，
跑分析时 YOLO 再占约 0.6 GB。**分析任务串行**（后端用 409 拦住并发），
因为并发两次多模态必然 OOM。启动前先 `./deploy/skyeyes.sh status` 看余量。


