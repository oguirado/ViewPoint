"""Bounded text-only gate evidence; never persists frames."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
REPORT=Path(os.environ.get('VIS_GATE_REPORT',str(ROOT/'docs/GATE_RESULTS_W.md')))
VIS=ROOT/'vis'
BASE=Path('/dev/shm/vqa-cu/vis')
CODES={0:'OK',2:'E_USAGE',3:'E_EXPECT',4:'E_RETRIES',5:'E_RULE',6:'E_BUDGET',7:'E_ABSENT',8:'E_TARGET',9:'E_INTERNAL'}

def require(value,message='gate assertion failed'):
    if not value: raise AssertionError(message)

def compatibility_lock(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    (root/'deps.lock').write_text(json.dumps({'dependencies':{'wender-agent':{'commit':'7648b258d1d04953bacffe8627de5acd7c75d817','version':'ignored'},'vision-engine':{'commit':'9bbb49f220837388e1ab90c34ae0eedee27a66eb'}}}))
    return root

def log(text):
    with open(REPORT,'a') as f:
        fcntl.flock(f,fcntl.LOCK_EX); f.write(text+'\n'); f.flush()

def execute(cmd,expected=0,env=None,timeout=130,brief=True):
    e=os.environ.copy()
    if env: e.update(env)
    r=subprocess.run(list(map(str,cmd)),env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    out=r.stdout.decode(errors='replace'); err=r.stderr.decode(errors='replace')
    try:
        d=json.loads(out)
    except ValueError: d=None
    evidence=out
    if brief and isinstance(d,dict):
        keep=('ok','rc','code','verb','reason','job','display','scope','capabilities','drift','matched','receipts','frame_sha256','changed','changed_regions','receipt')
        evidence=json.dumps({k:d[k] for k in keep if k in d},sort_keys=True)
        if 'marks' in d: evidence+='\nmarks_count='+str(len(d['marks']))
        if 'state' in d: evidence+='\nSTATE='+json.dumps(d['state'],sort_keys=True)
    log('\nCommand: `'+shlex.join(list(map(str,cmd)))+'`\n\nrc: **'+str(r.returncode)+' [MEASURED]**\n\n```text\n'+evidence[-18000:]+('\nstderr: '+err[-2000:] if err else '')+'\n```')
    if expected is not None: require(r.returncode==expected,(cmd,r.returncode,expected,out,err))
    if cmd and Path(str(cmd[0])).name=='vis':
        require(len(out.splitlines())==1,(cmd,out))
        require(isinstance(d,dict) and d['rc']==r.returncode and d['ok']==(r.returncode==0),(cmd,out))
        require(d.get('code')==CODES.get(r.returncode),(cmd,out))
        require(out==json.dumps(d,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n','noncanonical stdout')
        require(d.get('verb')==(str(cmd[1]) if len(cmd)>1 else ''),'wrong verb')
    return d if isinstance(d,dict) else r

PIDNS=os.environ.get('NEWTON_TEST_PIDNS')=='1'
SKIP=77
def skip_started_jobs():
    """Inside safe_run (NEWTON_TEST_PIDNS=1) vis start runs its supervisor as a direct
    child (test mode, no systemd scope; safe_run caps memory), so started-job gates run.
    Only a PIDNS run in the initial PID namespace, where vis has no test mode, skips (rc 77)."""
    if PIDNS and os.readlink('/proc/self/ns/pid')=='pid:[4026531836]':
        note('SKIPPED [MEASURED]: '+Path(sys.argv[0]).name+' needs vis start; NEWTON_TEST_PIDNS=1 outside a private PID namespace')
        sys.exit(SKIP)
    if PIDNS: fixture_chrome()

def fixture_chrome():
    """Test mode only. The test runs as uid 0 of safe_run's user namespace, where Chrome
    refuses its sandbox. These gates load only their own local fixture pages, so they
    point CHROME_BIN at a shim adding --no-sandbox. vis itself is unchanged."""
    import atexit,shutil,tempfile
    real=os.environ.get('CHROME_BIN','/usr/bin/google-chrome')
    shim=Path(tempfile.mkdtemp(dir=ROOT/'gates',prefix='.chrome-'))  # RAM is noexec here
    (shim/'chrome').write_text('#!/bin/sh\nexec '+shlex.quote(real)+' --no-sandbox "$@"\n'); (shim/'chrome').chmod(0o700)
    atexit.register(shutil.rmtree,shim,True)
    os.environ['CHROME_BIN']=str(shim/'chrome')
    note('TEST MODE [MEASURED]: web fixtures use Chrome with --no-sandbox (uid 0 inside the safe_run user namespace)')

def not_in_test_mode(what):
    """A check of the systemd scope itself (per-job MemoryMax, --mem OOM, scope units) has no
    test-mode counterpart: say so in the evidence instead of passing silently."""
    if PIDNS: note('NOT COVERED IN TEST MODE [MEASURED]: '+what+' (no systemd scope under safe_run; host run needed)')
    return PIDNS

# Ordinary typed strings that the former credential pattern refused or nearly refused.
ORDINARY_TEXTS=(
    'task-list','Add a new task-board','desk-top layout','risk-free trial','disk-usage 80%',
    'Ask-file not set','mask-region','kiosk-mode','asterisk-marked field','Click the flask-icon',
    'https://example.com/task-123','sk-learn','MetaViewer desk-01','holdings-e1/sk-test','ask-sol',
    'Ver risk-analysis.md','Disk-I/O panel','task-management-dashboard-v2-final',
    'multitask-scheduler-integration-tests','desk-booking-application-frontend',
    'the risk-adjusted-return-calculation-module','gho_ is a GitHub prefix','AIza appears in docs',
    'xoxb- tokens are Slack bot tokens','-----BEGIN PUBLIC KEY-----','Hola mundo','Example')
# Filler placeholders behind real provider prefixes and hyphenated words behind sk-: typed, not refused.
PLACEHOLDER_TEXTS=('AIza'+'0'*35,'sk-ant-api03-'+'A'*24,'ghp_'+'A1'*18,'ghp_'+'x'*36,'github_pat_'+'0'*59+'A',
    'sk-proj-'+'X'*30,'sk-'+'learn-preprocessing-pipeline-v2','xoxb-'+'0'*12)

def synthetic_keys():
    """Fresh random key-shaped strings; never a real credential."""
    import random,string
    rng=random.SystemRandom(); alnum=string.ascii_letters+string.digits
    def rs(n,alpha=alnum):
        while True:
            s=''.join(rng.choice(alpha) for _ in range(n))
            if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s): return s
    return ['sk-'+rs(48),'sk-proj-'+rs(64),'sk-ant-api03-'+rs(80),'ghp_'+rs(36),'gho_'+rs(36),
            'github_pat_'+rs(82,alnum+'_'),'AIza'+rs(35),'xoxb-'+''.join(rng.choice(string.digits) for _ in range(12))+'-'+rs(24),
            '-----BEGIN OPENSSH '+'PRIVATE KEY-----','my key is sk-'+rs(40)+' ok']+lowercase_keys(rng)

def lowercase_keys(rng):
    """Key shapes without upper case: lowercase hex or lowercase alphanumerics."""
    import string
    def lo(n,alpha=string.ascii_lowercase+string.digits):
        while True:
            s=''.join(rng.choice(alpha) for _ in range(n))
            if any(c.isdigit() for c in s) and any(c.isalpha() for c in s): return s
    digits=lambda n: ''.join(rng.choice(string.digits) for _ in range(n))
    return ['xoxp-'+digits(11)+'-'+digits(11)+'-'+digits(12)+'-'+lo(32,'0123456789abcdef'),
            'xoxb-'+digits(12)+'-'+digits(13)+'-'+lo(24),'sk-'+lo(32,'0123456789abcdef'),'ghp_'+lo(36)]

def vis(*args,rc=0,env=None): return execute([VIS,*args],rc,env)
def state(job): return json.loads((BASE/job/'job.json').read_text())
def note(text): log('\n'+text); print(text,flush=True)
def cleanup(job):
    if (BASE/job).exists(): vis('stop','--job',job)
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
