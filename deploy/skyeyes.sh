#!/usr/bin/env bash
# Sky Eyes 服务器端一键启停。
#
#   ./deploy/skyeyes.sh start              启动全部（Janus → 检索编码 → 后端）
#   ./deploy/skyeyes.sh stop               停止全部
#   ./deploy/skyeyes.sh restart
#   ./deploy/skyeyes.sh status             逐项探活（含 GPU 余量）
#   ./deploy/skyeyes.sh logs [名字]        跟日志（backend / janus / search）
#   ./deploy/skyeyes.sh env                建 pipeline 环境（vendor，跑 setup_env.sh）
#   ./deploy/skyeyes.sh venv               建后端 venv（server/.venv）
#   ./deploy/skyeyes.sh frontend           构建前端（web/dist）
#
# 三个进程全部只绑 127.0.0.1（2026-09-29 起）：
#   Janus 多模态服务    127.0.0.1:8000   pipeline 的"理解"阶段用它（见 Janus/run_server.sh）
#   检索编码服务        127.0.0.1:8001   "文字找帧"用它（代码在 sky_eyes_retrieval）
#   后端 + 前端静态     127.0.0.1:8787   **主入口**，前端与 /api 同源
#
# 为什么绑回环：这三个服务**都没有认证**，绑 0.0.0.0 等于把「能触发 GPU 任务、能删产物」
# 的接口交给整个网段。2026-09-29 查后端日志发现，服务被 **10 个不同来源**访问过，
# 其中包含非本人的公网 IP（`UNKNOWN_PUBLIC_IP`，29 次）——所以收敛成只绑回环。
#
# ⚠ 绑 0.0.0.0 还是 127.0.0.1，直接决定本机能不能直连 —— 这条网络上**没有端口白名单**。
#   2026-09-23 实测：在服务器上随便起一个 `0.0.0.0:45678`，本机立刻可达（HTTP 200）；
#   起在 `127.0.0.1:45679` 的则不通。也就是说 "扫得到/扫不到" 只反映 bind 地址，
#   不反映网络策略。曾据此误判成"只放通了 3009"，白绕了一条 SSH 隧道。
#
#   绑回环后**本机浏览器不能再直连** `http://<服务器IP>:8787`，必须走 SSH 隧道：
#       本机执行  ./scripts/remote_access.sh --daemon
#       然后打开  http://127.0.0.1:8888
#     （前端全部用相对路径 /api，走隧道无需改任何代码）
#   确实需要临时对同网段开放时：
#       SKYEYES_HOST=0.0.0.0 ./deploy/skyeyes.sh restart
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="$PROJ_DIR/logs"
PORT="${SKYEYES_PORT:-8787}"
# 后端监听地址。127.0.0.1 = 只经 SSH 隧道可达（默认，服务无认证，收敛暴露面）；
# 0.0.0.0 = 同网段可直连（仅临时演示时用，见文件头说明）。
BIND="${SKYEYES_HOST:-127.0.0.1}"

# 装了 torch/ultralytics 的那个解释器。默认复用 Janus 的 venv（见 setup_env.sh 的说明）
BASE_PY="${SKYEYES_BASE_PY:-/remote/Janus/.venv/bin/python}"
VENDOR="${SKYEYES_VENDOR:-$PROJ_DIR/vendor}"
BACKEND_VENV="${SKYEYES_BACKEND_VENV:-$PROJ_DIR/server/.venv}"
JANUS_DIR="${SKYEYES_JANUS_DIR:-/remote/Janus}"
SEARCH_DIR="${SKYEYES_SEARCH_DIR:-/remote/sky_eyes_retrieval}"
DIST="$PROJ_DIR/web/dist"

# 前端构建需要 Node >= 20（Tailwind v4 的 oxide 原生包声明了 `node: ">= 20"`）。
# 系统 node 太旧时，把 Node 下到项目自己的 .node/ 里——不需要 sudo，也不动系统环境。
NODE_DIR="${SKYEYES_NODE_DIR:-$PROJ_DIR/.node}"
NODE_VERSION="${SKYEYES_NODE_VERSION:-v20.19.5}"
NODE_MIN_MAJOR=20
NODE_BIN=""

mkdir -p "$LOGS"

c_ok()   { printf "  \033[32m%s\033[0m\n" "$*"; }
c_warn() { printf "  \033[33m%s\033[0m\n" "$*"; }
c_bad()  { printf "  \033[31m%s\033[0m\n" "$*"; }

port_listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

http_ok() {  # url
  curl -sS -m 5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null | grep -q '^200$'
}

pid_alive() {  # pidfile
  [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null
}

# 后台起一个进程。用 setsid 脱离当前会话——普通的 `nohup ... &` 在
# ssh 会话结束时会被连带收走（本项目踩过：进程没了但任务显示成功）。
#
# pid 由被启动的进程自己写（`echo $$` 后再 exec），**不用 shell 的 `$!`**：
# setsid 在调用者已是进程组组长时会 fork，那时 `$!` 拿到的是那个短命的
# setsid 进程，pid 文件当场就过期了。
spawn() {  # name  workdir  cmd...
  local name="$1" dir="$2"; shift 2
  local pidfile="$LOGS/$name.pid"
  (
    cd "$dir"
    setsid sh -c 'echo $$ >"$0"; exec "$@"' "$pidfile" env "$@" \
      >>"$LOGS/$name.log" 2>&1 </dev/null &
  )
}

wait_port() {  # port  seconds  label
  local p="$1" n="$2" label="$3" i
  for i in $(seq 1 "$n"); do
    port_listening "$p" && { c_ok "$label 已就绪（:${p}）"; return 0; }
    sleep 1
  done
  c_bad "$label 在 ${n}s 内没起来，看 $LOGS/ 下的日志"
  return 1
}

# ─────────────────────────── 启动 ───────────────────────────

start_janus() {
  if http_ok "http://127.0.0.1:8000/health"; then c_ok "Janus 已在运行"; return 0; fi
  if [ ! -x "$JANUS_DIR/start.sh" ]; then c_bad "找不到 $JANUS_DIR/start.sh"; return 1; fi
  echo "  · 启动 Janus ..."
  ( cd "$JANUS_DIR" && ./start.sh ) | sed 's/^/    /'
  wait_port 8000 120 janus
}

start_search() {
  if http_ok "http://127.0.0.1:8001/health"; then c_ok "检索编码服务已在运行"; return 0; fi
  if [ ! -x "$SEARCH_DIR/start.sh" ]; then c_bad "找不到 $SEARCH_DIR/start.sh"; return 1; fi
  echo "  · 启动检索编码服务 ..."
  ( cd "$SEARCH_DIR" && ./start.sh ) | sed 's/^/    /'
  wait_port 8001 180 search
}

start_backend() {
  if http_ok "http://127.0.0.1:$PORT/api/health"; then
    c_ok "后端已在运行"
    return 0
  fi
  if [ ! -x "$BACKEND_VENV/bin/uvicorn" ]; then
    c_bad "后端 venv 不存在，先跑：./deploy/skyeyes.sh venv"
    return 1
  fi
  if [ ! -x "$BASE_PY" ]; then
    c_bad "找不到推理解释器 ${BASE_PY}（可用 SKYEYES_BASE_PY 指定）"
    return 1
  fi
  if [ ! -d "$VENDOR" ]; then
    c_bad "vendor 不存在，先跑：./deploy/skyeyes.sh env"
    return 1
  fi
  if [ ! -f "$DIST/index.html" ]; then
    c_warn "web/dist 还没构建，后端只提供 API；跑 ./deploy/skyeyes.sh frontend 构建前端"
  fi

  echo "  · 启动后端 ${BIND}:${PORT} ..."
  if [ "$BIND" = "0.0.0.0" ]; then
    c_warn "监听 0.0.0.0 —— 同网段任何机器都能访问，而本服务没有认证"
    c_warn "  改回只绑回环： SKYEYES_HOST=127.0.0.1 ./deploy/skyeyes.sh restart"
  else
    c_ok "只绑回环 ${BIND}（外部需 SSH 隧道，见 scripts/remote_access.sh）"
  fi
  # PYTHONPATH 会被 pipeline 子进程继承（后端用 subprocess.Popen 且未传 env=），
  # 所以 vendor 那层在这里设一次就够，不必改调度代码。
  spawn backend "$PROJ_DIR/server" \
    "PYTHONPATH=$VENDOR" \
    "SKYEYES_PY=$BASE_PY" \
    "SKYEYES_STATIC_DIR=$DIST" \
    "JANUS_API_BASE=http://127.0.0.1:8000" \
    "SKYEYES_CLIP_URL=http://127.0.0.1:8001" \
    "YOLO_AUTOINSTALL=false" \
    "PYTHONUNBUFFERED=1" \
    "$BACKEND_VENV/bin/uvicorn" app:app --host "$BIND" --port "$PORT" --log-level info
  wait_port "$PORT" 60 backend
}

cmd_start() {
  echo "启动 Sky Eyes（目录 ${PROJ_DIR}）"
  start_janus
  start_search
  start_backend
  echo
  cmd_status
  echo
  if [ "$BIND" = "127.0.0.1" ]; then
    echo "访问方式：服务只绑回环，在**本机**跑 ./scripts/remote_access.sh --daemon"
    echo "          然后打开 http://127.0.0.1:8888"
  else
    srv_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "访问方式：http://${srv_ip:-<服务器IP>}:${PORT}（已对同网段开放，无认证请注意）"
  fi
}

# ─────────────────────────── 停止 ───────────────────────────

kill_port() {  # port  label
  local p="$1" label="$2" pids
  pids="$(ss -ltnp 2>/dev/null | grep ":$p " | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)"
  if [ -z "$pids" ]; then echo "    · $label :$p 本来就没在监听"; return 0; fi
  for pid in $pids; do
    # 只杀我们自己的进程：命令行里必须出现本项目相关路径
    if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -qE "sky_eyes|Janus"; then
      kill "$pid" 2>/dev/null && echo "    · 已停 $label pid=$pid"
    else
      echo "    · $label :$p 上的 pid=$pid 不属于本项目，跳过"
    fi
  done
}

cmd_stop() {
  echo "停止 Sky Eyes"
  if [ -f "$LOGS/backend.pid" ]; then
    pid="$(cat "$LOGS/backend.pid")"
    kill "$pid" 2>/dev/null && echo "    · 已停 backend pid=$pid" || true
    rm -f "$LOGS/backend.pid"
  fi
  kill_port "$PORT" backend
  if [ -x "$JANUS_DIR/stop.sh" ]; then ( cd "$JANUS_DIR" && ./stop.sh ) | sed 's/^/    · /'; fi
  if [ -x "$SEARCH_DIR/stop.sh" ]; then ( cd "$SEARCH_DIR" && ./stop.sh ) | sed 's/^/    · /'; fi
  sleep 2
  echo
  echo "停止后 GPU："
  nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/    /'
}

# ─────────────────────────── 状态 ───────────────────────────

cmd_status() {
  echo "状态"
  # 后端
  if http_ok "http://127.0.0.1:$PORT/api/health"; then
    c_ok "后端          http://127.0.0.1:$PORT  ✅"
    # 实际可达性由 bind 地址决定（这条网络没有端口白名单），所以把对外地址也报出来，
    # 免得又有人靠"扫端口"去猜。
    if [ "$BIND" != "127.0.0.1" ]; then
      srv_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
      if [ -n "$srv_ip" ]; then
        c_ok "  对外访问    http://${srv_ip}:${PORT}  （监听 ${BIND}）"
      fi
    else
      c_warn "  仅本机可达  （监听 127.0.0.1，外部需 SSH 隧道）"
    fi
  elif port_listening "$PORT"; then
    c_warn "后端          :$PORT 在监听但 /api/health 不通"
  else
    c_bad "后端          未启动"
  fi
  # 前端静态
  if [ -f "$DIST/index.html" ]; then
    c_ok "前端静态      web/dist ✅（由后端同端口托管）"
  else
    c_warn "前端静态      web/dist 缺失 → 只有 API，没有界面"
  fi
  # 检索编码
  if http_ok "http://127.0.0.1:8001/health"; then
    dev="$(curl -sS -m 5 http://127.0.0.1:8001/health 2>/dev/null | grep -o '"device":"[^"]*"' | cut -d'"' -f4)"
    c_ok "检索编码服务  http://127.0.0.1:8001  ✅ device=$dev"
  else
    c_bad "检索编码服务  未启动（「文字找帧」将不可用）"
  fi
  # Janus
  if http_ok "http://127.0.0.1:8000/health"; then
    c_ok "Janus 多模态  http://127.0.0.1:8000  ✅"
  else
    c_bad "Janus 多模态  未启动（实时分析+等级判定将不可用）"
  fi
  # STUN/TURN —— Janus 的 WebRTC 穿透依赖。
  # ⚠ 这是**系统级 systemd 服务**（coturn），既不随本项目 start 拉起，也不随 stop 关闭，
  #   且已 enabled 开机自启。这里只做**只读探活**：systemctl is-active 无需 sudo，
  #   因此不破坏本脚本"无需提权"的前提。实时链路连不通时，先看这一行。
  #   想改它的状态请直接对 systemd 操作（见下面的 tips），不要由本项目脚本代管——
  #   本项目 stop 的语义是"停本项目进程"，不该顺手关掉可能被其他 WebRTC 共享的系统服务。
  if systemctl is-active --quiet coturn 2>/dev/null; then
    c_ok "STUN/TURN     coturn :3478 ✅（systemd 服务，独立于本项目）"
  else
    c_warn "STUN/TURN     coturn 未运行 → Janus 的 WebRTC 穿透可能失效"
    c_warn "              启动 sudo systemctl start coturn ｜ 关闭 sudo systemctl stop coturn ｜ 重启 sudo systemctl restart coturn"
    c_warn "              查看 sudo systemctl status coturn ｜ 开机自启 sudo systemctl enable coturn"
  fi
  # pipeline 环境
  if [ -d "$VENDOR" ] && [ -x "$BASE_PY" ]; then
    c_ok "pipeline 环境 vendor ✅（解释器 ${BASE_PY}）"
  else
    c_bad "pipeline 环境 缺 vendor 或解释器 → 跑 ./deploy/skyeyes.sh env"
  fi
  echo
  echo "  GPU: $(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null | head -1)"
}

# ─────────────────────────── 构建 ───────────────────────────

cmd_env() { exec "$PROJ_DIR/deploy/setup_env.sh" "${1:-}"; }

# Ubuntu 的系统 python3 常常没装 `python3-venv`（缺 ensurepip），
# `python3 -m venv` 会以 "You may need to use sudo with that command" 收场。
# 所以按可用性挑一个真能建 venv 的解释器。
VENV_PY=""
pick_venv_python() {
  VENV_PY=""
  local cand
  for cand in "${SKYEYES_VENV_PY:-}" python3 "$HOME/miniconda3/bin/python" "$HOME/anaconda3/bin/python"; do
    [ -n "$cand" ] || continue
    if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import ensurepip' >/dev/null 2>&1; then VENV_PY="$cand"; return 0; fi
    fi
  done
  return 1
}

cmd_venv() {
  if [ -x "$BACKEND_VENV/bin/uvicorn" ]; then
    echo "server/.venv 已存在，跳过"
    "$BACKEND_VENV/bin/python" -c "import fastapi,uvicorn,httpx;print('  依赖 ok')"
    return 0
  fi
  if ! pick_venv_python; then
    c_bad "找不到能建 venv 的 python（需要 ensurepip）。可用 SKYEYES_VENV_PY 指定"
    return 1
  fi
  echo "用 ${VENV_PY} 创建 server/.venv ..."
  rm -rf "$BACKEND_VENV"
  "$VENV_PY" -m venv "$BACKEND_VENV"
  "$BACKEND_VENV/bin/pip" install --quiet --upgrade pip
  "$BACKEND_VENV/bin/pip" install --quiet -r "$PROJ_DIR/server/requirements.txt"
  "$BACKEND_VENV/bin/python" -c "import fastapi,uvicorn,httpx;print('  依赖 ok')"
}

# 解析出一个 >= 20 的 node，必要时下载到项目本地。结果放进 NODE_BIN。
ensure_node() {
  NODE_BIN=""
  local major=""
  if command -v node >/dev/null 2>&1; then
    major="$(node -v | sed 's/^v//' | cut -d. -f1)"
    [ "${major:-0}" -ge "$NODE_MIN_MAJOR" ] && NODE_BIN="$(command -v node)"
  fi
  if [ -z "$NODE_BIN" ] && [ -x "$NODE_DIR/bin/node" ]; then
    major="$("$NODE_DIR/bin/node" -v | sed 's/^v//' | cut -d. -f1)"
    [ "${major:-0}" -ge "$NODE_MIN_MAJOR" ] && NODE_BIN="$NODE_DIR/bin/node"
  fi
  if [ -z "$NODE_BIN" ]; then
    echo "  · 系统 node 不满足 >= ${NODE_MIN_MAJOR}，下载 Node ${NODE_VERSION} 到 ${NODE_DIR}"
    local arch="x64" tar="/tmp/skyeyes-node-dl.tar.xz"
    [ "$(uname -m)" = "aarch64" ] && arch="arm64"
    mkdir -p "$NODE_DIR"
    curl -sSL --retry 3 -o "$tar" \
      "https://cdn.npmmirror.com/binaries/node/${NODE_VERSION}/node-${NODE_VERSION}-linux-${arch}.tar.xz" \
      || { c_bad "Node 下载失败"; return 1; }
    tar xf "$tar" -C "$NODE_DIR" --strip-components=1 && rm -f "$tar" || return 1
    NODE_BIN="$NODE_DIR/bin/node"
  fi
  echo "  · 使用 $("$NODE_BIN" -v)  ($NODE_BIN)"
  return 0
}

cmd_frontend() {
  ensure_node || return 1
  local bindir npm_bin
  bindir="$(dirname "$NODE_BIN")"
  npm_bin="$bindir/npm"
  cd "$PROJ_DIR/web"
  # 用 npm ci 而不是 install：严格按 package-lock.json 装，
  # 保证服务器构建出的依赖树与本地一致。npm 10 能正确处理
  # 跨平台的 optionalDependencies（npm 9 会漏掉 oxide 的 linux 原生包，
  # 表现为 "Cannot find native binding"）。
  echo "npm ci（按 lockfile 装依赖）"
  PATH="$bindir:$PATH" "$npm_bin" ci --registry=https://registry.npmmirror.com --no-audit --no-fund
  echo "npm run build"
  PATH="$bindir:$PATH" "$npm_bin" run build
  echo
  du -sh "$DIST" 2>/dev/null | sed 's/^/  dist: /'
  [ -f "$DIST/index.html" ] && c_ok "前端已构建：$DIST/index.html" || c_bad "没有 index.html，构建异常"
}

cmd_logs() {
  local name="${1:-backend}"
  tail -n 60 -f "$LOGS/$name.log"
}

case "${1:-status}" in
  start)    cmd_start ;;
  stop)     cmd_stop ;;
  restart)  cmd_stop; echo; cmd_start ;;
  status)   cmd_status ;;
  logs)     cmd_logs "${2:-backend}" ;;
  env)      cmd_env "${2:-}" ;;
  venv)     cmd_venv ;;
  frontend) cmd_frontend ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs [名字]|env|venv|frontend}" >&2
    exit 2
    ;;
esac
