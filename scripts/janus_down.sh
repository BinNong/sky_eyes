#!/usr/bin/env bash
# 一键关闭 Janus 多模态服务（释放服务器 GPU 资源）
#   1) 关闭本地 SSH 隧道
#   2) 停止远程 uvicorn 服务
#   3) 确认 GPU 已释放
#
# 用法:
#   ./scripts/janus_down.sh
#   JANUS_SSH_PASS='密码' ./scripts/janus_down.sh
set -euo pipefail

HOST="${JANUS_SSH_HOST:-SERVER_IP}"
SSH_PORT="${JANUS_SSH_PORT:-3009}"
SSH_USER="${JANUS_SSH_USER:-USER}"
REMOTE_DIR="${JANUS_REMOTE_DIR:-/remote/Janus}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
REMOTE_PORT="${REMOTE_PORT:-8000}"

SSH_ARGS=(
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=15
  -p "$SSH_PORT"
)
if [[ -n "${JANUS_SSH_PASS:-}" ]]; then
  command -v sshpass >/dev/null 2>&1 || { echo "[ERROR] 未找到 sshpass，请改用 SSH 密钥登录" >&2; exit 1; }
  SSH_CMD=(sshpass -p "$JANUS_SSH_PASS" ssh "${SSH_ARGS[@]}")
else
  SSH_CMD=(ssh "${SSH_ARGS[@]}")
fi

remote() { "${SSH_CMD[@]}" "${SSH_USER}@${HOST}" "$@" < /dev/null; }

# ---------- 1. 关闭本地隧道 ----------
echo "[1/3] 关闭本地 SSH 隧道 ..."
if pkill -f "ssh.*-L 127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" 2>/dev/null; then
  echo "      已关闭"
else
  echo "      无运行中的隧道"
fi
rm -f /tmp/janus_tunnel.pid

# ---------- 2. 停止远程服务 ----------
echo "[2/3] 停止远程服务 ..."
remote "cd '${REMOTE_DIR}' && ./stop.sh"

# ---------- 3. 确认 GPU 已释放 ----------
echo "[3/3] 检查服务器状态 ..."
remote "echo -n '  8000 端口: '; ss -ltn 2>/dev/null | grep -q ':${REMOTE_PORT}' && echo '仍在监听 ✗' || echo '已释放 ✓'; echo -n '  GPU 显存: '; nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"

echo ""
echo "[OK] 服务已关闭（部署文件与模型权重保留，下次 ./scripts/janus_up.sh 即可恢复）"
