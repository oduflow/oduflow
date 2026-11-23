/// <reference types="vitest/config" />
// Build config for the shared-environment workspace (/env/<name>).
//
// The bundle is committed to the Python package so that installing and running
// Oduflow never needs Node: `npm run build` writes it straight into
// src/oduflow/templates/static/workspace/, where web_ui serves index.html as the
// page template and the hashed assets under /static/workspace/. CI rebuilds and
// fails on any diff (see .github/workflows/frontend.yml).
//
// `npm run dev` serves the same app with hot reload and proxies every server
// route to a running Oduflow (ODUFLOW_URL, default http://127.0.0.1:8000). Open
// http://localhost:5173/?env=<environment> after signing in through /login.
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

const repoRoot = fileURLToPath(new URL('../..', import.meta.url));
const outDir = fileURLToPath(
  new URL('../../src/oduflow/templates/static/workspace', import.meta.url)
);
const oduflow = process.env.ODUFLOW_URL || 'http://127.0.0.1:8000';

export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/static/workspace/' : '/',
  // Fonts, icons and the logo are served by Oduflow itself (/static, /logo.png).
  publicDir: false,
  plugins: [svelte()],
  build: {
    outDir,
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
  },
  server: {
    // acp-client.js is shared with the dashboard and lives in the package.
    fs: { allow: [repoRoot] },
    proxy: {
      '/api': { target: oduflow, ws: true },
      '/login': oduflow,
      '/logout': oduflow,
      '/static': oduflow,
      '/logo.png': oduflow,
      '/favicon.ico': oduflow,
    },
  },
  test: {
    include: ['tests/**/*.test.ts'],
  },
}));
