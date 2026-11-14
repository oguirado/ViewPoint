import { defineConfig } from '@playwright/test';
import { DOM_H_SEXTO, DOM_W } from './src/lib/geometria';

// Browser tests of the cockpit run in Playwright 1.58.0 Chromium headless (DEC-18): a measurement proxy for the
// WebKitGTK window. The viewport is the DOM viewport of the 1/6 tile and comes from src/lib/geometria.ts, the single
// source of the figures (spec 1.2: the window manager takes 9 px of the window width); it is created before
// navigation with a device scale factor of 1, exactly as scripts/ng/medir-cls.mjs does.
// No spec consumes this configuration yet: a `*.e2e.ts` file under tests/ would, and vitest never picks those up.
export default defineConfig({
  testDir: 'tests',
  testMatch: '**/*.e2e.ts',
  fullyParallel: false,
  workers: 1,
  reporter: 'line',
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: DOM_W, height: DOM_H_SEXTO },
    deviceScaleFactor: 1,
    screenshot: 'off',
    video: 'off',
    trace: 'off',
  },
});
