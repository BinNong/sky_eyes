#!/usr/bin/env bash
# Sky Eyes 视频检索编码服务 —— 停止
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"

PORT="${SKYEYES_CLIP_PORT:-8001}"
PID_FILE="logs/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[INFO] 未找到 ${PID_FILE}，尝试按端口清理"
  PID="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $NF}' | sed 's/.*pid=\([0-9]*\).*/\1/' | head -1)"
  if [[ -n "${PID:-}" ]]; then
    kill "$PID" && echo "[OK] 已停止 pid=$PID"
  else
    echo "[INFO] 端口 $PORT 上没有监听进程"
  fi
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  for _ in $(seq 1 15); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 1
  done
  kill -0 "$PID" 2>/dev/null && { echo "[WARN] 优雅退出超时，强制 kill"; kill -9 "$PID"; }
  echo "[OK] 已停止 pid=$PID"
else
  echo "[INFO] pid=$PID 已不在运行"
fi

rm -f "$PID_FILE"
sleep 1
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "[WARN] 端口 $PORT 仍在监听"
else
  echo "[OK] 端口 $PORT 已释放"
fi
