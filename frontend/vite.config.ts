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
export default defineConfig({
  plugins: [react()],
  preview: {
    host: '0.0.0.0',
    port: 4173,
    allowedHosts: true,
  },
})
