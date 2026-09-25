import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/debates': 'http://localhost:8000',
      '/admin': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
      '/metrics': 'http://localhost:8000',
    }
  }
})
