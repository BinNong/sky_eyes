#!/usr/bin/env bash
# 建立到远程 Janus 多模态服务的 SSH 本地端口转发
#
# 背景：SERVER_IP 仅放通 3009 端口，8000 端口无法直连，
#       因此通过 SSH 隧道把远程 8000 映射到本地 127.0.0.1:8000。
#
# 用法:
#   ./scripts/janus_tunnel.sh              # 前台运行，Ctrl-C 断开
#   nohup ./scripts/janus_tunnel.sh &      # 后台运行
#   LOCAL_PORT=9000 ./scripts/janus_tunnel.sh
#
# 连接信息见项目根目录 remote_server.md
set -euo pipefail

HOST="${JANUS_SSH_HOST:-SERVER_IP}"
SSH_PORT="${JANUS_SSH_PORT:-3009}"
SSH_USER="${JANUS_SSH_USER:-USER}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
REMOTE_PORT="${REMOTE_PORT:-8000}"

echo "[INFO] ${HOST}:${SSH_PORT}  ->  本地 127.0.0.1:${LOCAL_PORT}"
echo "[INFO] 断开请按 Ctrl-C"

exec ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -p "${SSH_PORT}" \
  -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  "${SSH_USER}@${HOST}"
