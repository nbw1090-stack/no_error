import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 远程访问部署：放开监听地址，让 cloudflared 隧道能从本机回源 localhost:5173
    // （否则 vite 默认只监听 [::1]，IPv4 回源连不上）。
    host: true,
    // 允许穿透域名（app.<域名>）访问：vite 6 默认 allowedHosts:[] 会以 403
    // 拒绝非 localhost 的 Host 头。开发演示场景放开全部。
    allowedHosts: true,
    port: 5173,
    proxy: {
      '/api': {
        // 用 127.0.0.1（IPv4）而非 localhost：node 在 macOS 上把 localhost
        // 解析为 IPv6 (::1)，而后端 uvicorn 默认仅监听 IPv4 127.0.0.1，
        // 用 localhost 会导致所有 /api 代理请求 502（登录/会话/对话全失败）。
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
