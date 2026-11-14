# LANG-X X2 browser seed (Python - py_w1)

`langx/x2/py_w1/x2.py` is a single-file Python command-line tool using only the Python standard library to turn web pages into JSON states for agent interaction via Chrome DevTools Protocol (CDP) over operating system pipes.

## Usage

```bash
CHROME_BIN=/usr/bin/google-chrome python3 langx/x2/py_w1/x2.py \
  --page /path/to/page.html \
  --steps /path/to/steps.json \
  --timing /path/to/timing.json \
  [--timeout-s 30]
```

## CLI Flags

- `--page PATH`: Path to local HTML file.
- `--steps PATH`: Path to steps JSON file (`{"steps": [{"op": "click"|"type"|"wait", "selector": "...", "text": "...", "timeout_ms": 2000}]}`).
- `--timing PATH`: Output path for execution timing statistics JSON file.
- `--timeout-s N`: Optional overall timeout in seconds (default: 30).

## Architecture & IPC Protocol

1. **Process Isolation (Rule R1):** Spawns Chrome as a child process using `subprocess.Popen` with flags `--headless=new --remote-debugging-pipe --no-first-run --no-default-browser-check --disable-gpu --disable-extensions --hide-scrollbars --force-device-scale-factor=1 --window-size=1280,800 --user-data-dir=<temp_dir>`.
2. **Pipes & Framing (Rule R2):** Two `os.pipe()` pairs are mapped to file descriptors 3 and 4 in the child process. Every CDP protocol message in both directions is a UTF-8 JSON object terminated by a single `NUL` (`\x00`) byte. No TCP ports are opened.
3. **CDP Workflow:** Attaches to target page (`Target.attachToTarget` with `flatten=True`), enables page events (`Page.enable`), navigates to `file://` URL, waits for `Page.loadEventFired`.
4. **Settle & Capture:** Evaluates 2 consecutive `requestAnimationFrame` callbacks (`Runtime.evaluate`) prior to taking snapshots with `DOMSnapshot.captureSnapshot` and `Accessibility.getFullAXTree`.
5. **Bounded Backoff (Rule R8):** Resolves step target selectors with exponential backoff sequence (1, 2, 4, 8, 16, 32, 64, 64... ms) up to step `timeout_ms`.
6. **Closed Integer Boxes (Rule R10):** All element boxes are reported as closed integer sets `[x0, y0, x1, y1]`.
7. **Canonical Serialization:** Standardized json serialization with sorted keys, compact separators, escaped non-ASCII characters, and unescaped slashes.

## Exit Codes

- `0`: Success (including failed individual steps).
- `2`: Invalid CLI arguments or missing required files.
- `3`: `CHROME_BIN` environment variable unset or browser launch failed.
- `4`: Page navigation or load failed.
- `5`: Overall timeout exceeded.
- `6`: Torn tail detected at pipe EOF.
- `7`: Protocol error or invalid CDP message format.

## Running Tests

To run unit and integration tests:

```bash
# Pure unit tests (no browser needed)
python3 -m unittest discover -s langx/x2/py_w1 -p test_x2.py

# Full unit and browser integration tests
CHROME_BIN=/usr/bin/google-chrome python3 -m unittest discover -s langx/x2/py_w1 -p test_x2.py
```
