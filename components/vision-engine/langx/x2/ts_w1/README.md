# LANG-X X2 TypeScript seed (`ts_w1`)

`x2.ts` is a single-file TypeScript command-line browser interaction tool that executes in Node.js 24+ without compilation, `tsc`, or third-party npm dependencies using native Node type stripping.

## Usage

```bash
CHROME_BIN=/usr/bin/google-chrome node langx/x2/ts_w1/x2.ts \
  --page /path/to/page.html \
  --steps /path/to/steps.json \
  --timing /path/to/timing.json \
  [--timeout-s 30]
```

### CLI Arguments
- `--page PATH` (Required): Absolute or relative path to local HTML file.
- `--steps PATH` (Required): Path to steps JSON file (`{"steps": [{"op": "click"|"type"|"wait", "selector": "...", "text": "...", "timeout_ms": 2000}]}`).
- `--timing PATH` (Required): Output path for the benchmark timing report.
- `--timeout-s N` (Optional): Overall execution timeout in seconds (default `30`).

## Environment Variables
- `CHROME_BIN`: Absolute path to Chromium or Chrome executable binary.
- `X2_WHERE`: Location tag stored in the output timing JSON file (defaults to `unlabelled`).

## Exit Codes
- `0`: Success (all steps completed or marked failed with timeout).
- `2`: Invalid CLI arguments or unreadable input files.
- `3`: `CHROME_BIN` environment variable missing or browser child process launch failed.
- `4`: Page navigation or initial `Page.loadEventFired` load failed.
- `5`: Execution exceeded overall `--timeout-s` duration.
- `6`: CDP pipe reached EOF with a torn/incomplete message buffer.
- `7`: Protocol framing error, invalid CDP message structure, or unexpected response ID.

## Running Tests

Run unit and integration tests using native `node:test`:

```bash
# With browser tests enabled:
CHROME_BIN=/usr/bin/google-chrome node --test langx/x2/ts_w1/x2.test.ts

# Pure unit tests only (skips browser test):
node --test langx/x2/ts_w1/x2.test.ts
```
