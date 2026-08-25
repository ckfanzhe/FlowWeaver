import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// `preview` config matches the runtime stage of `frontend/Dockerfile`:
// `npx vite preview --host 0.0.0.0 --port 4173 --strictPort`. Local
// `npm run preview` picks up the same block. `allowedHosts: true`
// accepts any Host header (containers / reverse proxies may route to
// the preview server with an arbitrary Host that's not on Vite's
// default allowlist).
//
// `build.rollupOptions.output.manualChunks` splits the heavy
// dependency trees into separately-cacheable chunks so a CSS /
// TypeScript-only edit doesn't invalidate the entire vendor
// bundle. `@xyflow/react` + `@assistant-ui/react` together account
// for ~600 kB gzip; isolating them keeps the main app chunk under
// 100 kB and turns most rebuilds into <100 ms HMR updates.
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          xyflow: ['@xyflow/react'],
          'assistant-ui': ['@assistant-ui/react'],
          // React + react-dom — these rarely change, isolating them
          // lets a vendor-version bump invalidate only this chunk.
          react: ['react', 'react-dom'],
        },
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
    allowedHosts: true,
  },
})
