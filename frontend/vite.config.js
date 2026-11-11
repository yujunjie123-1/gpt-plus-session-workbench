import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  build: {
    outDir: fileURLToPath(new URL('../web/dist', import.meta.url)),
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      onwarn(warning, warn) {
        if (warning.code === 'MODULE_LEVEL_DIRECTIVE' && warning.message.includes('use client')) return;
        warn(warning);
      },
      output: {manualChunks: {fluent: ['@fluentui/react-components']}}
    }
  }
});
