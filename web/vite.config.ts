import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * `/api` 一律代理到本地实时推理后端（server/app.py，默认 127.0.0.1:8787）。
 *
 * 走代理而不是让前端直连 8787 有两个好处：同源，不用处理 CORS；
 * 后端地址变了只改这一处，前端代码里全是相对路径 `/api/...`。
 *
 * 后端没启动时，代理会返回 500/连接拒绝——前端把任何失败都当成
 * "实时模式不可用"处理并降级回静态数据，演示不会中断。
 */
const API_TARGET = process.env.SKYEYES_API || 'http://127.0.0.1:8787'

const apiProxy = {
  '/api': {
    target: API_TARGET,
    changeOrigin: true,
    // SSE 不缓冲由后端响应头保证（server/app.py 设了 X-Accel-Buffering: no），
    // Vite 的 http-proxy 默认就是流式转发，这里不需要额外配置。
  },
}

/**
 * 端口可用环境变量覆盖。
 *
 * 为什么需要：本机 5173 已被另一个项目的 vite 占用（监听 `[::1]:5173`，IPv6），
 * 而本项目的 vite 监听 `127.0.0.1:5173`（IPv4）。两者并存时，
 * 地址栏输 `localhost:5173` 会因解析到 `::1` 而打开**另一个项目**——
 * 排查成本极高，因为页面看起来只是"不对"而不是"报错"。
 * 换到独立端口可彻底消除这个歧义。
 */
const PORT = Number(process.env.SKYEYES_PORT || 5173)

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: PORT,
    open: false,
    proxy: apiProxy,
  },
  preview: {
    port: Number(process.env.SKYEYES_PORT || 4173),
    proxy: apiProxy,
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
