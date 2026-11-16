/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, 'src') } },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8001',
      '/health': 'http://127.0.0.1:8001',
    },
  },
  test: { environment: 'jsdom', globals: true, setupFiles: './src/setupTests.ts', exclude: ['e2e/**', 'node_modules/**'] },
})
