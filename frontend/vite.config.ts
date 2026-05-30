import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// VitePWA intentionally NOT used. This is a Tauri desktop app — assets are
// already bundled locally and installed via the .exe, so a service-worker
// precache adds zero value. Worse, it caused a real upgrade-pain bug: when
// users updated to a new .exe with a new catalog, the SW from the previous
// install (still alive in WebView2 storage) kept intercepting requests and
// serving the OLD bundled chunks — connector setup instructions appeared
// blank because the cached catalog predated those entries. Removing the
// plugin avoids that whole class of cache-vs-binary skew on every update.

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [
    react(),
    tailwindcss(),
  ],
  build: {
    outDir: '../src/openjarvis/server/static',
    emptyOutDir: true,
    minify: 'esbuild',
    rollupOptions: {
      output: {
        // Granular chunks so the initial download stays small and heavy
        // libraries (katex, rehype-highlight, recharts) only load when a
        // page that needs them is visited. See App.tsx — pages are
        // React.lazy()'d so each route gets its own chunk on top of these.
        manualChunks: {
          'react-vendor':       ['react', 'react-dom', 'react-router'],
          'markdown':           ['react-markdown', 'remark-gfm'],
          'markdown-math':      ['katex', 'remark-math', 'rehype-katex'],
          'markdown-highlight': ['rehype-highlight'],
          'charts':             ['recharts'],
          'motion':             ['motion'],
          'analytics':          ['posthog-js'],
          'ui-base':            ['@base-ui/react', 'lucide-react'],
          'tauri':              [
            '@tauri-apps/api',
            '@tauri-apps/plugin-autostart',
            '@tauri-apps/plugin-dialog',
            '@tauri-apps/plugin-global-shortcut',
            '@tauri-apps/plugin-notification',
            '@tauri-apps/plugin-process',
            '@tauri-apps/plugin-shell',
            '@tauri-apps/plugin-updater',
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': process.env.VITE_API_URL || 'http://localhost:8000',
      '/health': process.env.VITE_API_URL || 'http://localhost:8000',
      '/api': process.env.VITE_API_URL || 'http://localhost:8000',
    },
  },
});
