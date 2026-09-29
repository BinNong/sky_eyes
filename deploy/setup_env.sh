#!/usr/bin/env bash
# 在服务器上准备 pipeline.py 的运行环境。
#
#   ./deploy/setup_env.sh            # 安装 + 自检
#   ./deploy/setup_env.sh --check    # 只自检，不安装
#
# 不需要 sudo，也**不改动服务器上任何既有环境**。
#
# ── 做法与理由 ─────────────────────────────────────────────
# pipeline.py 需要 torch + ultralytics + opencv。远端 Janus 的 .venv 里已经有
# torch 2.6.0+cu124 / torchvision / numpy / pillow / requests，缺的只有
# ultralytics 和 opencv。所以把缺的这部分用 `pip install --target` 装进项目
# 自己的 vendor/，运行时用 PYTHONPATH 引入。
#
# 为什么不另建一个 conda 环境把 torch 再装一遍：
#   1) 要重下约 2.5GB 的 CUDA 版 torch，而远端已经有一份能用的；
#   2) `--target` 不写入任何 site-packages —— **Janus 的环境零改动**。
#      那是另一条产线的服务，不能因为这次部署把它弄坏。
#
# 代价（明写出来）：pipeline 因此**依赖 Janus 的 venv 继续存在**。
# 若哪天那份 venv 被重建或删掉，这里会失效——重跑本脚本即可，但 torch 得跟着它。
#
# ── 版本策略 ───────────────────────────────────────────────
# **影响检测结果的**（ultralytics / opencv / pyyaml）按本地 visual_search 环境钉死，
# 这样服务器算出的框与本地生成的演示数据可比；纯胶水依赖（matplotlib 那一串）
# 只给包名，不钉版本——它们不影响检测，钉了反而容易因缺少对应 py3.12 wheel 而装不上。
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_PY="${SKYEYES_BASE_PY:-/remote/Janus/.venv/bin/python}"
VENDOR="${SKYEYES_VENDOR:-$PROJ_DIR/vendor}"
CHECK_ONLY=0
for a in "$@"; do
  [ "$a" = "--check" ] && CHECK_ONLY=1
done

echo "项目目录 : $PROJ_DIR"
echo "基础解释器: $BASE_PY"
echo "vendor   : $VENDOR"
echo

if [ ! -x "$BASE_PY" ]; then
  echo "[错误] 找不到基础解释器 $BASE_PY" >&2
  echo "       用 SKYEYES_BASE_PY=/path/to/python 指定" >&2
  exit 1
fi
echo "基础解释器版本: $("$BASE_PY" -V 2>&1)"

# 检测结果强相关的，钉死版本
PINNED=(
  "ultralytics==8.3.76"
  "ultralytics-thop==2.0.14"
  "opencv-python-headless==4.8.0.74"
  "pyyaml==6.0.2"
)
# 纯胶水依赖，不钉版本。列全是为了配合 --no-deps（见下方说明）
GLUE=(
  matplotlib pandas scipy tqdm psutil py-cpuinfo seaborn
  contourpy cycler fonttools kiwisolver packaging pyparsing
  python-dateutil pytz tzdata
)

if [ "$CHECK_ONLY" -eq 0 ]; then
  echo
  echo "[1/2] 安装到 $VENDOR"
  echo "      必须带 --no-deps：pip 用 --target 时**看不到基础环境已有的包**，"
  echo "      不加就会把 numpy/torch/torchvision 又装一份到 vendor/ 里，"
  echo "      反而把基础环境那套遮住（那是这套方案最容易踩的坑）。"
  mkdir -p "$VENDOR"
  "$BASE_PY" -m pip install --quiet --upgrade --target "$VENDOR" \
    --no-deps "${PINNED[@]}" "${GLUE[@]}"
  echo "      完成"
else
  echo
  echo "[1/2] 跳过安装（--check）"
fi

echo
echo "[2/2] 自检"
# YOLO_AUTOINSTALL=false 很关键：ultralytics 默认会"发现缺依赖就自己 pip install"，
# 那会**直接写进 Janus 的 venv**——正是本方案要避免的事。缺什么宁可报错。
export PYTHONPATH="$VENDOR${PYTHONPATH:+:$PYTHONPATH}"
export YOLO_AUTOINSTALL=false
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$PROJ_DIR/.ultralytics}"

"$BASE_PY" - "$PROJ_DIR" <<'PYEOF'
import os, shutil, subprocess, sys, tempfile
import numpy as np

proj = sys.argv[1]
fails, warns = [], []

def section(t):
    print()
    print(f"── {t} " + "─" * max(0, 60 - len(t)))

# ---------- 1. 关键库 ----------
section("关键库")
import torch
import cv2
import ultralytics
from PIL import Image
print(f"  torch       {torch.__version__}")
print(f"  torchvision {__import__('torchvision').__version__}")
print(f"  ultralytics {ultralytics.__version__}")
print(f"  opencv      {cv2.__version__}")
print(f"  numpy       {np.__version__}")
print(f"  pillow      {__import__('PIL').__version__}")
if not torch.cuda.is_available():
    fails.append("torch 看不到 CUDA")
    print("  CUDA        不可用 ❌")
else:
    print(f"  CUDA        {torch.cuda.get_device_name(0)} ✅")

# ---------- 2. 真的跑一次检测 ----------
section("检测（用项目自带权重，跑视频里的整帧）")
model_path = os.path.join(proj, "models", "trained_model.pt")
video = os.path.join(proj, "source", "raw_videos", "burstling_street.mp4")
if not os.path.isfile(model_path):
    fails.append(f"找不到权重 {model_path}")
    print(f"  ❌ 找不到权重 {model_path}")
elif not os.path.isfile(video):
    warns.append(f"找不到源视频 {video}，跳过检测自检")
    print(f"  ⚠ 找不到源视频，跳过检测自检")
else:
    from ultralytics import YOLO
    m = YOLO(model_path)
    names = m.names
    print(f"  权重类别    {names}")
    # ⚠ 探针必须用**整帧**，不能用 output/accident_frames 里的 ROI 裁剪：
    #   这个模型是在整帧上训练的，紧致裁剪里常常没有可检出的目标。
    #   第一次写这段自检时就选错了，得到"0 个框"的误导性结论。
    cap = cv2.VideoCapture(video)
    probes = []
    for fi in (100, 600, 810):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi - 1)
        ok, fr = cap.read()
        if ok:
            probes.append((fi, fr))
    cap.release()
    if not probes:
        fails.append("源视频读不出任何帧")
        print("  ❌ 源视频读不出帧")
    total_boxes = 0
    for fi, fr in probes:
        r = m.predict(fr, conf=0.4, verbose=False)[0]
        n = len(r.boxes)
        total_boxes += n
        cls = [names[int(c)] for c in r.boxes.cls]
        print(f"  帧 {fi:>4}    {n:>2} 个框  {cls}")
    if total_boxes == 0:
        fails.append("整帧检测一个框都没检出 —— 权重或环境有问题")
        print("  ❌ 整帧都检不出任何目标，不正常")
    else:
        acc = [
            (fi, names[int(c)], round(float(p), 3))
            for fi, fr in probes
            for r in [m.predict(fr, conf=0.4, verbose=False)[0]]
            for c, p in zip(r.boxes.cls, r.boxes.conf)
            if names[int(c)].lower() == "accident"
        ]
        print(f"  Accident 类: {acc or '（这几帧没有）'}")

# ---------- 3. 标注视频编码（浏览器能不能播的关键）----------
section("标注视频编码（决定浏览器能否播放）")
tmp = tempfile.mkdtemp(prefix="skyeyes_codec_")
probe = os.path.join(tmp, "probe.mp4")
w = cv2.VideoWriter(probe, cv2.VideoWriter_fourcc(*"avc1"), 25, (64, 64))
can_direct = w.isOpened()
w.release()
shutil.rmtree(tmp, ignore_errors=True)

ff = shutil.which("ffmpeg")
has_x264 = False
if ff:
    enc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    has_x264 = "libx264" in enc

print(f"  直接写 avc1   {'可以' if can_direct else '不行（pip 的 opencv wheel 不含 H.264 编码器）'}")
print(f"  系统 ffmpeg   {ff or '无'}")
print(f"  libx264       {'有' if has_x264 else '没有'}")
if can_direct:
    print("  → 策略 direct-h264：检测时直接写出 H.264")
elif ff and has_x264:
    print("  → 策略 via-ffmpeg：先写 mp4v，检测完由 detector.py 自动转成 H.264")
elif ff:
    fails.append("ffmpeg 没有 libx264，无法产出浏览器可播的标注视频")
    print("  ❌ ffmpeg 缺 libx264")
else:
    fails.append("既不能直写 avc1 也没有 ffmpeg → 标注视频将是不能播的 mp4v")
    print("  ❌ 两条路都不通，标注视频浏览器无法播放")

# ---------- 汇总 ----------
print()
print("=" * 64)
if fails:
    print("❌ 自检未通过：")
    for f in fails:
        print(f"   · {f}")
if warns:
    print("⚠ 需要注意：")
    for w_ in warns:
        print(f"   · {w_}")
if not fails and not warns:
    print("✅ 环境自检全部通过")
print("=" * 64)
sys.exit(1 if fails else 0)
PYEOF

echo
echo "完成。运行 pipeline 时这样引入 vendor："
echo "  PYTHONPATH=$VENDOR $BASE_PY $PROJ_DIR/pipeline.py --help"
echo "（deploy/skyeyes.sh 已经替你设好了）"
