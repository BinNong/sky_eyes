#!/usr/bin/env bash
# 一键启动 Janus 多模态服务
#   1) 在远程服务器上启动 uvicorn 服务
#   2) 等待服务就绪
#   3) 建立本地 SSH 隧道（本地 127.0.0.1:8000 -> 服务器 127.0.0.1:8000）
#   4) 从本机验证一次
#
# 用法:
#   ./scripts/janus_up.sh
#   JANUS_SSH_PASS='密码' ./scripts/janus_up.sh     # 未配置 SSH 密钥时
#
# 连接信息见 remote_server.md
set -euo pipefail

HOST="${JANUS_SSH_HOST:-SERVER_IP}"
SSH_PORT="${JANUS_SSH_PORT:-3009}"
SSH_USER="${JANUS_SSH_USER:-USER}"
REMOTE_DIR="${JANUS_REMOTE_DIR:-/remote/Janus}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
REMOTE_PORT="${REMOTE_PORT:-8000}"

# ---------- 组装 ssh 前缀（优先密钥，其次 sshpass）----------
SSH_ARGS=(
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=15
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -p "$SSH_PORT"
)
if [[ -n "${JANUS_SSH_PASS:-}" ]]; then
  command -v sshpass >/dev/null 2>&1 || { echo "[ERROR] 未找到 sshpass，请改用 SSH 密钥登录" >&2; exit 1; }
  SSH_CMD=(sshpass -p "$JANUS_SSH_PASS" ssh "${SSH_ARGS[@]}")
else
  # 没给密码 = 走密钥。此时**必须**禁掉密码提示：
  # ssh 的密码提示读的是 /dev/tty 而不是 stdin，`< /dev/null` 挡不住它，
  # 在无控制终端的环境（后台任务、CI）里会直接永久挂住——实测挂过 14 分钟没任何输出。
  # 禁掉之后密钥一旦失效会立刻报错退出，而不是静静地卡死。
  SSH_CMD=(ssh -o NumberOfPasswordPrompts=0 -o BatchMode=yes "${SSH_ARGS[@]}")
fi

remote() { "${SSH_CMD[@]}" "${SSH_USER}@${HOST}" "$@" < /dev/null; }

# ---------- 1. 启动远程服务 ----------
echo "[1/4] 启动远程服务 (${HOST}:${REMOTE_DIR}) ..."
remote "cd '${REMOTE_DIR}' && ./start.sh"

# ---------- 2. 等待服务就绪 ----------
echo "[2/4] 等待模型加载完成（首次加载约 30~60 秒）..."
if ! remote "for i in \$(seq 1 90); do curl -sS --max-time 3 -o /dev/null http://127.0.0.1:${REMOTE_PORT}/health && exit 0; sleep 2; done; exit 1"; then
  echo "[ERROR] 服务在 3 分钟内未就绪，请到服务器查看 ${REMOTE_DIR}/logs/server.log" >&2
  exit 1
fi
echo "      服务已就绪"

# ---------- 3. 建立本地隧道 ----------
echo "[3/4] 建立本地 SSH 隧道 127.0.0.1:${LOCAL_PORT} -> ${HOST}:${REMOTE_PORT} ..."
pkill -f "ssh.*-L 127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" 2>/dev/null || true
sleep 1

nohup "${SSH_CMD[@]}" -N -o ExitOnForwardFailure=yes \
  -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  "${SSH_USER}@${HOST}" </dev/null > /tmp/janus_tunnel.log 2>&1 &
#                                              ^^^^^^^^^^ 同上：隧道是常驻进程，
#                                              必须断开 stdin，否则出问题时会挂在提示符上
TUNNEL_PID=$!
echo "$TUNNEL_PID" > /tmp/janus_tunnel.pid

# ---------- 4. 本机验证 ----------
echo "[4/4] 从本机验证 ..."
for _ in $(seq 1 20); do
  if curl -sS --noproxy '*' --max-time 3 -o /dev/null "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null; then
    echo ""
    echo "[OK] 服务已就绪，隧道 pid=${TUNNEL_PID}"
    curl -sS --noproxy '*' "http://127.0.0.1:${LOCAL_PORT}/health"
    echo ""
    echo "[OK] 现在可以直接运行: python multimodal_understanding.py <图片或目录>"
    exit 0
  fi
  sleep 0.5
done

echo "[ERROR] 隧道已启动但本机无法访问，请查看 /tmp/janus_tunnel.log" >&2
exit 1
