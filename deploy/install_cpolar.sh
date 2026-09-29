#!/usr/bin/env bash
# cpolar 离线安装（3.3.12）—— 适用于官方域名被网络拦截、无法在线安装的机器
#
# 用法：
#   sudo bash install_cpolar.sh                 # 安装（交互确认，暂不写 token）
#   sudo bash install_cpolar.sh --yes           # 无人值守
#   sudo bash install_cpolar.sh <AUTHTOKEN>     # 安装并写入账号 token
#
# 为什么不能在线装：
#   本机对 www.cpolar.com 的 TLS 握手被中间设备 RST（curl 35 Connection reset），
#   明文 HTTP 返回「错误，访问被禁止」拦截页 → 官方一键脚本的下载步骤必然失败。
#   安装包已由本地下载后传到 ~/cpolar-install/，本脚本纯离线执行，
#   步骤与官方 install-release-cpolar.sh 等价。
#
# 与官方脚本的一处有意差异：
#   官方把 cpolar.yml 装成 666（world-writable），目的是让以 nobody 运行的
#   守护进程能读到它。这里改为 root:nogroup + 640：nobody 能读，
#   其他用户既不能读也不能改。注意不能简单改成 600 —— 那样 nobody 读不到，
#   服务会起不来。

set -euo pipefail

SRC="/home/USER/cpolar-install"
JSON_PATH="/usr/local/etc/cpolar"
BIN_PATH="/usr/local/bin/cpolar"
CFG="${JSON_PATH}/cpolar.yml"

ASSUME_YES=0
AUTHTOKEN=""
for arg in "$@"; do
  case "${arg}" in
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) AUTHTOKEN="${arg}" ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "错误：需要 root 权限。请用： sudo bash $0 [AUTHTOKEN]" >&2
  exit 1
fi

NOBODY_GROUP="$(id -gn nobody 2>/dev/null || echo nogroup)"

echo "=============================================================="
echo " 离线安装 cpolar"
echo "=============================================================="
echo " 安装包来源 : ${SRC}"
echo " 程序       : ${BIN_PATH}  (+ 软链 /usr/bin/cpolar)"
echo " 配置       : ${CFG}   [root:${NOBODY_GROUP} 640]"
echo " systemd    : /etc/systemd/system/cpolar.service, cpolar@.service"
echo " 日志       : /var/log/cpolar/"
if [ -n "${AUTHTOKEN}" ]; then
  echo " authtoken  : 本次写入"
else
  echo " authtoken  : 本次不写（装完可再补）"
fi
echo " 服务       : 已存在则先停；装完 enable + start"
echo "=============================================================="
if [ "${ASSUME_YES}" -ne 1 ]; then
  printf '确认执行？输入 yes 后回车：'
  read -r ans
  if [ "${ans}" != "yes" ]; then echo "已取消，未做任何修改。"; exit 0; fi
fi

echo
echo "[1/7] 校验安装包 ..."
for f in cpolar cpolar.demo.yml cpolar.service cpolar@.service; do
  if [ ! -f "${SRC}/${f}" ]; then
    echo "错误：缺少 ${SRC}/${f}" >&2
    exit 1
  fi
done
echo "      实测版本： $("${SRC}/cpolar" version)"

echo "[2/7] 安装二进制 ..."
if systemctl list-unit-files 2>/dev/null | grep -qw 'cpolar'; then
  systemctl stop cpolar 2>/dev/null || true
fi
install -m 755 "${SRC}/cpolar" "${BIN_PATH}"
ln -sf "${BIN_PATH}" /usr/bin/cpolar
echo "      ${BIN_PATH}  +  /usr/bin/cpolar"

echo "[3/7] 安装配置 ..."
install -d -m 755 "${JSON_PATH}"
if [ -f "${CFG}" ]; then
  echo "      已存在 ${CFG}，保留原有内容"
else
  install -m 640 "${SRC}/cpolar.demo.yml" "${CFG}"
  echo "      已由示例配置创建 ${CFG}"
fi

echo "[4/7] 安装 systemd unit ..."
install -m 644 "${SRC}/cpolar.service"  /etc/systemd/system/cpolar.service
install -m 644 "${SRC}/cpolar@.service" /etc/systemd/system/cpolar@.service
mkdir -p /etc/systemd/system/cpolar.service.d /etc/systemd/system/cpolar@.service.d

echo "[5/7] 准备日志目录 ..."
install -d -m 700 -o nobody -g "${NOBODY_GROUP}" /var/log/cpolar
install -m 644 -o nobody -g "${NOBODY_GROUP}" /dev/null /var/log/cpolar/access.log
install -m 644 -o nobody -g "${NOBODY_GROUP}" /dev/null /var/log/cpolar/error.log

echo "[6/7] 写入 authtoken ..."
if [ -n "${AUTHTOKEN}" ]; then
  "${BIN_PATH}" authtoken "${AUTHTOKEN}" -config "${CFG}" >/dev/null
  echo "      已写入"
else
  echo "      未提供，跳过"
fi
# 统一收紧权限：守护进程以 nobody 运行，必须组可读
chown "root:${NOBODY_GROUP}" "${CFG}"
chmod 640 "${CFG}"
echo "      权限： $(stat -c '%U:%G %a' "${CFG}")"

echo "[7/7] 注册并启动服务 ..."
systemctl daemon-reload
systemctl enable cpolar >/dev/null 2>&1 && echo "      已设为开机自启"
systemctl start cpolar || true

echo
echo "================== 安装后核查 =================="
printf ' 版本        : '; "${BIN_PATH}" version || true
printf ' 服务状态    : '; systemctl is-active cpolar || true
printf ' 开机自启    : '; systemctl is-enabled cpolar 2>/dev/null || true
printf ' 9200 管理页 : '; if ss -lnt 2>/dev/null | grep -qw 9200; then echo "监听中"; else echo "未监听"; fi
printf ' 配置权限    : '; stat -c '%U:%G %a' "${CFG}" || true
printf ' authtoken   : '; if grep -q '^authtoken:' "${CFG}" 2>/dev/null; then echo "已配置"; else echo "未配置"; fi
echo "================================================"

if ! systemctl is-active --quiet cpolar 2>/dev/null; then
  echo
  echo "⚠ 服务未处于 active 状态，最近日志："
  journalctl -u cpolar -n 20 --no-pager 2>/dev/null || true
fi

echo
echo "后续操作："
echo "  查看状态   : systemctl status cpolar"
echo "  管理界面   : http://<本机IP>:9200"
echo "  补写 token : sudo ${BIN_PATH} authtoken <TOKEN> -config ${CFG} && sudo systemctl restart cpolar"
echo "  取消自启   : sudo systemctl disable cpolar"
