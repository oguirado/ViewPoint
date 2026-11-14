import { defineConfig } from 'vitest/config';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// Wails serves the built `dist/` as its asset root; relative URLs keep the page valid from any mount point.
export default defineConfig({
  base: './',
  plugins: [svelte({ preprocess: vitePreprocess() })],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
  },
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    // tests/geometria.test.ts builds the app, drives headless Chromium and compiles the Go bridge: seconds when warm,
    // minutes on a cold cache. A timeout is never the reason a test fails.
    testTimeout: 180_000,
    hookTimeout: 300_000,
  },
});
