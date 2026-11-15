"""Build the pinned vision-engine wheel from the sealed tag and verify its sha256 (A2.1). Stdlib only.

  build_wheel.py            build into build/wheels/ and verify
  build_wheel.py --check    rebuild in scratch, verify sha, require build/wheels, run the peer-equivalence witness (read-only)
                            [--peer WHEEL] compares against an explicit law4 wheel; witness equal:<sha8> when it matches the pin
rc: 0 ok, 2 usage, 5 E_PIN (mismatch; never re-pin). Never 1.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TAG = "ve-sealed-1-law4"
EXPECTED_COMMIT = "2612b3acd67c49234adfaa77ab2b3803d5a81cb5"
EXPECTED_WHEEL_SHA256 = "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5"
WHEEL_NAME = "vision_engine-1.1.0-py3-none-any.whl"
VE_DEFAULT = "/home/user/Documentos/vision-engine"
# W3b CI-in-VM: VE_REPO names another checkout of the peer (absolute, existing dir); the tag must still resolve to
# EXPECTED_COMMIT and the wheel must still hash to EXPECTED_WHEEL_SHA256, so a wrong clone fails E_PIN.
_ve_env = os.environ.get("VE_REPO", "")
VE = _ve_env if os.path.isabs(_ve_env) and os.path.isdir(_ve_env) else VE_DEFAULT
PEER_WHEEL_SHA256 = "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5"
PEER_WHEEL = VE + "/build/wheels/" + WHEEL_NAME
SCRATCH = REPO / "build" / "room_L4"
OUT = REPO / "build" / "wheels"
VENDORED = REPO / "vision" / "VENDORED.json"


class BuildError(Exception):
    pass


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _git(ve, *args):
    r = subprocess.run(["git", "-C", ve] + list(args), capture_output=True, timeout=60)
    if r.returncode != 0:
        raise BuildError("git %s failed: %s" % (" ".join(args), r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout


def extract_tag(ve, dest):
    commit = _git(ve, "rev-parse", TAG + "^{commit}").decode().strip()
    if commit != EXPECTED_COMMIT:
        raise BuildError("tag %s is %s, expected %s" % (TAG, commit, EXPECTED_COMMIT))
    sde = int(_git(ve, "log", "-1", "--format=%ct", TAG).decode().strip())
    data = _git(ve, "archive", TAG)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter="data")  # plain extractall, exactly as the law4 tag tool (no chmod: it changes the wheel sha)
    return sde


def build_from_tree(tree, wheel_dir, sde):
    """pip wheel with SOURCE_DATE_EPOCH (reproducible); returns the single wheel path."""
    shutil.rmtree(wheel_dir, ignore_errors=True)
    env = {"PATH": os.environ.get("PATH", ""), "SOURCE_DATE_EPOCH": str(sde), "PYTHONDONTWRITEBYTECODE": "1",
           "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "HOME": os.environ.get("HOME", "/nonexistent")}
    r = subprocess.run([sys.executable, "-B", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "-q", "-w", str(wheel_dir), str(tree)], env=env, cwd=str(tree), capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        raise BuildError("pip wheel failed: " + r.stderr.strip()[-300:])
    whls = sorted(Path(wheel_dir).glob("*.whl"))
    if len(whls) != 1:
        raise BuildError("expected one wheel, got %d" % len(whls))
    return whls[0]


def members_equivalent(a, b):
    """Every zip member except *.dist-info/RECORD has the same sha256 in both wheels. Returns (ok, differing names)."""
    def table(p):
        with zipfile.ZipFile(p) as z:
            return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if not n.endswith(".dist-info/RECORD")}
    ta, tb = table(a), table(b)
    diffs = sorted(n for n in set(ta) | set(tb) if ta.get(n) != tb.get(n))
    return not diffs, diffs


def witness_for(whl, peer, peer_sha=PEER_WHEEL_SHA256):
    """Peer-equivalence witness. The peer path is mutable, so its sha must equal the pinned one before members are compared;
    a rebuilt peer is reported as peer_moved:<sha8> (rc 0). A member mismatch with the pinned peer is a BuildError (rc 5)."""
    if not Path(peer).is_file():
        return "absent"
    got = sha256_file(peer)
    if got != peer_sha:
        return "peer_moved:%s" % got[:8]
    ok, diffs = members_equivalent(whl, peer)
    if not ok:
        raise BuildError("peer wheel member mismatch: %s" % diffs[:5])
    return "equal:%s" % got[:8]


def _record(witness):
    """Persist the witness (build mode only). A moved or absent peer never rewrites the stored witness."""
    if not witness.startswith("equal:"):
        return
    try:
        v = json.loads(VENDORED.read_text(encoding="utf-8"))
        if v.get("equivalence_witness") == witness:
            return
        v["equivalence_witness"] = witness
        VENDORED.write_text(json.dumps(v, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass


def run(check, ve, peer):
    tree = SCRATCH / "engine-2612b3a"
    sde = extract_tag(ve, tree)
    whl = build_from_tree(tree, SCRATCH / "wheel_out", sde)
    got = sha256_file(whl)
    if got != EXPECTED_WHEEL_SHA256:
        raise BuildError("wheel sha256 %s != pinned %s (STOP, never re-pin)" % (got, EXPECTED_WHEEL_SHA256))
    witness = witness_for(whl, peer)
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / WHEEL_NAME
    if check:
        if not dst.is_file():
            raise BuildError("build/wheels/%s missing (run build_wheel.py first)" % WHEEL_NAME)
        if sha256_file(dst) != EXPECTED_WHEEL_SHA256:
            raise BuildError("build/wheels wheel sha mismatch")
    else:
        for old in OUT.glob("*.whl"):
            old.unlink()
        shutil.copyfile(whl, dst)
    if not check:  # --check is read-only: it never writes VENDORED.json
        _record(witness)
    return {"code": "OK", "wheel_sha256": got, "witness": witness, "installed": dst.exists()}


def main(argv):
    ap = argparse.ArgumentParser(prog="build_wheel.py", add_help=False)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ve", default=VE)
    ap.add_argument("--peer", default=PEER_WHEEL)
    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        return 2
    try:
        res = run(ns.check, ns.ve, ns.peer)
    except (BuildError, OSError, subprocess.SubprocessError, tarfile.TarError, ValueError) as e:
        sys.stdout.write(json.dumps({"code": "E_PIN", "detail": str(e)}, sort_keys=True) + "\n")
        return 5
    except Exception as e:  # noqa: BLE001  rc 1 is unreachable
        sys.stdout.write(json.dumps({"code": "E_GATE", "detail": "%s: %s" % (type(e).__name__, e)}, sort_keys=True) + "\n")
        return 5
    sys.stdout.write(json.dumps(res, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
