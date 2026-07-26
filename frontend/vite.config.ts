import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import monacoEditorPluginModule from 'vite-plugin-monaco-editor'
import { resolve } from 'path'

// Handle CJS/ESM default export interop
const monacoEditorPlugin = (monacoEditorPluginModule as any).default || monacoEditorPluginModule

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    monacoEditorPlugin({
      languageWorkers: ['editorWorkerService'],
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      }
    }
  }
})
