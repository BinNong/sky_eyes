#!/usr/bin/env bash
# Sky Eyes 视频检索编码服务 —— 启动（前台，供 nohup / systemd 托管）
#
# 依赖来自 Janus 的 venv，但**不往里装任何东西**：cn_clip 用 pip --target 装在本目录
# 的 vendor/ 下，靠 PYTHONPATH 引入。这样共用 torch 的同时，Janus 的环境保持原样 ——
# Janus 是演示主线，不该被一个旁路功能影响。
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"

#: 提供 torch / torchvision / numpy / fastapi / uvicorn 的环境。
#: 默认复用 Janus 的 venv；如需独立环境，用 SKYEYES_CLIP_PYTHON 覆盖。
PYTHON="${SKYEYES_CLIP_PYTHON:-/remote/Janus/.venv/bin/python}"
MODEL_DIR="${SKYEYES_CLIP_MODEL:-$PROJ_DIR/model}"
PORT="${SKYEYES_CLIP_PORT:-8001}"
HOST="${SKYEYES_CLIP_HOST:-127.0.0.1}"

if [[ ! -x "$PYTHON" ]]; then
  echo "[ERROR] 找不到 python：$PYTHON" >&2
  echo "        用 SKYEYES_CLIP_PYTHON 指定一个带 torch 的解释器" >&2
  exit 1
fi

if [[ ! -f "$MODEL_DIR/pytorch_model.bin" ]]; then
  echo "[ERROR] 找不到权重：$MODEL_DIR/pytorch_model.bin" >&2
  echo "        先执行 scripts/search_deploy.sh 推送模型" >&2
  exit 1
fi

# --no-site-packages 关闭；这里只是显式声明依赖来源，便于排查
export PYTHONPATH="$PROJ_DIR/vendor${PYTHONPATH:+:$PYTHONPATH}"
export SKYEYES_CLIP_MODEL="$MODEL_DIR"

echo "[INFO] python   : $PYTHON"
echo "[INFO] model    : $MODEL_DIR"
echo "[INFO] vendor   : $PROJ_DIR/vendor"
echo "[INFO] listen   : http://$HOST:$PORT"

exec "$PYTHON" "$PROJ_DIR/service.py" --host "$HOST" --port "$PORT" --model-dir "$MODEL_DIR"
