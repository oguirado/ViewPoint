# LANG-X X2 browser seed (Python seed `py_w2`)

Command-line browser interaction tool for computer-use agents, converting web pages into structured JSON states over Chrome DevTools Protocol (CDP) pipe IPC.

## Usage

```bash
CHROME_BIN=/usr/bin/google-chrome python3 langx/x2/py_w2/x2.py \
  --page /path/to/file.html \
  --steps /path/to/steps.json \
  --timing /path/to/timing.json \
  [--timeout-s 30]
```

### CLI Arguments
- `--page PATH`: Path to local HTML file.
- `--steps PATH`: Path to steps JSON file.
- `--timing PATH`: Path to write performance timing metrics JSON.
- `--timeout-s N`: Optional overall timeout in seconds (default: 30).

## Exit Codes

- `0`: Done successfully (including handled per-step timeouts).
- `2`: Bad arguments or invalid file paths/JSON structure.
- `3`: `CHROME_BIN` environment variable unset or browser launch failed.
- `4`: Navigation or page load failed.
- `5`: Overall timeout exceeded.
- `6`: Torn tail (EOF reached on pipe with non-empty partial buffer).
- `7`: CDP protocol error or invalid framing message.

## Host Harness Invocation

The host harness invokes `x2.py` via `python3 langx/x2/py_w2/x2.py` with `--page`, `--steps`, and `--timing`. Standard output contains canonical JSON conforming to `vqa.x2-state/v1`.

## Running Tests

Run the test suite using standard Python `unittest`:

```bash
CHROME_BIN=/usr/bin/google-chrome python3 -m unittest discover -s langx/x2/py_w2 -p test_x2.py
```
