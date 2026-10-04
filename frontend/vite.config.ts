import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In the demo, FastAPI serves the built app from frontend/dist. During `npm run dev`,
// API calls (including the SSE stream) are proxied to the backend on port 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    // Reporters may be on old phones, so emit older syntax than Vite's modern default.
    target: ['es2019', 'chrome70', 'safari13', 'firefox68'],
  },
})
