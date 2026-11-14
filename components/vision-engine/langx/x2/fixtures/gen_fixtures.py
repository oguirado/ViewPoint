#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys

def generate_fixtures(out_dir: str):
    abs_out_dir = os.path.abspath(out_dir)
    os.makedirs(abs_out_dir, exist_ok=True)

    # 1. p1_form.html & p1_form.steps.json
    p1_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>p1_form</title>
<style>
body { margin: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
</style>
</head>
<body>
<form id="form">
  <label for="q">Query:</label>
  <input type="text" id="q" name="q">
  <br>
  <label for="agree">
    <input type="checkbox" id="agree" name="agree"> Agree
  </label>
  <br>
  <label for="color">Color:</label>
  <select id="color" name="color">
    <option value="red">Red</option>
    <option value="green">Green</option>
    <option value="blue">Blue</option>
  </select>
  <br>
  <button id="go" type="button">Go</button>
</form>
<div id="out"></div>
<script>
document.getElementById('go').addEventListener('click', function() {
  var qVal = document.getElementById('q').value;
  document.getElementById('out').textContent = qVal;
});
</script>
</body>
</html>
"""

    p1_steps = {
        "steps": [
            {"op": "type", "selector": "#q", "text": "hello"},
            {"op": "click", "selector": "#agree"},
            {"op": "click", "selector": "#go"}
        ]
    }

    # 2. p2_list.html & p2_list.steps.json
    p2_rows_initial = []
    for i in range(60):
        p2_rows_initial.append(f'  <div class="row-{i}"><a class="row-{i}" href="#row-{i}">Row {i}</a></div>')
    initial_rows_html = "\n".join(p2_rows_initial)

    p2_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>p2_list</title>
<style>
body {{ margin: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
#list {{ height: 200px; overflow: auto; border: 1px solid #ccc; }}
</style>
</head>
<body>
<div id="list">
{initial_rows_html}
</div>
<button id="more" type="button">More</button>
<template class="row-64"><a class="row-64" href="#row-64">Row 64</a></template>
<script>
var currentCount = 60;
document.getElementById('more').addEventListener('click', function() {{
  var list = document.getElementById('list');
  for (var i = 0; i < 5; i++) {{
    var idx = currentCount + i;
    var div = document.createElement('div');
    div.className = 'row-' + idx;
    var a = document.createElement('a');
    a.className = 'row-' + idx;
    a.href = '#row-' + idx;
    a.textContent = 'Row ' + idx;
    div.appendChild(a);
    list.appendChild(div);
  }}
  currentCount += 5;
}});
</script>
</body>
</html>
"""

    p2_steps = {
        "steps": [
            {"op": "click", "selector": "#more"},
            {"op": "wait", "selector": ".row-64"}
        ]
    }

    # 3. p3_dialog.html & p3_dialog.steps.json
    p3_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>p3_dialog</title>
<style>
body { margin: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
</style>
</head>
<body>
<button id="open" type="button">Open Dialog</button>
<div id="dialog" role="dialog" aria-modal="true" style="display: none;">
  <h2>Dialog Heading</h2>
  <button id="close" type="button">Close</button>
</div>
<script>
document.getElementById('open').addEventListener('click', function() {
  document.getElementById('dialog').style.display = 'block';
});
document.getElementById('close').addEventListener('click', function() {
  document.getElementById('dialog').style.display = 'none';
});
</script>
</body>
</html>
"""

    p3_steps = {
        "steps": [
            {"op": "click", "selector": "#open"},
            {"op": "click", "selector": "#close"}
        ]
    }

    # 4. p4_late.html & p4_late.steps.json
    p4_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>p4_late</title>
<style>
body { margin: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
</style>
</head>
<body>
<button id="enable" disabled type="button">Enable</button>
<div id="container"></div>
<template id="late-template"><div id="late">Late inserted content</div></template>
<script>
setTimeout(function() {
  var tpl = document.getElementById('late-template');
  document.getElementById('container').appendChild(tpl.content.cloneNode(true));
}, 1500);
setTimeout(function() {
  document.getElementById('enable').removeAttribute('disabled');
}, 1800);
</script>
</body>
</html>
"""

    p4_steps = {
        "steps": [
            {"op": "wait", "selector": "#late", "timeout_ms": 4000},
            {"op": "click", "selector": "#enable"}
        ]
    }

    # 5. p5_text.html & p5_text.steps.json
    p5_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>p5_text</title>
<style>
body { margin: 8px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.hidden-elem { visibility: hidden; }
.zero-elem { width: 0; height: 0; overflow: hidden; }
</style>
</head>
<body>
<p id="es-label" class="es-label">Spanish: ¿Cómo estás?</p>
<p id="ja-label" class="ja-label">Japanese: こんにちは</p>
<p id="emoji-label" class="emoji-label">Emoji: 🚀</p>
<button id="aria-btn" class="aria-btn" aria-label="Action Button" type="button">Visible Label</button>
<div id="hidden-elem" class="hidden-elem">Hidden text</div>
<div id="zero-elem" class="zero-elem">Zero size text</div>
<div id="whitespace-elem" class="whitespace-elem">   Multiple    spaces   between    words   </div>
</body>
</html>
"""

    p5_steps = {
        "steps": [
            {"op": "click", "selector": "#aria-btn"}
        ]
    }

    files = {
        "p1_form.html": p1_html.encode("utf-8"),
        "p1_form.steps.json": json.dumps(p1_steps, indent=2).encode("utf-8") + b"\n",
        "p2_list.html": p2_html.encode("utf-8"),
        "p2_list.steps.json": json.dumps(p2_steps, indent=2).encode("utf-8") + b"\n",
        "p3_dialog.html": p3_html.encode("utf-8"),
        "p3_dialog.steps.json": json.dumps(p3_steps, indent=2).encode("utf-8") + b"\n",
        "p4_late.html": p4_html.encode("utf-8"),
        "p4_late.steps.json": json.dumps(p4_steps, indent=2).encode("utf-8") + b"\n",
        "p5_text.html": p5_html.encode("utf-8"),
        "p5_text.steps.json": json.dumps(p5_steps, indent=2).encode("utf-8") + b"\n"
    }

    manifest_lines = []
    for filename in sorted(files.keys()):
        content = files[filename]
        filepath = os.path.abspath(os.path.join(abs_out_dir, filename))
        if not (filepath == abs_out_dir or filepath.startswith(abs_out_dir + os.sep)):
            raise RuntimeError(f"Attempted to write outside target directory: {filepath}")
        with open(filepath, "wb") as f:
            f.write(content)
        sha = hashlib.sha256(content).hexdigest()
        manifest_lines.append(f"{sha}  {filename}")

    manifest_output = "\n".join(manifest_lines)
    return manifest_lines

def main():
    parser = argparse.ArgumentParser(description="Generate LANG-X X2 shared HTML and steps fixtures.")
    parser.add_argument("--out", required=True, help="Output directory where fixtures will be written.")
    args = parser.parse_args()
    lines = generate_fixtures(args.out)
    print("\n".join(lines))

if __name__ == "__main__":
    main()
