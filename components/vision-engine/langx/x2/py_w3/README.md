# LANG-X X2 Browser Seed (`py_w3`)

Single-file Python standard library command-line tool (`x2.py`) for turning web pages into structured JSON states for computer-use agents.

## Usage

```bash
export CHROME_BIN=/usr/bin/google-chrome
python3 langx/x2/py_w3/x2.py --page PATH --steps PATH --timing PATH [--timeout-s N]
```

### Options

- `--page PATH`: Path to a local HTML file.
- `--steps PATH`: Path to a steps JSON file.
- `--timing PATH`: Output path for timing JSON file.
- `--timeout-s N`: Overall timeout in seconds (default: 30).

## Environment Variables

- `CHROME_BIN`: Absolute path to Chrome or Chromium binary (required for execution).
- `X2_WHERE`: Location or platform label written into the timing JSON (defaults to `unlabelled`).

## Exit Codes

- `0`: Done successfully (including steps marked as failed due to timeout).
- `2`: Bad arguments or invalid steps file.
- `3`: `CHROME_BIN` unset or browser launch failed.
- `4`: Navigation or target attachment failed.
- `5`: Overall timeout.
- `6`: Torn tail detected at pipe EOF.
- `7`: CDP protocol error or message validation failure.

## Testing

Run unit and browser test suite:

```bash
python3 -m unittest discover -s langx/x2/py_w3 -p test_x2.py
```
