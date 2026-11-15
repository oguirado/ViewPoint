"""Black-box conformance runner for the four srf-cu-* CLIs (contracts/cubook).

  python3 -B contracts/cubook/run_conformance.py --bin-dir DIR [--only CLI] [--work-dir D] [--json OUT]

DIR must hold srf-cu-exec, srf-cu-mcp, srf-cu-snap, srf-cu-wlcap (any implementation). Each fixtures/<cli>/<case>/ holds
argv.json (tokens; {WORK} = scratch cwd, {RAM} = managed ramws dir), env.json ({"set":{},"unset":[]}), optional stdin,
expected_stdout (byte-identical unless meta.volatile), expected_rc, files/ (copied into the cwd), expected_files/ and
meta.json. Exit: 0 all pass; 5 any failure or missing binary; 2 usage. Never 1. Frames only go to /dev/shm/vqa-ramws/..."""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CLIS = ("exec", "mcp", "snap", "wlcap")
RAM_ROOT = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"
RC_OK = (0, 2, 3, 4, 5, 6, 7)
VOLATILE_ALLOW = ("ts_ns", "ts_ms", "started_ns", "ended_ns", "elapsed_ms", "dur_ms", "pid", "tmp_dir", "work_dir", "out_dir", "run_id",
                  "span_ms", "longest_run_ms", "segment_ms")
PLACEHOLDER = "<V>"


def load_schema(sid, schema_dir=None):
    d = schema_dir or os.path.join(HERE, "schemas")
    with open(os.path.join(d, sid.replace("/", "-") + ".schema.json")) as f:
        return json.load(f)


def validate(obj, sid, schema_dir=None):
    import jsonschema
    jsonschema.Draft202012Validator(load_schema(sid, schema_dir)).validate(obj)


def _walk(x, keys, seen):
    if isinstance(x, dict):
        for k in list(x):
            if k in keys:
                seen.add(k)
                x[k] = PLACEHOLDER
            else:
                _walk(x[k], keys, seen)
    elif isinstance(x, list):
        for v in x:
            _walk(v, keys, seen)


def normalise(data, keys):
    """Replace the declared volatile keys by <V>; returns (bytes, keys_seen). Keys outside VOLATILE_ALLOW are refused."""
    bad = [k for k in keys if k not in VOLATILE_ALLOW]
    if bad:
        raise ValueError("volatile keys outside the closed allowlist: %s" % bad)
    seen, out = set(), []
    for ln in data.decode("utf-8", "replace").split("\n"):
        try:
            v = json.loads(ln)
        except ValueError:
            out.append(ln)
            continue
        _walk(v, set(keys), seen)
        out.append(json.dumps(v, separators=(",", ":")))
    return "\n".join(out).encode(), seen


class Ram:
    """A managed ramws run dir (owner.json + a flock-held .lock), the shape CheckRunDir accepts."""

    def __init__(self, managed, tag):
        self.path = os.path.join(RAM_ROOT, "conf-%d-%s" % (os.getpid(), tag))
        os.makedirs(self.path, exist_ok=True)
        self.lk = None
        if managed:
            with open(os.path.join(self.path, "owner.json"), "w") as f:
                f.write("{}")
            self.lk = open(os.path.join(self.path, ".lock"), "w")
            fcntl.flock(self.lk, fcntl.LOCK_EX)

    def close(self):
        if self.lk:
            self.lk.close()
        shutil.rmtree(self.path, ignore_errors=True)


def rd(p, mode="rb", default=None):
    if not os.path.exists(p):
        return default
    with open(p, mode) as f:
        return f.read()


def run_case(bin_dir, cli, fx, work_base):
    name = "%s/%s" % (cli, os.path.basename(fx))
    meta = json.loads(rd(os.path.join(fx, "meta.json"), "r", "{}"))
    argv = json.loads(rd(os.path.join(fx, "argv.json"), "r"))
    envspec = json.loads(rd(os.path.join(fx, "env.json"), "r", "{}"))
    want_rc = int(rd(os.path.join(fx, "expected_rc"), "r").strip())
    want_out = rd(os.path.join(fx, "expected_stdout"), "rb", b"")
    work = tempfile.mkdtemp(prefix="c_", dir=work_base)
    ram = None
    fails = []
    try:
        if os.path.isdir(os.path.join(fx, "files")):
            shutil.copytree(os.path.join(fx, "files"), work, dirs_exist_ok=True)
        if meta.get("ram"):
            ram = Ram(meta["ram"] == "managed", os.path.basename(work))
        sub = {"{WORK}": work, "{RAM}": ram.path if ram else RAM_ROOT + "/none"}
        args = []
        for t in argv:
            for k, v in sub.items():
                t = t.replace(k, v)
            args.append(t)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": work, "LANG": "C"}
        env.update(envspec.get("set", {}))
        for k in envspec.get("unset", []):
            env.pop(k, None)
        stdin = rd(os.path.join(fx, "stdin"), "rb", b"")
        try:
            p = subprocess.run([os.path.join(bin_dir, "srf-cu-" + cli)] + args, input=stdin, cwd=work, env=env,
                               capture_output=True, timeout=meta.get("timeout", 60))
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return name, ["timeout"]
        if rc not in RC_OK:
            fails.append("rc %d outside {0,2..7}" % rc)
        if rc != want_rc:
            fails.append("rc %d want %d (stderr: %s)" % (rc, want_rc, err[:160].decode("utf-8", "replace").strip()))
        vol = meta.get("volatile", [])
        try:
            if vol:
                n_out, seen = normalise(out, vol)
                n_want, _ = normalise(want_out, vol)
                miss = [k for k in vol if k not in seen]
                if miss:
                    fails.append("volatile keys absent pre-strip: %s" % miss)
            else:
                n_out, n_want = out, want_out
        except ValueError as e:
            fails.append(str(e))
            n_out, n_want = out, want_out
        if n_out != n_want:
            fails.append("stdout differs: got %r want %r" % (n_out[:200], n_want[:200]))
        if meta.get("stderr_contains") and meta["stderr_contains"].encode() not in err:
            fails.append("stderr lacks %r" % meta["stderr_contains"])
        ss = meta.get("stdout_schema")
        if ss:
            try:
                for ln in out.decode().splitlines():
                    if ln.strip():
                        validate(json.loads(ln), ss)
            except Exception as e:
                fails.append("stdout schema %s: %s" % (ss, str(e).splitlines()[0][:160]))
        for ef in meta.get("expect_files", []):
            data = rd(os.path.join(work, ef["path"]), "rb")
            if data is None:
                fails.append("missing file %s" % ef["path"])
                continue
            if ef.get("bytes") and data != rd(os.path.join(fx, "expected_files", ef["path"]), "rb"):
                fails.append("file %s differs" % ef["path"])
            if ef.get("schema"):
                try:
                    lines = data.decode().splitlines() if ef.get("jsonl") else [data.decode()]
                    if not any(l.strip() for l in lines):
                        raise ValueError("empty file")
                    for ln in lines:
                        if ln.strip():
                            validate(json.loads(ln), ef["schema"])
                except Exception as e:
                    fails.append("file %s schema %s: %s" % (ef["path"], ef["schema"], str(e).splitlines()[0][:160]))
        return name, fails
    finally:
        if ram:
            ram.close()
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    ap = argparse.ArgumentParser(prog="run_conformance.py")
    ap.add_argument("--bin-dir", required=True)
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--only", choices=CLIS)
    ap.add_argument("--work-dir", default=os.environ.get("CUBOOK_WORK") or os.path.join(REPO, "build", "cubook_conf"))
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    missing = [c for c in CLIS if not os.access(os.path.join(a.bin_dir, "srf-cu-" + c), os.X_OK)]
    if missing:
        print("GATE: missing binaries in %s: %s" % (a.bin_dir, ", ".join("srf-cu-" + m for m in missing)))
        return 5
    os.makedirs(a.work_dir, exist_ok=True)
    results, bad = [], []
    for cli in CLIS if not a.only else (a.only,):
        d = os.path.join(a.fixtures, cli)
        for case in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not os.path.exists(os.path.join(d, case, "argv.json")):
                continue
            name, fails = run_case(os.path.abspath(a.bin_dir), cli, os.path.join(d, case), a.work_dir)
            results.append({"case": name, "ok": not fails, "fails": fails})
            if fails:
                bad.append(name)
                print("FAIL", name, "; ".join(fails))
    summary = {"schema": "os.cu.conformance/v1", "cases": len(results), "failed": bad}
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"summary": summary, "results": results}, f, indent=1, sort_keys=True)
    print(json.dumps(summary, sort_keys=True))
    if not results:
        return 5
    return 5 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
