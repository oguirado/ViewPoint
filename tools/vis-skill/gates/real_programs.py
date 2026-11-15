#!/usr/bin/env python3
"""Real surfaces; frames never leave job RAM; text evidence survives in the report."""
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import *

def main():
    skip_started_jobs()
    work = Path(tempfile.mkdtemp(prefix='visb-real-', dir='/dev/shm/vqa-cu'))
    jobs = []
    held = []
    try:
        html = work / 'page.html'
        html.write_text('<!doctype html><html><body style="background:white"><button style="margin:80px;width:220px;height:100px;font-size:28px" onclick="this.textContent=\'Done\';document.body.style.background=\'#90ee90\'">Start job</button></body></html>')
        electron = work / 'electron'
        electron.mkdir()
        (electron / 'package.json').write_text('{"name":"vis-b-test","version":"1.0.0","main":"main.js"}')
        (electron / 'main.js').write_text("const {app,BrowserWindow}=require('electron');app.whenReady().then(()=>{let w=new BrowserWindow({width:800,height:600,webPreferences:{nodeIntegration:true,contextIsolation:false}});w.loadFile('" + str(electron / 'page.html') + "')});app.on('window-all-closed',()=>app.quit());")
        compat = compatibility_lock(work / 'compat')
        for profile, selection in [('current', []), ('compatibility', ['--target-repo', str(compat)])]:
            prefix = 'visb-real-' + profile + '-' + str(os.getpid())
            vis('doctor', *selection)
            name = prefix + '-gedit'
            jobs.append(name)
            saved = work / (profile + '-editor.txt')
            saved.touch()
            vis('start', '--job', name, '--app', shlex.join(['gedit', '--standalone', str(saved)]), '--size', '800x600', *selection, '--wait-title', 'gedit|editor.txt')
            text = 'VIS-B ' + profile + ' editor proof'
            vis('look', '--job', name)
            vis('act', '--job', name, '--step', 'write', '--type', text, '--expect-change', '0,50,800,500')
            # Compare painted text at the start of the line, excluding the
            # blinking insertion caret at its end. Exact saved content is
            # independently checked below; no mask hides document text.
            vis('check', '--job', name, '--region-changed-since', 'write', '32,54,100,24')
            vis('act', '--job', name, '--step', 'save', '--key', 'ctrl+s', '--expect-change', 'any')
            require(saved.read_text().strip() == text, 'gate assertion failed')
            note('gedit ' + profile + ': saved text equals ' + repr(text) + ' [MEASURED]; changed editing region and persisted RAM file')
            vis('stop', '--job', name)
            name = prefix + '-mines'
            jobs.append(name)
            vis('start', '--job', name, '--app', '/usr/games/gnome-mines', '--size', '900x700', *selection, '--wait-title', 'Mines')
            d = vis('look', '--job', name)
            cards = [m for m in d['marks'] if m['w'] > 150 and m['h'] > 150]
            require(len(cards) == 4, 'gate assertion failed')
            card = min(cards, key=lambda m: (m['y'], m['x']))
            d = vis('act', '--job', name, '--step', 'board', '--click', str(card['id']), '--expect-change', '150,170,600,430')
            require(len([m for m in d['marks'] if 40 <= m['w'] <= 60 and 40 <= m['h'] <= 60]) >= 16, 'gate assertion failed')
            note('gnome-mines ' + profile + ': upper-left size card mark ' + str(card['id']) + ' [MEASURED] replaced by cell grid; retained board frame for visual inspection')
            held.append(name)
            d = vis('look', '--job', name)
            cell = next((m for m in d['marks'] if 40 <= m['w'] <= 60 and 40 <= m['h'] <= 60))
            vis('act', '--job', name, '--step', 'reveal', '--click', str(cell['id']), '--expect-change', '150,170,600,430')
            status = work / (profile + '-electron-status.txt')
            (electron / 'page.html').write_text('<title>VIS-B Electron</title><body style="background:white"><button style="margin:100px;width:220px;height:100px;font-size:28px" onclick="this.innerText=\'Done\';document.body.style.background=\'lightgreen\';require(\'fs\').writeFileSync(' + repr(str(status)) + ',\'clicked\')">Start job</button></body>')
            name = prefix + '-electron'
            jobs.append(name)
            vis('start', '--job', name, '--electron', str(electron), '--size', '900x700', *selection, '--wait-title', 'VIS-B Electron', env={'VIS_ELECTRON_BIN': os.environ['VIS_ELECTRON_BIN']})
            d = vis('look', '--job', name)
            mark = next((m for m in d['marks'] if 200 <= m['w'] <= 240 and 90 <= m['h'] <= 110))
            vis('act', '--job', name, '--step', 'button', '--click', str(mark['id']), '--expect-change', 'any')
            require(status.read_text() == 'clicked', 'gate assertion failed')
            note('Electron ' + profile + ': numbered mark click produced renderer test-hook clicked and visible change [MEASURED]')
            vis('stop', '--job', name)
            name = prefix + '-web'
            jobs.append(name)
            vis('start', '--job', name, '--web', html.as_uri(), '--size', '800x600', *selection)
            d = vis('look', '--job', name)
            node = next((n for n in d['state']['nodes'] if n['role'] == 'button' and n['name'] == 'Start job'))
            vis('act', '--job', name, '--step', 'button', '--ax', node['id'], '--op', 'click', '--expect-change', 'any')
            vis('check', '--job', name, '--state', 'button=Done')
            vis('stop', '--job', name)

        def parallel(i):
            name = 'visb-parallel-' + str(os.getpid()) + '-' + str(i)
            jobs.append(name)
            saved = work / ('parallel-' + str(i) + '.txt')
            saved.touch()
            vis('start', '--job', name, '--app', shlex.join(['gedit', '--standalone', str(saved)]), '--size', '800x600', '--wait-title', 'parallel-')
            display = state(name)['display']
            text = 'Parallel editor ' + str(i)
            vis('act', '--job', name, '--step', 'write', '--type', text, '--expect-change', '0,50,800,500')
            vis('act', '--job', name, '--step', 'save', '--key', 'ctrl+s', '--expect-change', 'any')
            require(saved.read_text().strip() == text, 'gate assertion failed')
            vis('stop', '--job', name)
            return display
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            displays = list(pool.map(parallel, range(3)))
        require(len(set(displays)) == 3, 'gate assertion failed')
        note('Parallel gedit: 3 [MEASURED] distinct private displays ' + repr(displays) + '; every RAM file held only its own expected text')
        if '--hold-mines' in sys.argv:
            (ROOT / 'docs/.visb-held.json').write_text(json.dumps(held))
            jobs[:] = [j for j in jobs if j not in held]
        note('Real-program suite: PASS on current and compatibility pins [MEASURED]')
    finally:
        for job in reversed(jobs):
            cleanup(job)
        shutil.rmtree(work, ignore_errors=True)
if __name__ == '__main__':
    main()
