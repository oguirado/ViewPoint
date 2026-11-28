# SPDX-License-Identifier: AGPL-3.0-only
"""Observer attestation (rp4.observer-attestation.v1).

A response is observer evidence only when the swarm CLI's own records show that the seat
registered for a slot returned it for exactly that sealed prompt. Five things must agree:

  the summary the CLI printed, the wave receipt, the wave directory (prompt pack and task),
  the seat registry, and the central swarm ledger outside this lane.

Every link is read from disk again whenever a seal or a card is consumed, so evidence that
changed after acceptance stops counting. A response whose records do not close this chain is
refused; it is never sealed as observer evidence.
"""
import json
import os
import re
from pathlib import Path

import rpr_common as C

SCHEMA = "rp4.observer-attestation.v1"
SEATS = None
SCRIPTED_MODEL = "fake-adapter"
RECEIPT_SCHEMA = "swarm.receipt.v1"
SLOT_ID = "slot_001"
# Receipt fields that name the flight. Each must equal the row the swarm CLI appended to its
# central ledger; fields the ledger normalises (route, seq, coord hashes) are not among them.
IDENTITY = ("adapter", "attempt_id", "billing", "censored", "code", "dispatch_id", "effort_requested",
            "family", "is_error", "model", "outcome", "pack_bytes", "pack_sha256", "rc", "result_sha256",
            "seat", "seat_model", "slot_id", "task_bytes", "task_sha256", "t_end_ms", "t_start_ms", "wave_id")
CHECKS = ("summary", "location", "identity", "registry", "task", "pack", "layer", "movie", "result", "raw_slot",
          "wave_ledger", "central_ledger", "run_state")
DEFAULT_COMMAND = ["python3", "-B", "rp_review.py", "--help"]

LEDGER = None
_SCANNED = {}


def fail(check, detail, command=None):
    raise C.Refused(f"receipt_{check}", f"run the observer again from the same sealed context: {detail}",
                    command or DEFAULT_COMMAND)


def ledger_rows(path=None):
    """Receipt rows of the central ledger keyed by attempt id. Only the appended tail is re-read."""
    if path is None and LEDGER is None:
        raise C.Harness("configure the observer ledger with --ledger")
    path = Path(path or LEDGER)
    try:
        stat = path.stat()
        done = _SCANNED.get(str(path))
        # The ledger only grows. A file that was rewritten in place is read again from the start.
        if (done is None or done["inode"] != stat.st_ino or stat.st_size < done["offset"]
                or (stat.st_size == done["offset"] and stat.st_mtime_ns != done["mtime_ns"])):
            rows, offset = {}, 0
        else:
            rows, offset = dict(done["rows"]), done["offset"]
        with open(path, "rb") as stream:
            stream.seek(offset)
            for line in stream:
                offset += len(line)
                if b'"attempt_id"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("schema") == RECEIPT_SCHEMA and row.get("attempt_id"):
                    rows[row["attempt_id"]] = row
        _SCANNED[str(path)] = {"inode": stat.st_ino, "offset": offset, "rows": rows, "mtime_ns": stat.st_mtime_ns}
        return rows
    except OSError as exc:
        raise C.Harness(f"the central swarm ledger is unreadable: {exc}") from exc


def registry_seat(seat):
    if SEATS is None or not isinstance(seat, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", seat):
        raise C.Harness("configure --seat-registry and use a valid seat name")
    path = SEATS / f"{seat}.json"
    if not path.is_file():
        raise C.Harness(f"the seat registry has no entry for {seat}: {path}")
    return C.read_json(path), str(path), C.sha_file(path)


def check_registry(slot, protocol, command=None):
    """The live seat registry must still describe the seat the protocol preregistered for this slot."""
    expected = protocol["seats"][slot]
    data, path, digest = registry_seat(expected["seat"])
    launcher = Path(str(data.get("launcher", ""))).name
    seen = {"seat": data.get("name"), "model": data.get("model"), "family": data.get("family"), "adapter": launcher}
    if (data.get("schema") != "swarm.seat.v1" or seen != {key: expected[key] for key in ("seat", "model", "family", "adapter")}
            or not isinstance(data.get("expected_model_substr"), str)):
        fail("registry", f"{slot} is preregistered as {expected}, the registry describes {seen}")
    return {"path": path, "sha256_at_check": digest, **seen, "expected_model_substr": data["expected_model_substr"],
            "launcher": data.get("launcher"),
            "qualification_state": (data.get("qualification") or {}).get("state"),
            "seat_state": data.get("state"), "account": (data.get("quota") or {}).get("account")}


def is_scripted(oneshot):
    return oneshot.get("model") == SCRIPTED_MODEL


def oneshot_file(job_dir, attempt):
    return Path(job_dir) / f"oneshot.a{int(attempt)}.json"


def _sha(path):
    try:
        return C.sha_file(path)
    except OSError:
        return None


def _locate(job_dir, record):
    """Attempt number of a record sealed before attempts were recorded: the one whose receipt it pinned."""
    for path in sorted(Path(job_dir).glob("oneshot.a*.json"), key=lambda item: int(item.stem.split(".a")[-1])):
        try:
            data = C.read_json(path)
        except (C.Harness, ValueError):
            continue
        if data.get("receipt_path") == record.get("receipt_path") and record.get("receipt_path"):
            return int(path.stem.split(".a")[-1])
    fail("location", "the observer output of this record is no longer beside its seal")
    return None


def attest(meta, oneshot, job_dir, attempt, protocol, ledger=None, command=None):
    """Close the record chain of one observer call. Returns the attestation to store with the record."""
    job_dir, attempt = Path(job_dir).resolve(), int(attempt)
    command_file = job_dir / f"command.a{attempt}.json"
    if is_scripted(oneshot) or (command_file.is_file() and C.read_json(command_file).get("adapter") == "scripted"):
        if not is_scripted(oneshot):
            fail("summary", "a call launched through the scripted adapter must not claim a model", command)
        return {"schema": SCHEMA, "kind": "scripted", "attempt": attempt,
                "command_sha256": _sha(command_file), "checks": ["scripted_adapter"]}
    if not (oneshot.get("ok") is True and oneshot.get("outcome") == "done" and oneshot.get("rc") == 0
            and isinstance(oneshot.get("result"), str)):
        fail("summary", "the CLI did not report a completed result", command)
    if oneshot.get("seat") != meta["seat"]:
        fail("summary", f"the CLI ran seat {oneshot.get('seat')!r}, the slot is registered with {meta['seat']!r}", command)

    # Where the records live: only inside the wave directory of this attempt, inside this job.
    wave = job_dir / f"wave-a{attempt}"
    receipt_path = Path(str(oneshot.get("receipt_path") or ""))
    if (not receipt_path.is_absolute() or receipt_path.name != f"{SLOT_ID}.receipt.json"
            or receipt_path.resolve().parent != (wave / "receipts").resolve() or not receipt_path.is_file()
            or receipt_path.is_symlink() or wave.is_symlink() or not wave.is_dir()):
        fail("location", "the receipt is not the slot receipt of this attempt's wave directory", command)
    receipt_sha = C.sha_file(receipt_path)
    receipt = C.read_json(receipt_path)

    # The receipt itself: a completed, uncensored flight of one slot.
    identity = (receipt.get("schema") == RECEIPT_SCHEMA and receipt.get("slot_id") == SLOT_ID
                and receipt.get("seat") == meta["seat"] and receipt.get("outcome") == "done"
                and receipt.get("rc") == 0 and receipt.get("worker_rc") == 0 and receipt.get("censored") == 0
                and receipt.get("is_error") is False and receipt.get("verdict") == "pass"
                and isinstance(receipt.get("attempt"), int) and receipt["attempt"] > 0
                and all(isinstance(receipt.get(key), str) and receipt[key]
                        for key in ("attempt_id", "wave_id", "dispatch_id")))
    if not identity:
        fail("identity", "the wave receipt is not a completed flight of this slot", command)

    # The seat: the receipt's model, family and adapter are those the registry gives this seat.
    registry = check_registry(meta["slot"], protocol, command)
    model = str(oneshot.get("model"))
    if (receipt.get("seat_model") != registry["model"] or receipt.get("model") != registry["model"]
            or model != registry["model"] or not re.fullmatch(registry["expected_model_substr"], model)):
        fail("registry", f"the receipt names model {receipt.get('model')!r}, the registry gives {registry['model']!r}", command)
    if receipt.get("family") != registry["family"] or receipt.get("adapter") != registry["adapter"]:
        fail("registry", "the receipt names another family or adapter than the registry", command)

    # The task: the sealed task text is the task the flight was given.
    task = (job_dir / "task.md").read_bytes()
    task_sha = C.sha_bytes(task)
    if not (task_sha == meta["task_sha256"] == receipt.get("task_sha256")
            and len(task) == meta["task_bytes"] == receipt.get("task_bytes")
            and (wave / "oneshot-inputs" / "task.md").read_bytes() == task):
        fail("task", "the task hashed into the receipt is not the sealed task of this context", command)

    # The pack: one compiled pack, hashed into the receipt, with this context as its only layer.
    packs = sorted(wave.glob("pkg_*.md"))
    pack_path = wave / f"pkg_{registry['family']}.md"
    if packs != [pack_path]:
        fail("pack", "the wave holds no single prompt pack for the seat's family", command)
    pack_sha = C.sha_file(pack_path)
    compiled = C.read_json(wave / f"pkg_{registry['family']}.md.manifest.json")
    if not (pack_sha == receipt.get("pack_sha256") == compiled.get("pack_sha256")
            == (compiled.get("compiled") or {}).get("pack_sha256") and pack_path.stat().st_size == receipt.get("pack_bytes")):
        fail("pack", "the prompt pack hashed into the receipt is not the pack on disk", command)
    leaves = (compiled.get("compiled") or {}).get("leaves")
    if not (compiled.get("layers") == [{"path": str(job_dir / "ctx.md"), "sha256": meta["ctx_sha256"]}]
            and leaves == [{"bytes": meta["context_bytes"], "label": "ctx.md", "leaf": leaves[0]["leaf"],
                            "path": "ctx.md", "sha256": meta["ctx_sha256"]}]
            and _sha(job_dir / "ctx.md") == meta["ctx_sha256"]):
        fail("layer", "the prompt pack does not carry this sealed context as its only layer", command)
    ctx = (job_dir / "ctx.md").read_text(encoding="utf-8")
    if meta["movie_sha256"] not in ctx or meta["manifest_sha256"] not in ctx:
        fail("movie", "the sealed prompt does not name the movie and manifest of this record", command)

    # The result: what the CLI printed is what the flight returned.
    result = oneshot["result"]
    result_sha = C.sha_text(result)
    if not (result_sha == receipt.get("result_sha256") == C.sha_text(str(receipt.get("result")))
            and result == str(receipt.get("result"))[:len(result)] and result):
        fail("result", "the printed result is not the result hashed into the receipt", command)
    if (wave / "result.md").is_file() and (wave / "result.md").read_bytes() != str(receipt["result"]).encode("utf-8"):
        fail("result", "the wave result file differs from the receipt", command)

    # The raw adapter record and the slot state of this flight.
    raw = Path(str(receipt.get("raw_path") or ""))
    state_file = wave / "slots" / f"{SLOT_ID}.slot.json"
    if not (raw.parent.resolve() == (wave / "slots").resolve() and raw.is_file() and state_file.is_file()):
        fail("raw_slot", "the raw adapter record of the flight is missing from the wave", command)
    slot_state = C.read_json(state_file)
    if any(slot_state.get(key) != receipt.get(key) for key in ("attempt_id", "dispatch_id", "seat")):
        fail("raw_slot", "the slot state names another flight than the receipt", command)

    # The wave ledger and the central ledger both hold this flight.
    wave_rows = [row for row in (C.parse_json(line) for line in (wave / "receipts.jsonl").read_text().splitlines() if line.strip())
                 if isinstance(row, dict) and row.get("schema") == RECEIPT_SCHEMA and row.get("attempt_id") == receipt["attempt_id"]]
    if len(wave_rows) != 1 or any(wave_rows[0].get(key) != receipt.get(key) for key in IDENTITY):
        fail("wave_ledger", "the wave ledger does not hold exactly this flight", command)
    ledger_file = Path(ledger) if ledger else LEDGER
    row = ledger_rows(ledger_file).get(receipt["attempt_id"])
    if row is None:
        fail("central_ledger", "the central swarm ledger holds no flight with this attempt id", command)
    differing = [key for key in IDENTITY if row.get(key) != receipt.get(key)]
    if differing:
        fail("central_ledger", f"the central ledger row differs in {differing}", command)

    # The run state of the wave agrees.
    run = C.read_json(wave / "run.state.json")
    if not (run.get("seat") == meta["seat"] and run.get("state") == "finished" and run.get("status") == "done"
            and run.get("pack_sha256") == pack_sha and run.get("model") == model and run.get("wave_id") == receipt["wave_id"]):
        fail("run_state", "the run state of the wave does not describe this finished flight", command)

    return {"schema": SCHEMA, "kind": "swarm", "attempt": attempt, "checks": list(CHECKS),
            "core": {"slot_id": SLOT_ID, "wave": wave.name, "receipt_sha256": receipt_sha,
                     "attempt_id": receipt["attempt_id"], "wave_id": receipt["wave_id"],
                     "dispatch_id": receipt["dispatch_id"], "task_sha256": task_sha, "pack_sha256": pack_sha,
                     "layer_sha256": meta["ctx_sha256"], "result_sha256": result_sha,
                     "registry": {key: registry[key] for key in ("path", "seat", "model", "family", "adapter", "launcher")},
                     "ledger_path": str(ledger_file), "ledger_row": {key: row.get(key) for key in IDENTITY}},
            "info": {"model": model, "adapter": receipt["adapter"], "family": receipt["family"],
                     "effort_requested": receipt.get("effort_requested"), "loop_mode": receipt.get("loop_mode"),
                     "started_ms": receipt.get("t_start_ms"), "ended_ms": receipt.get("t_end_ms"),
                     "duration_ms": receipt.get("duration_ms"), "usage_norm": receipt.get("usage_norm"),
                     "warnings": receipt.get("warnings"), "registry_sha256_at_seal": registry["sha256_at_check"],
                     "qualification_state": registry["qualification_state"], "seat_state": registry["seat_state"],
                     "account": registry["account"], "raw_records": len(C.parse_json(raw.read_text(encoding="utf-8")))
                     if raw.stat().st_size < 64 * 1024 * 1024 else None}}


def recheck(record, meta, job_dir, protocol, ledger=None, command=None):
    """Read the whole chain of a stored record again and require it to be what was stored."""
    job_dir = Path(job_dir).resolve()
    attestation = record.get("attestation")
    attempt = record.get("attempt")
    if attempt is None:
        attempt = _locate(job_dir, record)
    path = Path(record["oneshot"]) if record.get("oneshot") else oneshot_file(job_dir, attempt)
    if not path.is_file():
        fail("location", "the observer output of this record is gone", command)
    if record.get("oneshot_sha256") and _sha(path) != record["oneshot_sha256"]:
        fail("summary", "the observer output changed after the record was written", command)
    oneshot = C.read_json(path)
    now = attest(meta, oneshot, job_dir, attempt, protocol, ledger, command)
    if record.get("receipt_sha256"):
        # A swarm receipt is re-read through the chain; the stub receipt of the scripted adapter by its digest.
        pinned = now["core"]["receipt_sha256"] if now["kind"] == "swarm" else _sha(record.get("receipt_path") or "")
        if pinned != record["receipt_sha256"]:
            fail("location", "the receipt changed after the record was written", command)
    if record.get("raw_result_sha256") and now["kind"] == "swarm" and now["core"]["result_sha256"] != record["raw_result_sha256"]:
        fail("result", "the result changed after the record was written", command)
    if now["kind"] != record.get("observer_kind"):
        fail("summary", f"the record was filed as {record.get('observer_kind')!r}, its chain closes as {now['kind']!r}", command)
    if attestation is not None:
        stored = attestation.get("core") if attestation.get("kind") == "swarm" else attestation
        current = now.get("core") if now["kind"] == "swarm" else now
        if stored != current:
            fail("central_ledger", "the observer records of this seal no longer close the chain they closed when it was accepted", command)
    return {**now, "attempt": attempt}


def summary(attestation):
    """One line for reports: what the evidence is and under which identities."""
    if not attestation or attestation.get("kind") != "swarm":
        return {"kind": "scripted" if attestation else "none", "attested": False}
    core = attestation["core"]
    return {"kind": "swarm", "attested": True, "seat": core["registry"]["seat"], "model": attestation["info"]["model"],
            "attempt_id": core["attempt_id"][:16], "receipt_sha256": core["receipt_sha256"][:16],
            "result_sha256": core["result_sha256"][:16], "pack_sha256": core["pack_sha256"][:16],
            "qualification_state": attestation["info"]["qualification_state"]}
