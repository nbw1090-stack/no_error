import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
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
