"""G5 nonce bridge: behavioural probe of a bridge module, in a throw-away runtime directory.

Desired (COMMON.md "Bridge"): only `<nonce>_<event_id>.py` runs; anything else is moved to
inbox/rejected/ and never executed; the answer is outbox/<event_id>.out with first line OK or ERROR.
Observed: the gate drops one valid job and several forged ones into a temp RT, calls the bridge's
`poll_once(rt, nonce, namespace)` and looks at effects (a sentinel file each job would create),
not at what the bridge says it did.

The bridge under test is imported in-process: point this gate only at bridge code of this repo.
A bridge without `poll_once` is UNOBSERVED (not green): there is nothing this probe can call.
"""
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
import tempfile

from common import finding, result

PROBE_NONCE = "gateprobe7f3a9c1e"


def load_bridge(path):
    spec = importlib.util.spec_from_file_location("bridge_under_test_%s" % hashlib.sha256(path.encode()).hexdigest()[:8],
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    saved = {k: os.environ.get(k) for k in ("VP_SWARM_RT", "VP_SWARM_NONCE")}
    os.environ.pop("VP_SWARM_NONCE", None)
    os.environ["VP_SWARM_RT"] = tempfile.gettempdir() + "/vp-gate-unused"
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def _job(sentinel, fail=False):
    return "with open(%r, 'w') as f:\n    f.write('ran')\n%s" % (sentinel, "raise RuntimeError('probe')\n" if fail else "")


def _poll(poll_once, rt, nonce, findings, log):
    """Call the bridge; a crash is an observation (RED), and the effects are still inspected."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            try:
                poll_once(rt, nonce, {}, log=log.append)
            except TypeError as e:
                if "log" not in str(e):
                    raise
                poll_once(rt, nonce, {})
    except Exception as e:
        findings.append(finding("BRIDGE_CRASHED", "poll_once", "%s: %s" % (type(e).__name__, str(e)[:100])))


def probe(poll_once, nonce=PROBE_NONCE):
    """Return (findings, observed) for one poll_once implementation."""
    findings, observed = [], {}
    forged = {
        "NO_NONCE": "evil-a.py",
        "WRONG_NONCE": "0000deadbeef0000_evil-b.py",
        "NONCE_PREFIX": nonce[:-1] + "_evil-c.py",
        "NONCE_SUPERSTRING": nonce + "x_evil-d.py",
        "NONCE_CASE": nonce.upper() + "_evil-e.py",
        "EMPTY_HEAD": "_evil-f.py",
    }
    with tempfile.TemporaryDirectory(prefix="vp-gate-g5-") as rt:
        inbox, outbox = os.path.join(rt, "inbox"), os.path.join(rt, "outbox")
        marks = os.path.join(rt, "marks")
        for d in (inbox, outbox, marks):
            os.makedirs(d)
        for rule, name in forged.items():
            with open(os.path.join(inbox, name), "w") as f:
                f.write(_job(os.path.join(marks, rule)))
        with open(os.path.join(inbox, "%s_ok-1.py" % nonce), "w") as f:
            f.write(_job(os.path.join(marks, "VALID")))
        with open(os.path.join(inbox, "%s_err-1.py" % nonce), "w") as f:
            f.write(_job(os.path.join(marks, "VALID_ERR"), fail=True))
        log = []
        _poll(poll_once, rt, nonce, findings, log)
        ran = sorted(os.listdir(marks))
        observed["executed"] = ran
        rejected = os.listdir(os.path.join(inbox, "rejected")) if os.path.isdir(os.path.join(inbox, "rejected")) else []
        observed["rejected"] = len(rejected)
        for rule, name in forged.items():
            if rule in ran:
                findings.append(finding("FORGED_JOB_EXECUTED", "inbox/" + name.replace(nonce, "<nonce>"),
                                        "%s job ran without the run nonce" % rule))
            elif not any(r.endswith(name) for r in rejected):
                findings.append(finding("FORGED_JOB_NOT_QUARANTINED", "inbox/" + name.replace(nonce, "<nonce>"),
                                        "%s job was not moved to inbox/rejected/" % rule))
        for mark, eid, want in (("VALID", "ok-1", "OK"), ("VALID_ERR", "err-1", "ERROR")):
            outp = os.path.join(outbox, eid + ".out")
            if mark not in ran:
                findings.append(finding("VALID_JOB_NOT_RUN", "inbox/<nonce>_%s.py" % eid, "nonce job did not execute"))
                continue
            try:
                with open(outp) as f:
                    lines = f.read().split("\n")
            except OSError:
                findings.append(finding("NO_OUTBOX_ANSWER", "outbox/%s.out" % eid, "job ran but no answer was written"))
                continue
            if lines[0] != want:
                findings.append(finding("WRONG_STATUS_LINE", "outbox/%s.out" % eid,
                                        "first line %r, want %r" % (lines[0][:20], want)))
            meta = None
            if len(lines) > 1 and lines[1].startswith("META "):
                try:
                    meta = json.loads(lines[1][5:])
                except ValueError:
                    pass
            if meta and "mac" in meta and "script_sha256" in meta:
                good = hmac.new(nonce.encode(), ("%s\n%s\n%s" % (eid, lines[0], meta["script_sha256"])).encode(),
                                hashlib.sha256).hexdigest()
                observed["outbox_mac"] = "verified"
                if not hmac.compare_digest(good, str(meta["mac"])):
                    observed["outbox_mac"] = "mismatch"
                    findings.append(finding("OUTBOX_MAC_MISMATCH", "outbox/%s.out" % eid, "answer MAC does not verify"))
            else:
                observed.setdefault("outbox_mac", "absent")
        # a bridge started WITHOUT a nonce must run nothing at all
        for name in ("_ghost-1.py", "ghost-2.py"):
            with open(os.path.join(inbox, name), "w") as f:
                f.write(_job(os.path.join(marks, "EMPTY_NONCE_" + name)))
        _poll(poll_once, rt, "", findings, log)
        for m in sorted(os.listdir(marks)):
            if m.startswith("EMPTY_NONCE_"):
                findings.append(finding("FORGED_JOB_EXECUTED", "inbox/" + m[12:], "bridge with an empty nonce ran a job"))
    return findings, observed


def run(bridges=()):
    findings, observed, n = [], {}, 0
    for path in bridges:
        if not os.path.isfile(path):
            findings.append(finding("BRIDGE_MISSING", path, "no such bridge file", "unobserved"))
            continue
        try:
            mod = load_bridge(path)
        except Exception as e:  # import error of the unit under test is an observation gap, not a pass
            findings.append(finding("BRIDGE_IMPORT_FAILED", path, "%s: %s" % (type(e).__name__, str(e)[:100]),
                                    "unobserved"))
            continue
        if not callable(getattr(mod, "poll_once", None)):
            findings.append(finding("NO_PROBE_SURFACE", path, "bridge exposes no poll_once(rt, nonce, namespace)",
                                    "unobserved"))
            continue
        f, obs = probe(mod.poll_once)
        for x in f:
            x["where"] = "%s -> %s" % (path, x["where"])
        findings += f
        observed[path] = obs
        n += 1
    return result("G5_nonce_bridge", {"runs": ["<nonce>_<event_id>.py"], "forged_jobs_executed": 0,
                                      "forged_jobs_quarantined": "all", "answer_first_line": ["OK", "ERROR"]},
                  observed, findings, n)
