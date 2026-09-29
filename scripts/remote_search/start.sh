#!/usr/bin/env bash
# Sky Eyes 视频检索编码服务 —— 后台启动（nohup），PID 写入 logs/server.pid
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"
mkdir -p logs

PORT="${SKYEYES_CLIP_PORT:-8001}"

if [[ -f logs/server.pid ]] && kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  echo "[WARN] 服务已在运行 (pid=$(cat logs/server.pid))，如需重启请先 ./stop.sh"
  exit 0
fi

# 端口被占（可能上次没写 pid 就退了）时明确报出来，别让新进程默默起不来
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "[ERROR] 端口 $PORT 已被占用，但 logs/server.pid 不存在。"
  echo "        先确认是谁：ss -ltnp | grep :$PORT"
  exit 1
fi

nohup ./run_server.sh > logs/server.log 2>&1 &
PID=$!
echo "$PID" > logs/server.pid

# 等它真正就绪：模型加载要几秒，直接返回会让人误以为"启动失败"
for _ in $(seq 1 60); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] 进程已退出，看日志：tail -40 $PROJ_DIR/logs/server.log" >&2
    rm -f logs/server.pid
    exit 1
  fi
  if curl -sS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "[OK] 已启动，pid=${PID}，监听 127.0.0.1:${PORT}"
    curl -sS -m 3 "http://127.0.0.1:$PORT/health"
    echo
    exit 0
  fi
  sleep 1
done

echo "[WARN] 60 秒内没通过健康检查，进程还在（pid=${PID}）但未就绪。"
echo "       看日志：tail -40 $PROJ_DIR/logs/server.log"
exit 1
