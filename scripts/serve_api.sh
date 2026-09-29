#!/usr/bin/env bash
# 启动实时推理后端（FastAPI，默认 127.0.0.1:8787）。
#
#   ./scripts/serve_api.sh            # 前台启动
#   PORT=8790 ./scripts/serve_api.sh  # 换端口
#
# 前置条件：
#   1) Janus 多模态服务已就绪：./scripts/janus_up.sh
#   2) visual_search conda 环境存在（跑 pipeline.py 用）
#
# 后端跑在 server/.venv 里——独立于 visual_search，不装 torch。
# 首次运行会自动建这个 venv 并装依赖。
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJ_DIR/server/.venv"
PORT="${PORT:-8787}"

# 推理环境（可以跑 pipeline.py 的那个）。不要用本 venv。
export SKYEYES_PY="${SKYEYES_PY:-/path/to/visual_search/bin/python}"

if [[ ! -x "$SKYEYES_PY" ]]; then
  echo "[ERROR] 找不到推理环境：$SKYEYES_PY" >&2
  echo "        用 SKYEYES_PY=/path/to/python 指定" >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "[1/2] 首次运行，创建 server/.venv ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$PROJ_DIR/server/requirements.txt"
  echo "      依赖安装完成"
else
  echo "[1/2] 复用已有的 server/.venv"
fi

echo "[2/2] 启动后端 http://127.0.0.1:${PORT}"
echo "      推理环境 SKYEYES_PY=$SKYEYES_PY"
echo "      接口文档     http://127.0.0.1:${PORT}/docs"
cd "$PROJ_DIR/server"
exec "$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port "$PORT" --log-level info
