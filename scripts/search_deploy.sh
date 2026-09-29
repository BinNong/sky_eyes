#!/usr/bin/env bash
# 把「视频检索编码服务」部署到带 GPU 的远端，并拉起。
#
# 为什么模型让远端自己下载，而不是从本地推：
#   本地 → 远端的 SSH 隧道要跟 Janus 抢带宽；远端到 modelscope 是直连，实测 ~7.6 MB/s。
#   代价是多一步校验——下面的 EXPECT_* 是本地那份权重的 sha256，
#   哈希对上才说明"远端跑的与本地做基准的是同一份权重"。
#
# 幂等：重复执行只会重推脚本、补缺的文件、重启服务。
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/remote_search" && pwd)"
REMOTE="${SKYEYES_REMOTE:-USER@SERVER_IP}"
SSH_PORT="${SKYEYES_SSH_PORT:-3009}"
RD="${SKYEYES_REMOTE_DIR:-/remote/sky_eyes_retrieval}"
PYTHON="${SKYEYES_CLIP_PYTHON:-/remote/Janus/.venv/bin/python}"
MS_BASE="https://www.modelscope.cn/api/v1/models/damo/multi-modal_clip-vit-base-patch16_zh/repo?Revision=master&FilePath="

# 本地基准权重的 sha256（damos/multi-modal_clip-vit-base-patch16_zh）
EXPECT_WEIGHTS="7198e28a6741c77c78df6ada4f4071aedb3ee3c57425f5fff0cf8603bc04a14d"
EXPECT_VOCAB="45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"
CN_CLIP_VERSION="1.6.0"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=15 -p "$SSH_PORT" "$REMOTE" "$@"; }

say "0/6 连通性与前置检查"
ssh_ "python3 -c 'print()' && ls -d $RD 2>/dev/null || echo '(远端目录尚未创建)'"
ssh_ "$PYTHON -c 'import torch,torchvision,numpy;print(\"  torch\",torch.__version__,\"| cuda\",torch.cuda.is_available())'"

say "1/6 创建目录"
ssh_ "mkdir -p $RD/model $RD/vendor $RD/logs && echo '  目录就绪'"

say "2/6 推送服务代码"
for f in service.py run_server.sh start.sh stop.sh; do
  ssh_ "cat > $RD/$f" < "$LOCAL_DIR/$f"
  printf '  ↑ %s\n' "$f"
done
ssh_ "chmod +x $RD/run_server.sh $RD/start.sh $RD/stop.sh && echo '  可执行位已设置'"

say "3/6 确保模型文件（缺失或哈希不符才下载）"
ssh_ "bash -s" <<EOF
set -e
RD=$RD
MS_BASE='$MS_BASE'
EXPECT_WEIGHTS='$EXPECT_WEIGHTS'
EXPECT_VOCAB='$EXPECT_VOCAB'
cd "\$RD/model"
need() {
  local f="\$1" want="\$2"
  if [ -f "\$f" ]; then
    local got=\$(sha256sum "\$f" | awk '{print \$1}')
    if [ "\$got" = "\$want" ]; then
      echo "  = \$f 已就绪且哈希一致"
      return 1
    fi
    echo "  ! \$f 哈希不符（现有 \${got:0:16}），重新下载"
  else
    echo "  + \$f 缺失，开始下载"
  fi
  curl -sSL --retry 3 -o "\$f" "\${MS_BASE}\${f}"
  local got=\$(sha256sum "\$f" | awk '{print \$1}')
  if [ "\$got" != "\$want" ]; then
    echo "  [ERROR] \$f 下载后哈希仍不符：\${got:0:16} != \${want:0:16}" >&2
    exit 1
  fi
  echo "  ✅ \$f 下载完成并校验通过（$(du -h "\$f" | cut -f1)）"
}
need pytorch_model.bin "\$EXPECT_WEIGHTS" || true
need vocab.txt "\$EXPECT_VOCAB" || true
for f in text_model_config.json vision_model_config.json; do
  [ -f "\$f" ] || curl -sSL --retry 3 -o "\$f" "\${MS_BASE}\${f}"
done
echo "  --- 最终 ---"
ls -la "\$RD/model"
EOF

say "4/6 安装 cn_clip 到 vendor/（--no-deps --target，不碰 Janus 的 venv）"
ssh_ "if [ -d $RD/vendor/cn_clip ]; then echo '  = 已存在，跳过'; else $PYTHON -m pip install --no-deps --quiet --target $RD/vendor cn_clip==$CN_CLIP_VERSION && echo '  装好了'; fi"
ssh_ "ls $RD/vendor"

say "5/6 重启服务"
ssh_ "$RD/stop.sh || true"
ssh_ "$RD/start.sh"

say "6/6 健康检查"
ssh_ "curl -sS -m 5 http://127.0.0.1:8001/health | python3 -m json.tool"

say "完成"
echo "本地要访问它，另开一条隧道： ./scripts/search_tunnel.sh"
