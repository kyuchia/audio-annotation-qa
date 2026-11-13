import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Where Django answers. Outside Docker Compose that is the host's own port 8000; under
// Compose docker-compose.yml names the django service.
const target = process.env.VITE_API_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // Proxy in development so the frontend is same-origin with the backend;
    // that way the session cookie and CSRF need no special handling.
    //
    // The browser's own Host header is forwarded as it arrived. Django answers only to the
    // hosts it is configured for, and the name the reader typed is one of them, while the
    // service name the proxy connects to under Compose is a detail of that network.
    proxy: {
      '/api': { target, changeOrigin: false },
      '/admin': { target, changeOrigin: false },
      // The admin's own stylesheets and scripts, which its pages request from /static.
      '/static': { target, changeOrigin: false },
    },
  },
})
