# LANG-X X2 seed ts_w3

Single-file TypeScript browser-interaction CLI tool for turning a web page into a JSON state sequence for a computer-use agent.

## Usage

Started using Node native type stripping (Node 22+ with `--experimental-strip-types` or Node 24+ directly):

```bash
node --experimental-strip-types langx/x2/ts_w3/x2.ts \
  --page /path/to/page.html \
  --steps /path/to/steps.json \
  --timing /path/to/timing.json \
  [--timeout-s 30]
```

Requires environment variable `CHROME_BIN` pointing to a Chrome or Chromium executable.

## Exit Codes

- `0`: Done successfully (including steps that may have failed with a timeout).
- `2`: Bad arguments or missing required parameters.
- `3`: `CHROME_BIN` unset or browser launch failed.
- `4`: Navigation or page load failed.
- `5`: Overall process timeout reached (`--timeout-s`).
- `6`: Torn tail (NUL-delimited protocol pipe reached EOF with non-empty partial buffer).
- `7`: Protocol error or invalid message response.

## Running Tests

```bash
CHROME_BIN=/usr/bin/google-chrome node --experimental-strip-types --test langx/x2/ts_w3/x2.test.ts
```

If `CHROME_BIN` is unset, pure unit tests will run and browser integration tests will be skipped with `SKIP CHROME_BIN unset`.
