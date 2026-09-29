#!/usr/bin/env bash
# 从本机开一条 SSH 隧道到服务器上部署的 Sky Eyes，然后用浏览器访问。
#
#   ./scripts/remote_access.sh              # 前台保持（Ctrl-C 断开）
#   ./scripts/remote_access.sh --daemon     # 后台常驻，日志 /tmp/skyeyes_access_tunnel.log
#   ./scripts/remote_access.sh --stop       # 后台那条停掉
#
# 然后打开： http://127.0.0.1:8888
#
# ⚠ 隧道现在是**备选路径**，不是唯一路径。2026-09-23 实测修正了此前写在这里的错误结论。
#
#   曾经的结论是"只放通了 3009"，那是**错的**。真相：这条网络**没有端口白名单**——
#   在服务器上随便起一个 `0.0.0.0:45678`，本机立刻可达（HTTP 200）；起在
#   `127.0.0.1:45679` 的则不通。也就是说，"能不能被访问"只取决于服务 bind 在哪个地址。
#   当初扫到 8787 不通，是因为**我们自己把它绑在了 127.0.0.1** —— 拿自己的配置
#   去"验证"了网络的限制，典型的自证循环。
#
#   现在 deploy/skyeyes.sh 默认绑 0.0.0.0，所以本机可以直接开：
#         http://SERVER_IP:8787
#
#   什么时候仍然需要这条隧道：
#     · 服务器上把服务绑回了 127.0.0.1（SKYEYES_HOST=127.0.0.1 ./deploy/skyeyes.sh restart）
#     · 想避免把这个**没有认证**的服务暴露给同网段
#     · 从别的机器访问，或服务器 IP 变了
#
# 隧道只需要一条就够：前端静态与 /api 都由服务器上的 :8787 提供（同源），
# Janus(:8000) 与检索编码(:8001) 是后端在服务器内部自己调的，不需要本地可达。
set -euo pipefail

REMOTE="${SKYEYES_REMOTE:-USER@SERVER_IP}"
SSH_PORT="${SKYEYES_SSH_PORT:-3009}"
LOCAL_PORT="${SKYEYES_LOCAL_PORT:-8888}"
REMOTE_PORT="${SKYEYES_APP_PORT:-8787}"
LOG=/tmp/skyeyes_access_tunnel.log

url="http://127.0.0.1:${LOCAL_PORT}"

if [[ "${1:-}" == "--stop" ]]; then
  found=0
  while read -r p; do
    [ -n "$p" ] || continue
    kill "$p" 2>/dev/null && { echo "[OK] 已停隧道 pid=${p}"; found=1; }
  done < <(pgrep -f "ssh.*-L 127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" || true)
  [ "$found" -eq 1 ] || echo "[INFO] 没找到在跑的隧道"
  exit 0
fi

if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[INFO] 本地 :${LOCAL_PORT} 已在监听，直接用： $url"
  exit 0
fi

SSH_ARGS=(-o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3
          -p "$SSH_PORT" -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$REMOTE")

if [[ "${1:-}" == "--daemon" ]]; then
  nohup ssh -o BatchMode=yes "${SSH_ARGS[@]}" </dev/null > "$LOG" 2>&1 &
  disown 2>/dev/null || true
  sleep 4
  # 本机装有 HTTP_PROXY，环回请求必须显式绕开代理，否则会走代理而失败
  code="$(curl -sS --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' "${url}/api/health" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    echo "[OK] 隧道就绪：$url   （本地 :${LOCAL_PORT} → 服务器 :${REMOTE_PORT}）"
  else
    echo "[WARN] 隧道进程已起，但 ${url}/api/health 返回 '$code'。日志尾部："
    tail -5 "$LOG" || true
    echo "       大概率是服务器上的后端没起：在服务器执行 ./deploy/skyeyes.sh start"
    exit 1
  fi
else
  echo "[INFO] 前台保持隧道：$url  （Ctrl-C 断开）"
  echo "       浏览器打开后即可使用；若页面空白，先确认服务器上跑着 ./deploy/skyeyes.sh status"
  exec ssh -o BatchMode=yes "${SSH_ARGS[@]}"
fi
