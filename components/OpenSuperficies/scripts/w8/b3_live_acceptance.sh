#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
# Conductor-only, opt-in live acceptance. This file neither builds an image nor
# creates approval records. Agents validate syntax only. No task gate runs here;
# auto_ingest's independent HOST rerun is the declared D section 10 deviation.
set -eu
trap 'rc=$?; trap - EXIT; case "$rc" in 0|2|3|4|5|6|7) ;; 1|124) rc=5;; *) rc=6;; esac; exit "$rc"' EXIT
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
usage() { printf '%s\n' 'usage: b3_live_acceptance.sh --plan | --run <1..8>'; }
fail() { printf '%s\n' "$2" >&2; exit "$1"; }
need() { eval 'value=${'"$1"'-}'; [ -n "$value" ] || fail 2 "required operator input: $1"; }
[ "$#" -gt 0 ] || { usage; exit 2; }
if [ "$#" -eq 1 ] && [ "$1" = --plan ]; then
    cat <<'PLAN'
Prerequisites: E1=B2 acceptance + rootless dedicated Docker 29.6.2; E2=approved b2-0002 image with measured runner pins; E4=auto_ingest REPOS includes vision-engine and wender-agent. E3 gates publication only.
Wall ceiling: 150 minutes for the conductor live list; keep an independent timeout 9000 around the full list. Each wave below is capped at 75 minutes, all commands are bounded.
1. sudo osworker-verify.sh: seat active, socket 0660 osworker:admin; health over seat UDS as admin.
2. Rootless image probe id=10001; five /opt/w8 regular files match measured pins. Never build here.
3. Two registered 10-item manifests, vision-engine and wender-agent: run Muse host driver; retain journal cursor before starting. Explicit W8_MODE=dry before E3, live only after E3. Count 20 receipts, validate HEAD pin, C1-C6 and inference_processes=0. Seat governor caps concurrency at two.
4. Dry: bridge_result:null, reason unavailable, no result.json/outbox and no pushes. Live after E3: auto_ingest.py --once and VERIFIED.tsv entries for all 20 attempts. Independent HOST gate re-run is a deliberate D section 10 Phase 2 deviation; W8_HOST_RERUN_ACK=1 is required.
5. Operator sudo journalctl _UID=<osworker uid> -t w8-capsule since saved cursor: exactly 20 task CREATED (+ probe lines), no surviving w8.seat containers, verified cleanup and body quota.
6. Live socket must-FAIL trio: binds, env_file, docker.sock mount return rc5 before any capsule or CREATED line. Serialize this step; journal cursor + container set unchanged.
7. Separate registered kill-wave: start Muse externally with timeout, wait for active capsule; operator runs kill off, verify no osworker processes, driver local seat_lost rc6; kill on, driver --reconcile fetches authoritative restart receipt rc6 (no second submit). No gate/script starts an unbounded background process.
8. Operator rollback -> verify fails; reinstall same measured seat binary with --seat-bin/--seat-sha256 -> verify succeeds. Do not invoke B2's obsolete setup-without-pin path.
Required common env: W8_CAMPAIGN_DIR (scratch), W8_BRIDGE_DIR, W8_MODE=dry|live.
Step 2: W8_RUNNER_SHA256, W8_BASE_IMAGE=os-worker-base@sha256:<digest>.
Step 3: W8_VISION_REPO, W8_WENDER_REPO, W8_VISION_WAVE, W8_WENDER_WAVE; optional W8_MUSE installed binary.
Step 6: W8_MUST_FAIL_WORKER (registered worker JSON, streams empty; refusals must precede stream reads).
Step 7: W8_KILL_REPO, W8_KILL_WAVE, W8_KILL_PHASE=off|on (start driver separately first).
Step 8: W8_SEAT_BIN, W8_SEAT_SHA256 (conductor-built merged binary; no build in this script).
PLAN
    exit 0
fi
[ "$#" -eq 2 ] && [ "$1" = --run ] || { usage; exit 2; }
case $2 in 1|2|3|4|5|6|7|8) STEP=$2;; *) usage; exit 2;; esac
need W8_CAMPAIGN_DIR; need W8_BRIDGE_DIR; need W8_MODE
case $W8_MODE in dry|live) ;; *) fail 2 'W8_MODE must explicitly be dry or live';; esac
mkdir -p "$W8_CAMPAIGN_DIR"
OSW_UID=$(id -u osworker); [ "$OSW_UID" -gt 0 ] || fail 4 'dedicated user absent'
SOCKET=${W8_SOCKET:-/run/opensuperficies/w8-seat.sock}
MUSE=${W8_MUSE:-/usr/local/libexec/opensuperficies/srf-w8-muse}
expect_rc() { want=$1; shift; if "$@"; then got=0; else got=$?; fi; [ "$got" -eq "$want" ] || fail 5 "expected rc=$want got=$got"; }
docker_osw() { timeout 60 sudo -u osworker env "XDG_RUNTIME_DIR=/run/user/$OSW_UID" taskset -c 10-14 docker --host "unix:///run/user/$OSW_UID/docker.sock" "$@"; }
journal_cursor() { timeout 15 sudo journalctl "_UID=$OSW_UID" -t w8-capsule --no-pager -n 0 --show-cursor | sed -n 's/^-- cursor: //p'; }
journal_since() { timeout 30 sudo journalctl "_UID=$OSW_UID" -t w8-capsule --after-cursor="$1" --no-pager -o cat; }
run_muse() {
    repo=$1; wave=$2; output=$3; shift 3
    if [ "$W8_MODE" = dry ]; then
        expect_rc 4 timeout 4500 taskset -c 10-14 "$MUSE" --dry --campaign-dir "$W8_CAMPAIGN_DIR" --bridge-dir "$W8_BRIDGE_DIR" --repo-dir "$repo" --socket "$SOCKET" --wave "$wave" --wave-timeout 75m "$@" > "$output"
    else
        timeout 4500 taskset -c 10-14 "$MUSE" --campaign-dir "$W8_CAMPAIGN_DIR" --bridge-dir "$W8_BRIDGE_DIR" --repo-dir "$repo" --socket "$SOCKET" --wave "$wave" --wave-timeout 75m "$@" > "$output"
    fi
}
case $STEP in
 1)
    timeout 90 sudo sh "$SCRIPT_DIR/osworker-verify.sh"
    timeout 15 python3 - "$SOCKET" <<'PY'
import json,socket,struct,sys
try:
 with socket.socket(socket.AF_UNIX) as s:
  s.settimeout(10);s.connect(sys.argv[1]);b=b'{"verb":"health"}';s.sendall(struct.pack('!I',len(b))+b)
  def read(n):
   b=b''
   while len(b)<n:
    p=s.recv(n-len(b))
    if not p:raise ValueError('short frame')
    b+=p
   return b
  n=struct.unpack('!I',read(4))[0]
  if not 0<n<=1048576:raise ValueError('header cap')
  r=json.loads(read(n));sys.exit(0 if r['exit_code']==0 else 5)
except (OSError,ValueError,KeyError):sys.exit(5)
PY
    ;;
 2)
    need W8_RUNNER_SHA256; need W8_BASE_IMAGE
    case $W8_BASE_IMAGE in os-worker-base@sha256:*) ;; *) fail 2 'base image must be digest-pinned';; esac
    [ "${#W8_RUNNER_SHA256}" -eq 64 ] || fail 2 'measured runner sha256 required'
    CID=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#CID}" -eq 12 ] || fail 4 'probe id unavailable'
    printf 'W8_CAPSULE_CREATED kind=probe lease=- image=%s container=%s caller_uid=%s\n' "${W8_BASE_IMAGE#os-worker-base@}" "$CID" "$(id -u)" | timeout 15 sudo -u osworker systemd-cat -t w8-capsule
    docker_osw run --rm --name "$CID" --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --security-opt seccomp=/usr/local/share/opensuperficies/w8-seccomp.json --pids-limit 64 --memory 4g --memory-swap 4g --cpuset-cpus 10-14 --oom-score-adj 800 --ulimit nofile=8192:8192 --ulimit core=0 "$W8_BASE_IMAGE" id -u | python3 -c 'import sys;sys.exit(0 if sys.stdin.read().strip()=="10001" else 5)'
    docker_osw image save "$W8_BASE_IMAGE" | timeout 60 python3 -c '
import hashlib,sys,tarfile
want=sys.argv[1];seen={};allowed={"opt/w8/"+x for x in ("ingest","gate","status","extract","purge")}
try:
 archive=tarfile.open(fileobj=sys.stdin.buffer,mode="r|*")
 for outer in archive:
  if not outer.isfile() or not outer.name.endswith("/layer.tar"):continue
  layer=tarfile.open(fileobj=archive.extractfile(outer),mode="r|*")
  for item in layer:
   name=item.name.removeprefix("./").rstrip("/")
   if not name.startswith("opt/w8/"):continue
   assert name in allowed and name not in seen and item.isfile() and item.uid==0 and item.mode==0o555
   h=hashlib.sha256();payload=layer.extractfile(item);remaining=item.size
   for _ in range((item.size+65535)//65536):
    chunk=payload.read(min(remaining,65536));assert chunk;h.update(chunk);remaining-=len(chunk)
   assert remaining==0;seen[name]=h.hexdigest()
 assert set(seen)==allowed and all(value==want for value in seen.values())
except (tarfile.TarError,OSError,AssertionError,ValueError):sys.exit(5)
sys.exit(0)' "$W8_RUNNER_SHA256"
    ;;
 3)
    need W8_VISION_REPO; need W8_WENDER_REPO; need W8_VISION_WAVE; need W8_WENDER_WAVE
    timeout 15 python3 - "$W8_VISION_WAVE" "$W8_WENDER_WAVE" "$W8_BRIDGE_DIR" <<'PY'
import json,pathlib,re,sys
try:
 seen=set()
 for wave,name in zip(sys.argv[1:3],('vision-engine','wender-agent')):
  items=json.loads(pathlib.Path(wave).read_text())
  assert len(items)==10
  for i in items:
   assert re.fullmatch('[a-z0-9-]+',i['task_id']) and re.fullmatch('a[0-9]+',i['attempt'])
   pair=(i['task_id'],i['attempt']);assert pair not in seen;seen.add(pair)
   task=json.loads((pathlib.Path(sys.argv[3])/'tasks'/pair[0]/pair[1]/'task.json').read_text())
   assert task['repo'].split('/')[-1]==name
except (OSError,ValueError,KeyError,AssertionError,TypeError):sys.exit(5)
PY
    CURSOR=$(journal_cursor); [ -n "$CURSOR" ] || fail 4 'journal cursor absent'
    printf '%s\n' "$CURSOR" > "$W8_CAMPAIGN_DIR/b3.cursor"
    run_muse "$W8_VISION_REPO" "$W8_VISION_WAVE" "$W8_CAMPAIGN_DIR/b3-vision.jsonl"
    run_muse "$W8_WENDER_REPO" "$W8_WENDER_WAVE" "$W8_CAMPAIGN_DIR/b3-wender.jsonl"
    ;;
 4)
    if [ "$W8_MODE" = live ]; then
        [ "${W8_HOST_RERUN_ACK:-}" = 1 ] || fail 2 'explicit deliberate host rerun acknowledgement required'
        need W8_AUTO_INGEST
        timeout 4500 python3 "$W8_AUTO_INGEST" --once
    fi
    timeout 30 python3 - "$W8_CAMPAIGN_DIR" "$W8_BRIDGE_DIR" "$W8_MODE" <<'PY'
import json,pathlib,sys
try:
 root=pathlib.Path(sys.argv[1]);rows=[]
 for name in ('b3-vision.jsonl','b3-wender.jsonl'):
  lines=(root/name).read_text().splitlines();assert len(lines)==10
  rows.extend(json.loads(x) for x in lines)
 assert len({(r['task_id'],r['attempt']) for r in rows})==20
 verified=(pathlib.Path(sys.argv[2])/'VERIFIED.tsv').read_text().splitlines() if sys.argv[3]=='live' else []
 for row in rows:
  r=row['receipt'];task=json.loads((pathlib.Path(sys.argv[2])/'tasks'/row['task_id']/row['attempt']/'task.json').read_text())
  assert r['exit_code']==0 and r['repo']['commit']==task['observed_source_commit']
  assert r['cleanup']['status']=='verified' and r['cleanup']['remaining_pids']==0 and r['cleanup']['remaining_workspace_bytes']==0 and r['cleanup']['resources_released'] is True
  p=r['observations']['probes'];assert p['inference_processes']==0 and p['orphans_after_task']==0 and p['etc_write']=='read_only' and p['home_admin']=='absent' and p['egress_probe']=='BLOCKED'
  assert p['bind_mounts']==0 and not p['env_file_used'] and p['env_secret_names']==0 and not p['runtime_socket_visible'] and p['daemon'] in ('rootless','dedicated_user') and p['memswap_equals_memory'] is True
  assert r['extract']['text_only'] is True and r['extract']['staging_residue']==0
  if sys.argv[3]=='dry':assert row['exit_code']==4 and row['reason']=='unavailable' and row['bridge_result'] is None
  else:
   assert row['bridge_result']['mode']=='live'
   branch='bridge/'+row['task_id']+'/'+row['attempt']
   assert any(len(ln.split('\t'))>=5 and ln.split('\t')[4]==branch for ln in verified)
 if sys.argv[3]=='dry':assert not list((root/'w8-muse').rglob('result.json')) and not list((root/'w8-muse').rglob('outbox'))
except (OSError,ValueError,KeyError,AssertionError,TypeError):sys.exit(5)
PY
    ;;
 5)
    CURSOR=$(cat "$W8_CAMPAIGN_DIR/b3.cursor"); [ -n "$CURSOR" ] || fail 4 'wave journal cursor absent'
    journal_since "$CURSOR" | timeout 15 python3 -c 'import sys;lines=sys.stdin.read().splitlines();n=sum(x.startswith("W8_CAPSULE_CREATED kind=task ") for x in lines);print("task_created="+str(n));sys.exit(0 if n==20 else 5)'
    AFTER=$(docker_osw ps -aq --filter label=w8.seat=1)
    [ -z "$AFTER" ] || fail 5 'seat containers survive'
    timeout 30 sudo -u osworker python3 - /var/lib/osworker/.local/state/w8/bodies <<'PY'
import pathlib,sys
try:
 root=pathlib.Path(sys.argv[1]);assert root.is_dir();attempts=list(root.iterdir());assert len(attempts)<=512
 assert sum(p.stat().st_size for p in root.rglob('*') if p.is_file())<=67108864
except (OSError,AssertionError):sys.exit(5)
PY
    ;;
 6)
    need W8_MUST_FAIL_WORKER
    CURSOR=$(journal_cursor); [ -n "$CURSOR" ] || fail 4 'journal cursor absent'
    BEFORE=$(docker_osw ps -aq --filter label=w8.seat=1)
    timeout 30 python3 - "$SOCKET" "$W8_MUST_FAIL_WORKER" <<'PY'
import json,pathlib,socket,struct,sys
try:
 task=json.loads(pathlib.Path(sys.argv[2]).read_text())
 for extra in ({'binds':['/tmp:/workspace']},{'env_file':'/tmp/refused'},{'mounts':[{'type':'bind','source':'/var/run/docker.sock','target':'/var/run/docker.sock'}]}):
  header=dict(verb='submit',worker_task=task,bundle_len=0,patch_len=0,**extra);b=json.dumps(header).encode()
  with socket.socket(socket.AF_UNIX) as s:
   s.settimeout(5);s.connect(sys.argv[1]);s.sendall(struct.pack('!I',len(b))+b)
   def read(n):
    b=b''
    while len(b)<n:
     v=s.recv(n-len(b))
     if not v:raise ValueError('short reply')
     b+=v
    return b
   n=struct.unpack('!I',read(4))[0];assert 0<n<=1048576
   reply=json.loads(read(n));assert reply['exit_code']==5
except (OSError,ValueError,KeyError,AssertionError):sys.exit(5)
PY
    AFTER=$(docker_osw ps -aq --filter label=w8.seat=1)
    [ "$AFTER" = "$BEFORE" ] || fail 5 'must-FAIL created capsule'
    journal_since "$CURSOR" > "$W8_CAMPAIGN_DIR/b3-must-fail-journal.log"
    timeout 15 python3 -c 'import pathlib,sys;sys.exit(5 if any(x.startswith("W8_CAPSULE_CREATED ") for x in pathlib.Path(sys.argv[1]).read_text().splitlines()) else 0)' "$W8_CAMPAIGN_DIR/b3-must-fail-journal.log"
    ;;
 7)
    need W8_KILL_PHASE; need W8_KILL_REPO; need W8_KILL_WAVE
    case $W8_KILL_PHASE in
      off)
        [ -n "$(docker_osw ps -q --filter label=w8.seat=1)" ] || fail 4 'start separate bounded kill-wave driver before off'
        timeout 90 sudo sh "$SCRIPT_DIR/osworker-kill.sh" off
        if pgrep -u osworker >/dev/null; then fail 5 'osworker processes survive off'; else rc=$?; [ "$rc" -eq 1 ] || fail 5 'process check error'; fi
        timeout 70 python3 - "$W8_CAMPAIGN_DIR" "$W8_KILL_WAVE" <<'PYLOCAL'
import json,pathlib,re,sys,time
try:
 items=json.loads(pathlib.Path(sys.argv[2]).read_text());assert len(items)==1
 item=items[0];assert re.fullmatch('[a-z0-9-]+',item['task_id']) and re.fullmatch('a[0-9]+',item['attempt'])
 path=pathlib.Path(sys.argv[1])/'w8-muse'/'local'/(item['task_id']+'.'+item['attempt']+'.json')
 for _ in range(60):
  if path.exists():
   row=json.loads(path.read_text())
   if row.get('exit_code')==6 and row.get('reason')=='seat_lost' and row.get('source')=='local_status':sys.exit(0)
  time.sleep(1)
except (OSError,ValueError,KeyError,AssertionError):sys.exit(5)
sys.exit(5)
PYLOCAL
        ;;
      on)
        timeout 90 sudo sh "$SCRIPT_DIR/osworker-kill.sh" on
        if [ "$W8_MODE" = dry ]; then set -- --dry; else set --; fi
        expect_rc 6 timeout 60 taskset -c 10-14 "$MUSE" "$@" --reconcile --campaign-dir "$W8_CAMPAIGN_DIR" --bridge-dir "$W8_BRIDGE_DIR" --repo-dir "$W8_KILL_REPO" --socket "$SOCKET" --wave "$W8_KILL_WAVE" > "$W8_CAMPAIGN_DIR/b3-kill-reconcile.jsonl"
        timeout 15 python3 - "$W8_CAMPAIGN_DIR/b3-kill-reconcile.jsonl" <<'PY'
import json,pathlib,sys
try:
 rows=[json.loads(x) for x in pathlib.Path(sys.argv[1]).read_text().splitlines()];assert rows
 for r in rows:assert r['source']=='seat_receipt' and r['receipt']['exit_code']==6
except (OSError,ValueError,KeyError,AssertionError):sys.exit(5)
PY
        ;;
      *) fail 2 'W8_KILL_PHASE must be off or on';;
    esac
    ;;
 8)
    need W8_SEAT_BIN; need W8_SEAT_SHA256
    timeout 90 sudo sh "$SCRIPT_DIR/osworker-setup.sh" --rollback
    expect_rc 5 timeout 90 sudo sh "$SCRIPT_DIR/osworker-verify.sh"
    timeout 180 sudo sh "$SCRIPT_DIR/osworker-setup.sh" --seat-bin "$W8_SEAT_BIN" --seat-sha256 "$W8_SEAT_SHA256"
    timeout 90 sudo sh "$SCRIPT_DIR/osworker-verify.sh"
    ;;
esac
printf 'B3_LIVE_ITEM_%s=PASS\n' "$STEP"
