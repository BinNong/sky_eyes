#!/usr/bin/env bash
# 卸载服务器上的 cpolar（纯本地方式，不依赖联网下载卸载脚本）
#
# 用法：
#   sudo bash remove_cpolar.sh              # 删除 cpolar 本体（交互确认）
#   sudo bash remove_cpolar.sh --yes        # 跳过确认（无人值守）
#   sudo bash remove_cpolar.sh --purge --yes    # 连同安装包与旧备份一并删除
#
# 背景：
#   cpolar 于 2026-04-14 由官方 install-release-cpolar.sh 首次安装，
#   2026-09-23 首次移除，2026-09-29 离线重装（当时官方域名被网络拦截，
#   包由本地下载后 tar|ssh 传入 ~/cpolar-install/）。
#   同日用户确认改用「快解析」（~/YunDNSClient，走环回 127.0.0.1:8787），
#   cpolar 不再需要，故第二次移除。
#
#   官方卸载方式 `curl -L .../install-release-cpolar.sh | sudo bash -s -- --remove`
#   需要联网，而这台机器连不上 cpolar 官网（TLS 被 RST + 明文被拦截页），
#   所以一律用手工等价步骤，离线可用。
#
# ⚠ 家目录陷阱：本脚本以 root 运行，`$HOME` 会是 /root，
#   所以下面凡涉及 USER 家目录的路径都显式推导，不用 `~`。

set -euo pipefail

ASSUME_YES=0
PURGE=0
for arg in "$@"; do
  case "${arg}" in
    --yes|-y)      ASSUME_YES=1 ;;
    --purge)       PURGE=1 ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *)
      echo "未知参数：${arg}（可用：--yes / --purge / --help）" >&2
      exit 2
      ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "错误：需要 root 权限。请用： sudo bash $0 [--purge] [--yes]" >&2
  exit 1
fi

# 从 SUDO_USER 推导目标用户家目录，回退到固定路径
TARGET_USER="${SUDO_USER:-USER}"
TARGET_HOME="$(getent passwd "${TARGET_USER}" 2>/dev/null | cut -d: -f6)"
[ -n "${TARGET_HOME}" ] || TARGET_HOME="/home/USER"
USER_CPOLAR_DIR="${TARGET_HOME}/.cpolar"
INSTALL_DIR="${TARGET_HOME}/cpolar-install"
OLD_BACKUP="${TARGET_HOME}/cpolar-backup-20260923"

echo "=============================================================="
echo " 即将移除 cpolar（目标用户 ${TARGET_USER}，家目录 ${TARGET_HOME}）"
echo "=============================================================="
cat <<LIST
 当前状态（无需先手动停服务）：
   进程   : $(pgrep -c cpolar 2>/dev/null || echo 0) 个
   service: $(systemctl is-active cpolar 2>/dev/null || echo inactive) / $(systemctl is-enabled cpolar 2>/dev/null || echo disabled)
   9200   : $(ss -lnt 2>/dev/null | grep -qw 9200 && echo "仍在监听" || echo "未监听")

 以下内容会被删除：
 [服务]  cpolar.service / cpolar@.service 的 stop + disable + 注销
 [unit]  /etc/systemd/system/cpolar.service
         /etc/systemd/system/cpolar@.service
         /etc/systemd/system/cpolar.service.d/
         /etc/systemd/system/cpolar@.service.d/
 [程序]  /usr/local/bin/cpolar                (v3.3.12, 19 MB)
         /usr/bin/cpolar                     (软链，删后不留坏链)
 [配置]  /usr/local/etc/cpolar/               (cpolar.yml, 权限 666, 含明文 authtoken)
         ${USER_CPOLAR_DIR}/                  (家目录副本，09-29 手动运行 cpolar 时生成)
 [日志]  /var/log/cpolar/

 会随之失效的隧道（账号 USER_EMAIL，配置里是 demo 默认值）：
   - ssh      tcp  -> 22       [本机 SSH 实际在 3009，22 无服务 → 这条本就死的]
   - website  http -> 8080     [8080 无服务 → 这条也是死的]
LIST

if [ "${PURGE}" -eq 1 ]; then
  echo
  echo " --purge 已启用，额外删除："
  echo "   ${INSTALL_DIR}/        (19 MB 离线安装包)"
  echo "   ${OLD_BACKUP}/         (09-23 的配置备份，内含 authtoken)"
fi

echo
echo " 不受影响（本脚本不会碰）："
echo "   · 快解析 ~/YunDNSClient/（当前在用的入口，走环回 127.0.0.1:8787）"
echo "   · crontab 里 */5 的 YunDnsCtl check 保活任务"
echo "   · 服务器上任何监听端口（cpolar 当前没在监听）"
echo "=============================================================="
echo

if [ "${ASSUME_YES}" -ne 1 ]; then
  printf '确认执行删除？请输入 yes 后回车：'
  read -r ans
  if [ "${ans}" != "yes" ]; then
    echo "已取消，未做任何修改。"
    exit 0
  fi
fi

echo
echo "[1/7] 停止服务 ..."
systemctl stop cpolar 2>/dev/null || true
systemctl stop 'cpolar@'* 2>/dev/null || true

echo "[2/7] 禁用开机自启 ..."
systemctl disable cpolar 2>/dev/null || true

echo "[3/7] 删除 systemd unit ..."
rm -f  /etc/systemd/system/cpolar.service
rm -f  /etc/systemd/system/cpolar@.service
rm -rf /etc/systemd/system/cpolar.service.d
rm -rf /etc/systemd/system/cpolar@.service.d

echo "[4/7] 删除程序（含软链，避免残留坏链）..."
rm -f  /usr/local/bin/cpolar
rm -f  /usr/bin/cpolar

echo "[5/7] 删除配置与日志（含家目录副本）..."
rm -rf /usr/local/etc/cpolar
rm -rf /var/log/cpolar
rm -rf "${USER_CPOLAR_DIR}"

echo "[6/7] 重载 systemd ..."
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "[7/7] 核查结果 ..."
if [ "${PURGE}" -eq 1 ]; then
  echo "      清理安装包与旧备份 ..."
  rm -rf "${INSTALL_DIR}"
  rm -rf "${OLD_BACKUP}"
fi

echo
echo "================== 移除后核查 =================="
printf ' 进程残留        : '; pgrep -a cpolar 2>/dev/null || echo "无"
printf ' unit 残留       : '; (systemctl list-unit-files 2>/dev/null | grep -i cpolar) || echo "无"
printf ' 9200 管理端口   : '; (ss -lnt 2>/dev/null | grep -w 9200) || echo "已释放"
printf ' /usr/local/bin  : '; (ls /usr/local/bin/cpolar 2>/dev/null) || echo "已删除"
printf ' /usr/bin 软链   : '; (ls /usr/bin/cpolar 2>/dev/null) || echo "已删除"
printf ' 配置目录        : '; (ls -d /usr/local/etc/cpolar 2>/dev/null) || echo "已删除"
printf ' 家目录副本      : '; (ls -d "${USER_CPOLAR_DIR}" 2>/dev/null) || echo "已删除"
printf ' 日志目录        : '; (ls -d /var/log/cpolar 2>/dev/null) || echo "已删除"
if [ "${PURGE}" -eq 1 ]; then
  printf ' 安装包          : '; (ls -d "${INSTALL_DIR}" 2>/dev/null) || echo "已删除"
  printf ' 旧备份          : '; (ls -d "${OLD_BACKUP}" 2>/dev/null) || echo "已删除"
else
  printf ' 安装包(保留)    : '; (ls -d "${INSTALL_DIR}" 2>/dev/null) || echo "不存在"
  printf ' 旧备份(保留)    : '; (ls -d "${OLD_BACKUP}" 2>/dev/null) || echo "不存在"
fi
echo "================================================"
echo
echo "---------- 对照检查：快解析应完全不受影响 ----------"
printf ' GNTCPRPClient 进程 : '; pgrep -f GNTCPRPClient >/dev/null 2>&1 && echo "在跑 ✅" || echo "⚠ 不在跑，请检查快解析"
printf ' crontab 保活任务   : '; n="$(crontab -l -u "${TARGET_USER}" 2>/dev/null | grep -c YunDnsCtl || true)"; [ "${n}" -gt 0 ] && echo "${n} 条 ✅（未被改动）" || echo "⚠ 未找到"
printf ' 快解析主进程连接    : '; (ss -tnp 2>/dev/null | grep -q GNTCPRPClient) && echo "有活跃连接 ✅" || echo "（当前无活跃连接，空闲属正常）"
echo "===================================================="
echo
if [ "${PURGE}" -ne 1 ]; then
  echo " 提示：本次保留了 ${INSTALL_DIR}/ 与 ${OLD_BACKUP}/。"
  echo "       若确认彻底弃用 cpolar，可执行： sudo bash $0 --purge --yes"
fi
