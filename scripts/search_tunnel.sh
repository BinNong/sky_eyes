#!/usr/bin/env bash
# 建立到远端「视频检索编码服务」（:8001）的隧道。
#
# 远端检索服务绑在 127.0.0.1:8001，所以本机要用它就得走隧道。
# ⚠ 这条网络**没有端口白名单**——"需不需要隧道"只取决于服务绑在哪个地址
#   （绑 0.0.0.0 就可达、绑 127.0.0.1 就不通），详见 remote_server.md。
#   Janus 就是绑 0.0.0.0:8000 的，本机可直连，janus_tunnel.sh 只是备选。
# 检索服务用 :8001，两者互不影响，可以同时开。
#
# 用法：
#   ./scripts/search_tunnel.sh            # 前台保持（Ctrl-C 断开）
#   ./scripts/search_tunnel.sh --daemon   # 后台常驻，日志写 /tmp/skyeyes_search_tunnel.log
set -euo pipefail

REMOTE="${SKYEYES_REMOTE:-USER@SERVER_IP}"
SSH_PORT="${SKYEYES_SSH_PORT:-3009}"
LOCAL_PORT="${SKYEYES_CLIP_LOCAL_PORT:-8001}"
REMOTE_PORT="${SKYEYES_CLIP_PORT:-8001}"
REMOTE_DIR="${SKYEYES_REMOTE_DIR:-/remote/sky_eyes_retrieval}"
LOG=/tmp/skyeyes_search_tunnel.log

if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[INFO] 本地 :$LOCAL_PORT 已在监听，无需重复建立"
  exit 0
fi

SSH_ARGS=(-o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -p "$SSH_PORT" -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$REMOTE")

if [[ "${1:-}" == "--daemon" ]]; then
  nohup ssh -o BatchMode=yes "${SSH_ARGS[@]}" </dev/null > "$LOG" 2>&1 &
  disown 2>/dev/null || true
  sleep 4
  if curl -sS --noproxy '*' -m 8 "http://127.0.0.1:${LOCAL_PORT}/health" >/dev/null 2>&1; then
    echo "[OK] 隧道就绪（本地 :${LOCAL_PORT} → 远端 :${REMOTE_PORT}），日志 ${LOG}"
  else
    echo "[WARN] 隧道进程已起，但健康检查没过。日志尾部："
    tail -5 "$LOG" || true
    echo "       通常是远端服务没起：在远端执行 $REMOTE_DIR/start.sh"
    exit 1
  fi
else
  echo "[INFO] 前台保持隧道 本地 :${LOCAL_PORT} → ${REMOTE}:${REMOTE_PORT}（Ctrl-C 断开）"
  exec ssh -o BatchMode=yes "${SSH_ARGS[@]}"
fi
