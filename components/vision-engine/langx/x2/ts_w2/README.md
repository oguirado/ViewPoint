# LANG-X X2 TypeScript Browser Seed (`ts_w2`)

`langx/x2/ts_w2/x2.ts` is a single-file command-line tool implemented in TypeScript for Node.js 24 using native type stripping (erasable TypeScript syntax). It turns a local HTML web page into canonical JSON state representations for a computer-use agent using Chrome DevTools Protocol (CDP) over pipe stdio fds 3 and 4.

## Usage

```bash
node langx/x2/ts_w2/x2.ts --page <PATH_TO_HTML> --steps <PATH_TO_STEPS_JSON> --timing <PATH_TO_TIMING_JSON> [--timeout-s <SECONDS>]
```

### Environment Variables

- `CHROME_BIN`: Absolute path to Chrome or Chromium executable binary (e.g. `/usr/bin/google-chrome`).
- `X2_WHERE`: Environment label recorded in timing output (default: `unlabelled`).

## Testing

Run unit and integration tests using Node's native test runner:

```bash
CHROME_BIN=/usr/bin/google-chrome node --test langx/x2/ts_w2/x2.test.ts
```

If `CHROME_BIN` is not set, browser integration tests will be skipped with `SKIP CHROME_BIN unset`.

## Exit Codes

- `0`: Completed successfully (including scenarios with failed steps recorded in states).
- `2`: Bad arguments or invalid steps/page file path.
- `3`: `CHROME_BIN` environment variable unset or browser launch failed.
- `4`: Navigation or page load event failed.
- `5`: Overall process timeout reached.
- `6`: Torn tail (pipe reached EOF with non-empty partial buffer).
- `7`: Protocol error or malformed JSON message from CDP.
