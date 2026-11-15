#!/usr/bin/env python3
"""C2 executable reference contract; no runtime, network, daemon or provider calls.

All fixture capacities, times, costs and probe rows are [TARGET] synthetic values,
not host measurements. [CITED] public lease fields and states are checked against
the unchanged worker-task schema. This model deliberately uses durable snapshots
and injected crash boundaries; it does not prove filesystem/daemon atomicity.
"""

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest


MAX_INT = (1 << 63) - 1  # [TARGET] checked signed integer control-plane range.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LIVE = {"ADMITTED", "STARTING", "RUNNING", "GATING", "COLLECTING"}
TERMINAL = {"SUCCEEDED", "FAILED", "REFUSED"}
STATES = LIVE | TERMINAL | {"QUEUED", "STOPPING", "INDETERMINATE"}
LEASE_FIELDS = {"schema", "lease_id", "target_id", "target_kind", "state", "restarts", "receipt"}


class Refusal(ValueError):
    def __init__(self, reason, rc=5):
        super().__init__(reason)
        self.rc = rc


class Crash(RuntimeError):
    pass


def require(condition, reason, rc=5):
    if not condition:
        raise Refusal(reason, rc)


def integer(value):
    require(type(value) is int and 0 <= value <= MAX_INT, "checked integer", 2)
    return value


def total(*values):
    result = sum(integer(value) for value in values)
    require(result <= MAX_INT, "integer overflow", 2)
    return result


def canonical(value):
    def visit(item):
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int:
            integer(item)
        elif type(item) is list:
            for child in item:
                visit(child)
        elif type(item) is dict:
            require(all(type(key) is str for key in item), "string keys", 2)
            for child in item.values():
                visit(child)
        else:
            raise Refusal("integer-only JSON", 2)
    visit(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def target_id(host, outer_instance, destination):
    return "tgt-" + digest([host, outer_instance, destination])


def validate_lease(lease):
    require(set(lease) == LEASE_FIELDS, "closed scheduler lease", 2)
    require(lease["schema"] == "os.cu.lease/v1", "lease schema", 2)
    require(all(type(lease[key]) is str and ID.fullmatch(lease[key])
                for key in ("lease_id", "target_id")), "opaque IDs", 2)
    require(lease["target_kind"] in {"capsule", "microvm", "browser_window"}, "target kind", 2)
    require(lease["state"] in STATES, "state", 2)
    require(integer(lease["restarts"]) <= 16, "restart schema bound", 2)
    receipt = lease["receipt"]
    if receipt is not None:
        require(type(receipt) is dict and set(receipt) == {"schema", "sha256"}, "receipt pointer", 2)
        require(receipt["schema"] in {"os.cu.worker-receipt/v1", "bridge-result-v1"}, "receipt schema", 2)
        require(type(receipt["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]),
                "receipt digest", 2)
    if lease["state"] in TERMINAL:
        require(receipt is not None, "terminal receipt required", 2)
    elif lease["state"] != "INDETERMINATE":
        require(receipt is None, "live receipt forbidden", 2)
    return lease


def hardened_profile():
    return {"base_image": "os-worker-base", "repo_code_in_image": 0,
            "workspace": "tmpfs", "extract": "tar-exec", "text_only": True,
            "inference": "host", "daemon": "rootless", "read_only": True,
            "cap_drop": ["ALL"], "no_new_privileges": True,
            "bind_mounts": [], "env_file": None, "runtime_socket": False,
            "memory_bytes": 4096, "memswap_bytes": 4096, "pids_limit": 256,
            "nofile": 8192, "interactive_cpus": [0], "cpuset": [1],
            "devices": [], "phase": "docker", "egress": "none",
            "host_rules_verified": False, "frames_root": "/dev/shm"}


def validate_profile(profile):
    require(set(profile) == set(hardened_profile()), "closed hardening profile")
    for key, value in {"base_image": "os-worker-base", "repo_code_in_image": 0,
                       "workspace": "tmpfs", "extract": "tar-exec", "text_only": True,
                       "inference": "host", "read_only": True, "cap_drop": ["ALL"],
                       "no_new_privileges": True, "bind_mounts": [], "env_file": None,
                       "runtime_socket": False, "frames_root": "/dev/shm"}.items():
        require(type(profile[key]) is type(value) and profile[key] == value, "A4 " + key)
    require(profile["daemon"] in {"rootless", "dedicated_user"}, "daemon privilege")
    require(integer(profile["memory_bytes"]) > 0 and
            profile["memswap_bytes"] == profile["memory_bytes"], "swap refused")
    require(integer(profile["pids_limit"]) > 0 and integer(profile["nofile"]) > 0, "finite process limits")
    require(profile["cpuset"] and not set(profile["cpuset"]) & set(profile["interactive_cpus"]), "interactive CPU")
    require(profile["phase"] in {"docker", "microvm"}, "phase")
    require(profile["devices"] == (["/dev/kvm"] if profile["phase"] == "microvm" else []), "device phase")
    require(profile["egress"] == "none" or
            (profile["egress"] == "host_rules" and profile["host_rules_verified"] is True), "unverified egress")


def governor(policy, telemetry, request):
    now, stamp = integer(telemetry["now_ms"]), integer(telemetry["sample_ms"])
    require(now >= stamp and now - stamp <= policy["sample_max_age_ms"], "stale telemetry", 4)
    require(telemetry["controllers_verified"] is True and telemetry["swap_zero"] is True, "controller proof", 4)
    require(telemetry["psi_bp"] < policy["psi_high_bp"], "pressure", 4)
    require(request["cpuset"] and len(request["cpuset"]) == len(set(request["cpuset"])) and
            set(request["cpuset"]) <= set(telemetry["effective_cpus"]) and
            not set(request["cpuset"]) & set(telemetry["interactive_cpus"]), "CPU set", 4)
    require(telemetry["cpu_ancestors"] and telemetry["hierarchy_complete"] is True, "complete CPU ancestry", 4)
    cpu = integer(request["cpu_milli"])
    require(cpu > 0 and total(telemetry["cpu_reserved_milli"], cpu) <= policy["cpu_budget_milli"], "CPU budget", 4)
    require(cpu <= len(request["cpuset"]) * 1000, "CPU affinity budget", 4)
    for limit in telemetry["cpu_ancestors"]:
        if limit is not None:
            quota, period = integer(limit["quota_us"]), integer(limit["period_us"])
            require(period > 0 and quota <= MAX_INT // 1000, "quota overflow", 2)
            require(total(telemetry["cpu_reserved_milli"], cpu) <= quota * 1000 // period, "ancestor CPU", 4)
    ram = integer(request["ram_bytes"])
    require(ram > 0 and telemetry["memory_ancestors"], "ancestor telemetry", 4)
    require(telemetry["memory_ancestors"][0]["limit"] is not None, "owned leaf must be bounded", 4)
    for ancestor in telemetry["memory_ancestors"]:
        for key in ("current", "committed", "pending", "running_slack"):
            integer(ancestor[key])
        limit = ancestor["limit"]
        if limit is not None:
            integer(limit)
            require(total(ancestor["committed"], ram) <= limit, "ancestor commitments", 4)
            # current already includes charged usage: add only pending maxima and growth slack.
            require(total(ancestor["current"], ancestor["pending"], ancestor["running_slack"], ram,
                          policy["emergency_bytes"]) <= limit, "ancestor live guard", 4)
    require(total(telemetry["host_pending"], telemetry["host_running_slack"], ram,
                  policy["host_floor_bytes"]) <= telemetry["host_available"], "host floor", 4)
    knee = policy["measured_knee"]
    cap = min(policy["operator_cap"], 1 if knee is None else integer(knee))
    require(telemetry["live_count"] < cap, "certified concurrency", 4)
    require(telemetry["startup_active"] < policy["startup_cap"], "startup permit", 4)
    return {"concurrency_cap": cap, "cpu_period_us": policy["cpu_period_us"]}


def direct_owner_charge(owners, cut):
    require(len(set(cut)) == len(cut), "duplicate owner", 2)
    for owner in cut:
        parent, seen = owners[owner]["parent"], {owner}
        while parent is not None:
            require(parent not in seen, "ownership cycle", 2)
            require(parent not in cut, "ancestor and child counted together", 2)
            seen.add(parent)
            parent = owners[parent]["parent"]
    return total(*(owners[owner]["bytes"] for owner in cut))


def qualified_channel(profile, request, registry):
    """Synthetic qualification attestation, not real daemon or VMM evidence."""
    require(profile["qualified"] is True and profile["vmm"] == "firecracker" and
            profile["control"] in {"vsock_uds", "serial"}, "unqualified VMM/channel", 4)
    for field in ("vmm_digest", "kernel_digest", "rootfs_digest", "base_digest", "cache_digest", "exec_digest", "relay_digest"):
        require(type(profile.get(field)) is str and re.fullmatch(r"[0-9a-f]{64}", profile[field]), "missing approved pin", 4)
    require(profile["daemon"] in {"rootless", "dedicated_user"}, "daemon", 4)
    if profile["inner_count"] > 1:
        require(profile["child_limits"] is True and profile["membership_before_run"] is True and
                profile["child_only_stop"] is True, "child enforcement absent", 4)
    require(set(request) == {"verb", "mode", "target_id", "generation", "length", "timeout_ms"}, "closed relay")
    require(request["verb"] == "status" and request["mode"] in {"status", "control", "ui"}, "closed relay mode")
    require(integer(request["length"]) <= 102400 and 0 < integer(request["timeout_ms"]) <= 1000, "relay budget")
    binding = registry.get(request["target_id"])
    require(binding is not None and binding["generation"] == request["generation"], "relay ownership")
    require(binding["socket"].startswith("/dev/shm/") and ".." not in binding["socket"].split("/"), "private RAM socket")
    return binding["socket"]


class PressureGate:
    def __init__(self):
        self.open, self.clear = True, 0

    def sample(self, bp):
        integer(bp)
        if bp >= 2000:  # [TARGET] high threshold; no admission on equality.
            self.open, self.clear = False, 0
        elif not self.open:
            self.clear = self.clear + 1 if bp <= 500 else 0
            if self.clear >= 3:  # [TARGET] consecutive recovery observations.
                self.open = True
        return self.open


class StartupPermits:
    def __init__(self, snapshot=None):
        self.db = copy.deepcopy(snapshot) if snapshot is not None else {"held": [], "acks": {}}

    def acquire(self, lease, launch_id):
        owner = [lease, launch_id]
        if owner in self.db["held"]:
            return
        require(len(self.db["held"]) < 1, "startup permit exhausted", 4)
        self.db["held"].append(owner)

    def ready(self, lease, launch_id, ack_id, *, backend_ready, profile, cdp_ready):
        body = [lease, launch_id, backend_ready, profile, cdp_ready]
        if ack_id in self.db["acks"]:
            require(self.db["acks"][ack_id] == body, "ready ACK conflict", 2)
            return False
        require(profile in {"worker", "worker_ui"} and backend_ready is True, "backend not ready", 4)
        owner = [lease, launch_id]
        require(owner in self.db["held"], "ready wrong startup owner", 2)
        self.db["held"].remove(owner)
        self.db["acks"][ack_id] = body
        return True


class ControlPlane:
    """Copy-on-commit model of a shared Go transaction over existing OS services."""
    def __init__(self, snapshot=None):
        self.db = copy.deepcopy(snapshot) if snapshot is not None else {
            "records": {}, "requests": {}, "epochs": {}, "generations": {}, "active": {}, "chains": {}, "chain_halts": {},
            "cohorts": {}, "released": [], "stops": {}, "actions": {}, "pins": {},
            "ram_reserved": 0, "cpu_reserved": 0, "cost_reserved": 0, "cost_spent": 0,
            "startup_held": 0, "terminal_slots": 0, "cost_ledger": CostBook().snapshot()}
        self.capabilities = {}  # Ephemeral, never a record, snapshot, receipt or guest input.
        self.now_ms = 0

    def snapshot(self):
        return json.loads(canonical(self.db))

    def transact(self, change, crash=None):
        pending = copy.deepcopy(self.db)
        result = change(pending)
        if crash == "before_commit":
            raise Crash("before commit")
        self.db = pending
        if crash == "after_commit":
            raise Crash("after commit before reply")
        return copy.deepcopy(result)

    def admit(self, request, crash=None):
        minted = []
        def change(db):
            request_hash, key = digest(request), request["request_id"]
            if key in db["requests"]:
                old = db["requests"][key]
                require(old["hash"] == request_hash, "idempotency payload conflict", 2)
                return db["records"][old["lease_id"]]
            for field in ("ram", "outer_ram", "outer_overhead", "cpu", "cost", "generation", "expiry_ms"):
                integer(request[field])
            require(request["generation"] > 0 and self.now_ms < request["expiry_ms"], "admission lifetime", 2)
            target = request["target_id"]
            count = db["chains"].get(request["chain_id"], 0)
            require(request["chain_id"] not in db["chain_halts"], "absorbing chain halt", 4)
            require(count < 3, "durable attempt cap", 4)
            require(not any(row["attempt_id"] == request["attempt_id"] and row["chain_id"] == request["chain_id"]
                            for row in db["records"].values()),
                    "attempt identity reused", 2)
            require(target not in db["active"], "destination already owned", 4)
            require(request["generation"] > db["generations"].get(target, 0), "instance generation reused", 2)
            require(db["startup_held"] < 1 and db["terminal_slots"] < 32, "admission permits", 4)
            cohort = request["cohort_id"]
            outer = request["outer_ram"] if cohort not in db["cohorts"] else 0
            if cohort in db["cohorts"]:
                require(not db["cohorts"][cohort]["released"] and
                        db["cohorts"][cohort]["ram"] == request["outer_ram"] and
                        db["cohorts"][cohort]["overhead"] == request["outer_overhead"], "cohort incarnation changed", 2)
            allocated = db["cohorts"].get(cohort, {}).get("child_reserved", 0)
            require(total(request["outer_overhead"], allocated, request["ram"]) <= request["outer_ram"],
                    "outer suballocation budget", 4)
            require(total(db["ram_reserved"], outer) <= 100, "RAM budget", 4)
            require(total(db["cpu_reserved"], request["cpu"]) <= 8, "CPU budget", 4)
            # Stage cost admission in this same transaction, never in a prior commit.
            costs = CostBook(snapshot=db["cost_ledger"])
            cost_owner = digest(["w8-cost-attempt", request["chain_id"], request["attempt_id"]])
            request_cap = costs.admit_attempt(cost_owner, request["cost_policy"])
            require(cost_product(request_cap, request["cost_policy"]["max_requests"]) == request["cost"],
                    "cost reservation must match pinned capability", 2)
            db["cost_ledger"] = costs.snapshot()
            epoch = total(db["epochs"].get(target, 0), 1)
            lease = "l-" + digest([request["attempt_id"], target, epoch])
            tx = "tx-" + request_hash
            row = dict(request, lease_id=lease, epoch=epoch, admission_tx=tx, state="ADMITTED", restarts=count,
                       resource_owner="owner-" + digest([lease, request["generation"]]),
                       launch_id="launch-" + digest([tx, "launch"]), launch_claimed=False,
                       runtime=None, release_key=None, receipt=None, uncertain=False,
                       cost_settled=False, cost_owner=cost_owner, outcome=None, pins=[], startup_returned=False)
            db["records"][lease] = row
            db["requests"][key] = {"hash": request_hash, "lease_id": lease}
            db["epochs"][target], db["active"][target] = epoch, lease
            db["generations"][target] = request["generation"]
            db["chains"][request["chain_id"]] = count + 1
            db["ram_reserved"] += outer
            db["cpu_reserved"] += request["cpu"]
            db["cost_reserved"] = total(costs.pools()["held_unissued"], costs.pools()["outstanding_issued"])
            db["cost_spent"] = costs.pools()["spent"]
            db["startup_held"] += 1
            db["terminal_slots"] += 1
            cohort_row = db["cohorts"].setdefault(cohort, {"ram": outer, "overhead": request["outer_overhead"],
                                                        "children": [], "child_reserved": 0, "released": False})
            cohort_row["children"].append(lease)
            cohort_row["child_reserved"] += request["ram"]
            row["release_id"] = digest(["w8-release", tx, row["resource_owner"], request["generation"]])
            minted.append(lease)
            return row
        result = self.transact(change, crash)
        for lease in minted:
            self.capabilities[lease] = object()
        return result

    def halt_chain(self, chain_id, reason):
        def change(db):
            require(reason in {"HALT_CAP", "HALT_POISON", "HALT_OPERATOR"}, "halt reason", 2)
            require(chain_id in db["chains"], "unknown task chain", 2)
            require(chain_id not in db["chain_halts"] or db["chain_halts"][chain_id] == reason,
                    "immutable halt reason", 2)
            db["chain_halts"][chain_id] = reason
        self.transact(change)

    def projection(self, lease):
        row = self.db["records"][lease]
        result = {"schema": "os.cu.lease/v1", "lease_id": lease, "target_id": row["target_id"],
                  "target_kind": row["target_kind"], "state": row["state"],
                  "restarts": row["restarts"], "receipt": row["receipt"]}
        return validate_lease(result)

    def guard(self, db, lease, target, epoch):
        integer(epoch)
        require(lease in db["records"], "unknown lease", 2)
        row = db["records"][lease]
        require(row["target_id"] == target and db["active"].get(target) == lease and
                row["epoch"] == epoch == db["epochs"][target] and row["state"] in LIVE and
                integer(self.now_ms) < row["expiry_ms"],
                "stale destination epoch")
        require(lease in self.capabilities, "authority lost: reconcile before dispatch")
        return row

    def claim_launch(self, lease):
        def change(db):
            row = db["records"][lease]
            if row["launch_claimed"]:
                return {"launch_id": row["launch_id"], "execute": False}
            require(row["state"] == "ADMITTED", "launch state")
            self.guard(db, lease, row["target_id"], row["epoch"])
            row["state"], row["launch_claimed"] = "STARTING", True
            return {"launch_id": row["launch_id"], "execute": True}
        return self.transact(change)

    def recover_launch(self, lease, observation):
        rebound = []
        def change(db):
            row = db["records"][lease]
            require(row["launch_claimed"], "no durable launch claim", 2)
            require(row["state"] in {"STARTING", "RUNNING", "INDETERMINATE"}, "recovery state closed")
            require(db["active"].get(row["target_id"]) == lease and row["release_key"] is None,
                    "recovery wrong owner")
            stopped = any(stop["lease"] == lease for stop in db["stops"].values())
            valid = type(observation) is dict and all(observation.get(key) == row[key]
                    for key in ("launch_id", "target_id", "generation"))
            runtime_identity = ("boot_id", "pid", "start_time", "cgroup_id")
            valid = valid and all(type(observation.get(key)) is str and observation[key]
                                  for key in ("boot_id", "cgroup_id"))
            valid = valid and all(type(observation.get(key)) is int and 0 < observation[key] <= MAX_INT
                                  for key in ("pid", "start_time"))
            if row["runtime"] is not None:
                valid = valid and all(observation.get(key) == row["runtime"][key] for key in runtime_identity)
            uncertain_action = any(action["lease"] == lease and action["sent"] and not action["resolved"]
                                   for action in db["actions"].values())
            valid = valid and not stopped and not uncertain_action and self.now_ms < row["expiry_ms"]
            if (lease not in self.capabilities or not valid) and "recovery_epoch" not in row:
                row["recovery_epoch"] = total(db["epochs"][row["target_id"]], 1)
                db["epochs"][row["target_id"]] = row["recovery_epoch"]
            if not valid:
                row["state"], row["uncertain"] = "INDETERMINATE", True
            else:
                row["runtime"] = copy.deepcopy(observation)
                ready = observation.get("backend_ready") is True
                row["state"], row["uncertain"] = "RUNNING" if ready else "STARTING", False
                if "recovery_epoch" in row:
                    row["epoch"] = row.pop("recovery_epoch")
                if ready and not row["startup_returned"]:
                    row["startup_returned"] = True
                    db["startup_held"] -= 1
                rebound.append(lease)
            return row["state"]
        state = self.transact(change)
        for recovered in rebound:
            self.capabilities[recovered] = object()
        return state

    def action(self, lease, target, epoch, action_id, payload, sent=False):
        def change(db):
            self.guard(db, lease, target, epoch)
            key = digest([lease, target, epoch, action_id])
            if key in db["actions"]:
                old = db["actions"][key]
                require(old["payload"] == digest(payload), "action identity conflict", 2)
                return "INDETERMINATE" if old["sent"] else "ALREADY_RECORDED"
            db["actions"][key] = {"lease": lease, "payload": digest(payload), "sent": sent, "resolved": False}
            return "ISSUED" if sent else "INTENT"
        return self.transact(change)

    def stop(self, lease, stop_id, *, expected_epoch, binding_digest=None):
        def change(db):
            row = db["records"][lease]
            if stop_id in db["stops"]:
                old = db["stops"][stop_id]
                require((old["lease"], old["request_epoch"], old["binding_digest"]) ==
                        (lease, expected_epoch, binding_digest), "STOP identity conflict", 2)
                return db["stops"][stop_id]["fence_epoch"]
            require(db["active"].get(row["target_id"]) == lease, "STOP wrong owner")
            require(integer(expected_epoch) == db["epochs"][row["target_id"]], "STOP stale epoch")
            db["epochs"][row["target_id"]] = total(db["epochs"][row["target_id"]], 1)
            db["stops"][stop_id] = {"lease": lease, "request_epoch": expected_epoch,
                                   "binding_digest": binding_digest, "fence_epoch": db["epochs"][row["target_id"]]}
            uncertain = row["uncertain"] or any(a["lease"] == lease and a["sent"] and not a["resolved"]
                                                for a in db["actions"].values())
            row["uncertain"] = uncertain
            row["state"] = "INDETERMINATE" if uncertain else "STOPPING"
            return db["epochs"][row["target_id"]]
        return self.transact(change)

    def reconcile(self, lease, outcome, process_proof):
        def change(db):
            row = db["records"][lease]
            require(row["state"] == "INDETERMINATE", "reconciliation state")
            self.check_cleanup(row, process_proof)
            require(outcome in {"cancelled", "execution", "gate"}, "known reconciliation outcome", 2)
            row["uncertain"], row["outcome"], row["state"] = False, outcome, "STOPPING"
            for action in db["actions"].values():
                if action["lease"] == lease:
                    action["resolved"] = True
        self.transact(change)

    @staticmethod
    def check_cleanup(row, proof):
        require(proof["generation"] == row["generation"], "cleanup generation")
        runtime = row["runtime"]
        if runtime is not None:
            require(all(proof.get(key) == runtime[key] for key in ("boot_id", "pid", "start_time", "cgroup_id")),
                    "process identity")
        else:
            require(proof.get("never_started") is True, "launch absence not proven")
        for key in ("pids", "workspace_bytes", "relays", "guest_processes", "cache_readers"):
            require(type(proof.get(key)) is int and proof[key] == 0, "cleanup " + key)

    def release(self, lease, proof, crash=None):
        def change(db):
            row = db["records"][lease]
            key = row["release_id"]
            if key in db["released"]:
                return key
            require(row["state"] == "STOPPING" and not row["uncertain"], "quarantine retains capacity")
            require(not any(a["lease"] == lease and a["sent"] and not a["resolved"] for a in db["actions"].values()),
                    "unresolved action")
            self.check_cleanup(row, proof)
            db["cohorts"][row["cohort_id"]]["child_reserved"] -= row["ram"]
            db["cpu_reserved"] -= row["cpu"]
            if not row["startup_returned"]:
                row["startup_returned"] = True
                db["startup_held"] -= 1
            for key_pin in row["pins"]:
                db["pins"][key_pin] -= 1
            row["pins"] = []
            row["release_key"] = key
            db["released"].append(key)
            del db["active"][row["target_id"]]
            return key
        return self.transact(change, crash)

    def release_cohort(self, cohort, proof):
        def change(db):
            row = db["cohorts"][cohort]
            if row["released"]:
                return
            require(all(db["records"][lease]["release_key"] is not None for lease in row["children"]),
                    "live or quarantined sibling")
            require(proof == {"outer_empty": True, "tmpfs_empty": True, "relays_empty": True}, "outer proof")
            db["ram_reserved"] -= row["ram"]
            row["released"] = True
        self.transact(change)

    def finish(self, lease, rc, reason, passed=False):
        def change(db):
            row = db["records"][lease]
            require(row["receipt"] is None and row["state"] not in TERMINAL, "immutable terminal receipt")
            adapt_outcome(rc, reason, domain="w8", clean=row["release_key"] is not None,
                          started=row["runtime"] is not None, passed=passed)
            if rc == 6:
                row["state"], row["receipt"], row["uncertain"] = "INDETERMINATE", None, True
                return
            require(row["release_key"] is not None, "terminal cleanup not committed")
            row["state"] = "SUCCEEDED" if rc == 0 else "REFUSED" if rc in {2, 4, 7} and row["runtime"] is None else "FAILED"
            row["outcome"] = reason
            row["receipt"] = {"schema": "os.cu.worker-receipt/v1",
                              "sha256": digest([lease, row["attempt_id"], rc, reason])}
        self.transact(change)

    def pin_memory(self, lease, target, epoch, cache_digest, context_digest, base_generation):
        def change(db):
            row = self.guard(db, lease, target, epoch)
            require(base_generation == row["generation"], "memory base generation")
            require(all(re.fullmatch(r"[0-9a-f]{64}", item) for item in (cache_digest, context_digest)), "full memory digest", 2)
            key = digest([cache_digest, context_digest, base_generation])
            if key not in row["pins"]:
                row["pins"].append(key)
                db["pins"][key] = db["pins"].get(key, 0) + 1
            return key
        return self.transact(change)

    def publish_memory(self, lease, target, epoch, pin, base_generation):
        row = self.guard(self.db, lease, target, epoch)
        require(base_generation == row["generation"] and pin in row["pins"] and self.db["pins"].get(pin, 0) > 0,
                "unpinned or stale memory")
        return {"lease_id": lease, "target_id": target, "epoch": epoch, "pin": pin}


class FakeCDP:
    """TARGET ensure_tab contract: identities are host-registered, never arbitrary URLs."""
    def __init__(self, plane):
        self.plane, self.sessions, self.tabs, self.requests = plane, {}, {}, {}
        self.now_ms = 0
        self.lose_next_reply = False

    def register(self, session_id, boot_id, target, generation):
        self.sessions[session_id] = {"boot_id": boot_id, "target": target, "generation": generation}

    def ensure_tab(self, *, lease_id, target_id, epoch, session_id, tab_id=None, request_id, deadline_ms):
        row = self.plane.guard(self.plane.db, lease_id, target_id, epoch)
        require(self.now_ms < deadline_ms <= row["expiry_ms"], "tab deadline")
        session = self.sessions.get(session_id)
        require(session is not None and session["target"] == target_id and session["generation"] == row["generation"],
                "host-registered browser identity")
        body = [lease_id, target_id, epoch, session_id, tab_id, deadline_ms]
        if request_id in self.requests:
            previous = self.requests[request_id]
            require(previous["hash"] == digest(body), "ensure_tab identity conflict", 2)
            require(previous["binding"] is not None, "unknown create: inspect, never replay", 6)
            binding = previous["binding"]
            require(binding["boot_id"] == session["boot_id"] and binding["generation"] == row["generation"] and
                    binding["epoch"] == epoch and self.tabs.get(binding["tab_id"]) == binding,
                    "stale prior tab binding")
            return copy.deepcopy(previous["binding"])
        if tab_id is not None:
            require(tab_id in self.tabs and self.tabs[tab_id]["session_id"] == session_id and
                    self.tabs[tab_id]["boot_id"] == session["boot_id"] and
                    self.tabs[tab_id]["target_id"] == target_id and self.tabs[tab_id]["lease_id"] == lease_id and
                    self.tabs[tab_id]["epoch"] == epoch and self.tabs[tab_id]["generation"] == row["generation"], "foreign tab")
        else:
            existing = [tab for tab in self.tabs.values() if tab["target_id"] == target_id]
            require(len(existing) <= 1, "ambiguous tab identity", 6)
            if existing:
                bound = existing[0]
                require(all(bound[key] == value for key, value in {
                    "lease_id": lease_id, "epoch": epoch, "generation": row["generation"],
                    "session_id": session_id, "boot_id": session["boot_id"]}.items()), "old tab still owned", 6)
                tab_id = bound["tab_id"]
            else:
                tab_id = "tab-" + digest([lease_id, request_id, session["boot_id"]])
        binding = {"lease_id": lease_id, "target_id": target_id, "epoch": epoch,
                   "session_id": session_id, "tab_id": tab_id, "boot_id": session["boot_id"],
                   "generation": row["generation"]}
        self.tabs[tab_id] = binding
        self.requests[request_id] = {"hash": digest(body), "binding": binding}
        if self.lose_next_reply:
            self.lose_next_reply = False
            self.requests[request_id]["binding"] = None
            raise Refusal("creation response lost", 6)
        return copy.deepcopy(binding)

    def stop(self, binding, stop_id):
        require(type(binding) is dict and set(binding) ==
                {"lease_id", "target_id", "epoch", "session_id", "tab_id", "boot_id", "generation"},
                "closed tab binding", 2)
        if stop_id not in self.plane.db["stops"]:
            row = self.plane.guard(self.plane.db, binding["lease_id"], binding["target_id"], binding["epoch"])
            require(self.tabs.get(binding["tab_id"]) == binding and row["generation"] == binding["generation"],
                    "STOP stale tab identity")
            session = self.sessions.get(binding["session_id"])
            require(session is not None and session == {"boot_id": binding["boot_id"],
                    "target": binding["target_id"], "generation": binding["generation"]}, "STOP recycled browser")
        result = self.plane.stop(binding["lease_id"], stop_id, expected_epoch=binding["epoch"],
                                 binding_digest=digest(binding))
        if self.tabs.get(binding["tab_id"]) == binding:
            del self.tabs[binding["tab_id"]]
        return result

    def harvest(self, binding):
        self.plane.guard(self.plane.db, binding["lease_id"], binding["target_id"], binding["epoch"])
        require(self.tabs.get(binding["tab_id"]) == binding, "late tab generation")
        session = self.sessions.get(binding["session_id"])
        require(session is not None and session["boot_id"] == binding["boot_id"], "browser restarted")
        return "bounded-text"


class ReceiptCredits:
    def __init__(self, snapshot=None):
        state = copy.deepcopy(snapshot) if snapshot is not None else {
            "pending": {}, "inflight": {}, "acked": {}, "terminals": {},
            "reservations": {}, "stops": [], "ready_owners": []}
        self.__dict__.update(state)

    def snapshot(self):
        return copy.deepcopy(self.__dict__)

    def reserve_target(self, target, identity):
        require(len(identity) == 6 and identity[3] == target, "terminal reservation identity", 2)
        owner = digest(list(identity))
        if owner not in self.reservations:
            require(len(self.reservations) < 32, "terminal pool exhausted", 4)
            self.reservations[owner] = list(identity)
        return owner

    def offer(self, target, payload, lease_tuple, terminal=False):
        require(len(payload.encode("utf-8")) <= 102400, "bounded evidence")
        require(len(lease_tuple) == 7 and lease_tuple[3] == target and
                lease_tuple[6] == hashlib.sha256(payload.encode("utf-8")).hexdigest(), "payload identity/digest", 2)
        owner = digest(list(lease_tuple[:6]))
        receipt_id = digest(["w8-receipt"] + list(lease_tuple[:5]) + [lease_tuple[6]])
        item = {"owner": owner, "target": target, "receipt_id": receipt_id, "payload": payload,
                "tuple": list(lease_tuple), "terminal": terminal, "sent": False}
        if receipt_id in self.inflight:
            prior = dict(self.inflight[receipt_id], sent=False)
            require(prior == item, "receipt identity conflict", 2)
            return receipt_id
        if receipt_id in self.acked:
            require(self.acked[receipt_id] == {"tuple": list(lease_tuple), "terminal": terminal}, "acked receipt conflict", 2)
            return receipt_id
        require(self.reservations.get(owner) == list(lease_tuple[:6]), "unreserved terminal owner", 2)
        if terminal:
            require(owner not in self.terminals, "terminal conflict", 2)
            self.terminals[owner] = item
        else:
            require(owner not in self.terminals, "progress after terminal refused", 2)
            queue = self.pending.get(owner, [])
            require(len(queue) < 2 and sum(len(q) for q in self.pending.values()) < 32, "receipt credit exhausted", 4)
            self.pending.setdefault(owner, []).append(item)
        self.inflight[receipt_id] = item
        if owner not in self.ready_owners:
            self.ready_owners.append(owner)
        return receipt_id

    def next(self):
        if not self.ready_owners:
            return None
        owner = self.ready_owners.pop(0)
        self.ready_owners.append(owner)
        item = self.pending[owner][0] if self.pending.get(owner) else self.terminals[owner]
        item["sent"] = True
        return copy.deepcopy(item)

    def ack(self, receipt_id, lease_tuple, *, authenticated=True, consumer="verifier"):
        require(authenticated is True and consumer == "verifier", "unauthenticated ACK", 2)
        if receipt_id in self.acked:
            require(self.acked[receipt_id]["tuple"] == list(lease_tuple), "duplicate ACK mismatch", 2)
            return False
        require(receipt_id in self.inflight, "unknown ACK", 2)
        item = self.inflight[receipt_id]
        require(item["tuple"] == list(lease_tuple), "ACK tuple mismatch", 2)
        require(item["sent"] is True, "ACK before send", 2)
        if item["terminal"]:
            del self.terminals[item["owner"]]
            del self.reservations[item["owner"]]
        else:
            self.pending[item["owner"]].remove(item)
        self.acked[receipt_id] = {"tuple": item["tuple"], "terminal": item["terminal"]}
        del self.inflight[receipt_id]
        if not self.pending.get(item["owner"]) and item["owner"] not in self.terminals:
            self.ready_owners.remove(item["owner"])
        return True

    def stop(self, target):
        if target not in self.stops:
            self.stops.append(target)

    def conservation(self):
        pending_terminal = sum(item["sent"] for item in self.terminals.values())
        held_terminal = len(self.reservations) - pending_terminal
        free_terminal = 32 - len(self.reservations)
        data_items = [item for item in self.inflight.values() if not item["terminal"]]
        pending_data = sum(item["sent"] for item in data_items)
        held_data, free_data = len(data_items) - pending_data, 32 - len(data_items)
        return (len(self.inflight) == sum(len(q) for q in self.pending.values()) + len(self.terminals)
                and min(pending_terminal, held_terminal, free_terminal) >= 0
                and min(pending_data, held_data, free_data) >= 0
                and pending_terminal + held_terminal + free_terminal == 32
                and pending_data + held_data + free_data == 32)


def receipt_identity(target):
    return ["task-" + target, "a1", "lease-" + target, target, 1, 1]


def receipt_tuple(target, payload):
    return receipt_identity(target) + [hashlib.sha256(payload.encode("utf-8")).hexdigest()]


def deadlines(start_ms, ttl_ms, expiry_ms):
    end = min(total(start_ms, ttl_ms), integer(expiry_ms))
    require(end >= total(start_ms, 1000, 2000, 500), "insufficient finalization time", 4)
    return {"work_end": end - 3500, "collect_end": end - 2500,
            "stop_end": end - 500, "ack_end": end, "expiry": end}


def resume_deadlines(saved, *, now_ms, boot_id):
    require(boot_id == saved["boot_id"] and integer(now_ms) >= integer(saved["last_seen_ms"]),
            "clock identity requires fencing", 5)
    original = deadlines(saved["start_ms"], saved["ttl_ms"], saved["absolute_expiry_ms"])
    require(saved["bounds"] == original, "persisted deadline changed", 2)
    return {"bounds": original, "expired": now_ms >= original["expiry"],
            "start_new_work": now_ms < original["work_end"]}


def normalize_gate(raw_code, signal=False):
    require(type(raw_code) is int and -255 <= raw_code <= 255 and type(signal) is bool,
            "raw process status type/range", 2)
    return {"raw_code": raw_code, "signal": signal, "rc": 0 if raw_code == 0 and not signal else 5}


def adapt_outcome(rc, reason, *, domain, clean, started, uncertain=False, transient=False,
                  prerequisite_changed=False, passed=False, cost_known=True):
    reasons = {0: {None}, 2: {"contract"}, 3: {"setup", "execution"}, 4: {"unavailable"},
               5: {"gate", "timeout", "oom", "storage", "verification", "containment", "cancelled"},
               6: {"indeterminate"}, 7: {"operator_recipe", "operator_capability"}}
    require(domain == "w8" and type(rc) is int and rc in reasons and reason in reasons[rc], "W8 typed outcome", 2)
    require(all(type(flag) is bool for flag in
                (clean, started, uncertain, transient, prerequisite_changed, passed, cost_known)),
            "typed outcome evidence flags", 2)
    if uncertain:
        rc, reason = 6, "indeterminate"
    require(rc != 0 or (passed and clean and not uncertain), "success proof")
    retry = (rc == 3 and transient and clean) or (rc == 4 and prerequisite_changed and clean)
    return {"rc": rc, "reason": reason, "retry": retry, "release": clean and rc != 6,
            "cost_hold": not cost_known or rc == 6, "refund_spent": False,
            "local_receipt_allowed": not clean if rc == 6 else clean,
            "quota_consumed": started or rc == 6}


def cost_product(left, right):
    left, right = integer(left), integer(right)
    require(right == 0 or left <= MAX_INT // right, "cost product overflow", 2)
    return left * right

def cost_ceil(units, rate, denominator):
    require(integer(denominator) > 0, "positive rate denominator", 2)
    product = cost_product(units, rate)
    return total(product // denominator, int(product % denominator != 0))

def request_liability(policy):
    numeric = {"max_requests", "input_cap", "generation_cap", "input_rate",
               "output_rate", "denominator", "tool_cap", "cache_cap", "cancel_cap"}
    require(type(policy) is dict and set(policy) == numeric | {"adapter_sha256", "generation_semantics"},
            "adapter liability unbounded", 4)
    require(type(policy["adapter_sha256"]) is str and len(policy["adapter_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in policy["adapter_sha256"]), "adapter pin", 2)
    require(policy["generation_semantics"] == "total_including_reasoning", "unproved generation cap", 4)
    for name in numeric:
        integer(policy[name])
    require(policy["max_requests"] > 0, "finite positive request count", 4)
    return total(cost_ceil(policy["input_cap"], policy["input_rate"], policy["denominator"]),
                 cost_ceil(policy["generation_cap"], policy["output_rate"], policy["denominator"]),
                 policy["tool_cap"], policy["cache_cap"], policy["cancel_cap"])

def usage_totals(usage, policy):
    """The trusted adapter has already normalized provider-specific raw fields."""
    require(set(usage) == {"input_total", "cached_input", "generation_total", "reasoning_output"},
            "usage shape", 2)
    for value in usage.values():
        integer(value)
    require(usage["cached_input"] <= usage["input_total"] <= policy["input_cap"], "input usage cap", 2)
    require(usage["reasoning_output"] <= usage["generation_total"] <= policy["generation_cap"],
            "generation usage cap", 2)
    return usage["input_total"], usage["generation_total"]  # Subsets are never added twice.

class CostBook:
    """Reference atomic cost ledger: issue records intent; it does not call a provider.

    Every paid SDK retry must obtain a new issue slot. A duplicate issue returns
    False, so its caller must reconcile the existing intent rather than resend it.
    Finalization is pre-reserved and unavailable for ordinary request spending.
    """
    def __init__(self, budget=None, finalization=None, snapshot=None):
        if snapshot is None:
            self.db = {"budget": integer(1000 if budget is None else budget),
                       "finalization": integer(20 if finalization is None else finalization),
                       "spent": 0, "attempts": {}, "requests": {}, "charges": {}, "releases": {}}
        else:
            self.db = copy.deepcopy(snapshot)
            require(budget is None or budget == self.db["budget"], "budget changed on recovery", 2)
            require(finalization is None or finalization == self.db["finalization"], "tail changed on recovery", 2)
        self.check()

    def snapshot(self):
        return copy.deepcopy(self.db)

    def pools(self):
        return {"spent": self.db["spent"],
                "held_unissued": total(*(cost_product(a["unissued"], a["cap"]) for a in self.db["attempts"].values())),
                "outstanding_issued": total(*(r["cap"] for r in self.db["requests"].values() if r["state"] != "SETTLED")),
                "finalization": self.db["finalization"]}

    def check(self):
        require(total(*self.pools().values()) <= self.db["budget"], "wave cost cap", 4)

    def admit_attempt(self, attempt_id, policy):
        require(type(attempt_id) is str and bool(attempt_id), "attempt identity", 2)
        if attempt_id in self.db["attempts"]:
            old = self.db["attempts"][attempt_id]
            require(old["policy_digest"] == digest(policy), "attempt capability/rates changed", 2)
            return old["cap"]
        cap = request_liability(policy)
        held = cost_product(cap, policy["max_requests"])
        require(total(*self.pools().values(), held) <= self.db["budget"], "wave cost cap", 4)
        self.db["attempts"][attempt_id] = {"policy": copy.deepcopy(policy), "policy_digest": digest(policy),
                                          "cap": cap, "unissued": policy["max_requests"], "issued": 0, "closed": False}
        self.check()
        return cap

    def issue(self, request_id, attempt_id, body_digest):
        require(type(request_id) is str and bool(request_id), "request identity", 2)
        require(attempt_id in self.db["attempts"], "unadmitted attempt", 2)
        require(type(body_digest) is str and len(body_digest) == 64
                and all(c in "0123456789abcdef" for c in body_digest), "request digest", 2)
        if request_id in self.db["requests"]:
            row = self.db["requests"][request_id]
            require((row["attempt_id"], row["body_digest"]) == (attempt_id, body_digest), "request identity conflict", 2)
            return False
        attempt = self.db["attempts"][attempt_id]
        require(not attempt["closed"] and attempt["unissued"] > 0, "request count or closed attempt", 4)
        attempt["unissued"] -= 1
        attempt["issued"] += 1
        self.db["requests"][request_id] = {"attempt_id": attempt_id, "body_digest": body_digest, "cap": attempt["cap"],
                                          "state": "ISSUED", "charge_id": None, "actual": None}
        self.check()
        return True

    def cancel(self, request_id):
        require(request_id in self.db["requests"], "unknown request", 2)
        row = self.db["requests"][request_id]
        if row["state"] != "SETTLED":
            row["state"] = "CANCEL_PENDING"
        self.check()
        return 0  # An issued request is never refunded by transport cancellation.

    def close_unissued(self, attempt_id, release_id, *, no_dispatch_proved):
        require(type(release_id) is str and bool(release_id), "release identity", 2)
        require(attempt_id in self.db["attempts"] and no_dispatch_proved is True, "no-dispatch proof required", 2)
        if release_id in self.db["releases"]:
            require(self.db["releases"][release_id]["attempt_id"] == attempt_id, "release identity conflict", 2)
            return 0
        attempt = self.db["attempts"][attempt_id]
        require(not attempt["closed"], "attempt already closed by another key", 2)
        released = cost_product(attempt["unissued"], attempt["cap"])
        attempt["unissued"], attempt["closed"] = 0, True
        self.db["releases"][release_id] = {"attempt_id": attempt_id, "released": released}
        self.check()
        return released  # Issued rows and spent charges are deliberately untouched.

    def settle(self, request_id, charge_id, actual):
        require(request_id in self.db["requests"], "unknown request", 2)
        row = self.db["requests"][request_id]
        if actual is None:
            require(charge_id is None, "unknown charge has no verified identity", 2)
            return 0
        require(type(charge_id) is str and bool(charge_id), "charge identity", 2)
        require(integer(actual) <= row["cap"], "bill exceeds proven liability", 2)
        if row["state"] == "SETTLED":
            require((row["charge_id"], row["actual"]) == (charge_id, actual), "settlement conflict", 2)
            return 0
        require(charge_id not in self.db["charges"], "charge already belongs to another request", 2)
        spent = total(self.db["spent"], actual)
        self.db["spent"] = spent
        row.update(state="SETTLED", charge_id=charge_id, actual=actual)
        self.db["charges"][charge_id] = {"request_id": request_id, "actual": actual}
        self.check()
        return row["cap"] - actual


def simulated_knee(rows):
    phases = {"setup", "offline_cache_miss", "ingest", "gate", "extract", "purge", "delivery"}
    require(type(rows) is list and rows, "probe rows", 2)

    def product(*values):
        result = 1
        for value in values:
            value = integer(value)
            require(not value or result <= MAX_INT // value, "probe product overflow", 2)
            result *= value
        return result

    points, paid_cost, identity, previous_n = [], 0, None, 0
    for index, row in enumerate(rows):
        fields = {"kind", "concurrency", "mixture", "pins", "topology", "criteria",
                  "budget_ok", "safety_ok", "runs"}
        require(type(row) is dict and set(row) == fields, "closed probe row", 2)
        require(row["kind"] == "synthetic", "fixture provenance", 2)
        n = integer(row["concurrency"])
        require(2 <= n <= 16 and n > previous_n, "ordered qualified sweep range", 2)
        previous_n = n
        mix, pins, topo, criteria = (row[key] for key in ("mixture", "pins", "topology", "criteria"))
        require(type(mix) is dict and set(mix) == {"manifest", "sha256"}, "mixture manifest", 2)
        manifest = mix["manifest"]
        require(type(manifest) is dict and manifest and
                all(type(k) is str and k and type(v) is str and v for k, v in manifest.items()),
                "chain-to-task-class manifest", 2)
        require(mix["sha256"] == digest(manifest), "mixture digest", 2)
        require(type(pins) is dict and set(pins) == {"base", "cache", "guest", "host", "adapter"} and
                all(type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v) for v in pins.values()),
                "probe pins", 2)
        require(type(topo) is dict and set(topo) ==
                {"class", "qualified", "outer", "inner", "max_inner", "active_cap"}, "topology", 2)
        require(topo["class"] in {"one_vm_per_outer", "qualified_children"} and
                topo["qualified"] is True, "qualified topology", 2)
        outer, inner, max_inner, cap = (integer(topo[k]) for k in ("outer", "inner", "max_inner", "active_cap"))
        require(outer > 0 and 0 < inner <= max_inner and product(outer, inner) == n and
                (topo["class"] != "one_vm_per_outer" or inner == 1), "topology bounds", 2)
        require(type(criteria) is dict and set(criteria) == {"tail_growth_bp", "pressure_growth_bp"} and
                all(0 < integer(v) <= 10000 for v in criteria.values()), "preregistered criteria", 2)
        binding = (mix, pins, topo["class"], max_inner, cap, criteria)
        require(identity is None or binding == identity, "comparison identity changed", 2)
        identity = copy.deepcopy(binding)
        require(type(row["budget_ok"]) is bool and type(row["safety_ok"]) is bool, "safety evidence", 2)
        safe = row["budget_ok"] and row["safety_ok"]
        require(not safe or n <= cap, "admitted topology exceeds budget", 2)
        runs = row["runs"]
        require(type(runs) is list and (len(runs) == 3 if safe else len(runs) <= 3), "repeat count", 2)
        require(safe or index == len(rows) - 1, "continued after first unsafe point", 2)
        observations = []
        for run in runs:
            require(type(run) is dict and set(run) ==
                    {"elapsed_ms", "verified_chain_ids", "attempts", "phase_ms", "tail_ms", "pressure_bp"},
                    "closed repeated run", 2)
            elapsed = integer(run["elapsed_ms"])
            require(elapsed > 0, "positive duration", 2)
            require(type(run["phase_ms"]) is dict and set(run["phase_ms"]) == phases and
                    total(*run["phase_ms"].values()) <= elapsed, "typed complete phases", 2)
            tail, pressure = integer(run["tail_ms"]), integer(run["pressure_bp"])
            require(tail > 0 and pressure <= 10000, "tail and pressure range", 2)
            require(type(run["attempts"]) is list and type(run["verified_chain_ids"]) is list,
                    "attempt history and verified chains", 2)
            seen, successful = set(), set()
            for attempt in run["attempts"]:
                require(type(attempt) is dict and set(attempt) ==
                        {"attempt_id", "chain_id", "rc", "verified", "paid_cost"}, "closed attempt", 2)
                aid, chain = attempt["attempt_id"], attempt["chain_id"]
                require(type(aid) is str and aid and type(chain) is str and chain in manifest and (chain, aid) not in seen,
                        "attempt and mixture identity", 2)
                require(integer(attempt["rc"]) in {0, 2, 3, 4, 5, 6, 7} and
                        type(attempt["verified"]) is bool, "typed terminal outcome", 2)
                require(not attempt["verified"] or attempt["rc"] == 0, "verified failure", 2)
                seen.add((chain, aid))
                if attempt["verified"]:
                    successful.add(chain)
                paid_cost = total(paid_cost, attempt["paid_cost"])
            verified = run["verified_chain_ids"]
            require(all(type(chain) is str and chain in manifest for chain in verified) and
                    set(verified) == successful, "verified success/history mismatch", 2)
            observations.append((len(set(verified)), elapsed, tail, pressure))
        points.append((n, safe, observations))

    def outcome(status, candidate=None):
        return {"status": status, "candidate": candidate, "evidence": "TARGET_SYNTHETIC",
                "certified": False, "paid_cost": paid_cost, "history": copy.deepcopy(rows)}

    if not points[-1][1]:
        return outcome("safety_cap", points[-2][0]) if len(points) > 1 else outcome("insufficient_admissible_evidence")
    pairs = 0
    for lower, upper in zip(points, points[1:]):
        if any(s == 0 for s, _, _, _ in lower[2]):
            continue  # A zero baseline cannot support a percentage-gain claim.
        pairs += 1
        low_gain, agreement = [], []
        for (s0, t0, tail0, pressure0), (s1, t1, tail1, pressure1) in zip(lower[2], upper[2]):
            low_gain.append(product(100, s1, t0) < product(105, s0, t1))
            agreement.append(product(10000, tail1) >= product(total(10000, criteria["tail_growth_bp"]), tail0)
                             or pressure1 >= total(pressure0, criteria["pressure_growth_bp"]))
        if all(low_gain) and all(agreement):
            return outcome("knee_candidate", lower[0])
    return outcome("knee_not_observed" if pairs else "insufficient_admissible_evidence")

def knee_rows_fixture():
    manifest = {"chain-" + str(i): "headless" if i % 2 else "ui" for i in range(100)}
    rows = []
    for n, elapsed, tail, pressure in [(2, 1000, 400, 100), (4, 980, 450, 250)]:
        runs = []
        for repeat in range(3):
            attempts = [{"attempt_id": "a-" + str(i), "chain_id": "chain-" + str(i),
                         "rc": 0, "verified": True, "paid_cost": 2} for i in range(10)]
            attempts.append({"attempt_id": "failed-retry", "chain_id": "chain-0",
                             "rc": 3, "verified": False, "paid_cost": 7})
            runs.append({"elapsed_ms": elapsed, "verified_chain_ids": ["chain-" + str(i) for i in range(10)],
                         "attempts": attempts, "phase_ms": {p: 10 for p in
                         ("setup", "offline_cache_miss", "ingest", "gate", "extract", "purge", "delivery")},
                         "tail_ms": tail, "pressure_bp": pressure})
        rows.append({"kind": "synthetic", "concurrency": n,
                     "mixture": {"manifest": dict(manifest), "sha256": digest(manifest)},
                     "pins": {k: chr(97 + i) * 64 for i, k in enumerate(("base", "cache", "guest", "host", "adapter"))},
                     "topology": {"class": "one_vm_per_outer", "qualified": True, "outer": n,
                                  "inner": 1, "max_inner": 1, "active_cap": 16},
                     "criteria": {"tail_growth_bp": 1000, "pressure_growth_bp": 100},
                     "budget_ok": True, "safety_ok": True, "runs": runs})
    return rows


def request_fixture(destination="left", attempt="a1", chain="chain-a", generation=1):
    return {"request_id": "req-" + attempt, "chain_id": chain, "task_id": "task-a",
            "attempt_id": attempt, "target_id": target_id("host-a", "outer-a", destination),
            "target_kind": "microvm", "cohort_id": "outer-a", "generation": generation,
            "ram": 20, "outer_ram": 60, "outer_overhead": 10, "cpu": 1, "cost": 100, "expiry_ms": 10000,
            "cost_policy": {"adapter_sha256": "a" * 64, "generation_semantics": "total_including_reasoning",
                            "max_requests": 2, "input_cap": 20, "generation_cap": 10,
                            "input_rate": 1, "output_rate": 3, "denominator": 1,
                            "tool_cap": 0, "cache_cap": 0, "cancel_cap": 0}}


def runtime_fixture(row):
    return {key: row[key] for key in ("launch_id", "target_id", "generation")} | {
        "boot_id": "host-boot-a", "pid": 123, "start_time": 456, "cgroup_id": "cg-" + row["lease_id"], "backend_ready": True}


def cleanup_fixture(row):
    value = {"generation": row["generation"], "pids": 0, "workspace_bytes": 0,
             "relays": 0, "guest_processes": 0, "cache_readers": 0, "never_started": row["runtime"] is None}
    if row["runtime"] is not None:
        value.update({key: row["runtime"][key] for key in ("boot_id", "pid", "start_time", "cgroup_id")})
    return value


def governor_fixture():
    policy = {"cpu_period_us": 100000, "sample_max_age_ms": 1000, "startup_cap": 1,
              "psi_high_bp": 2000, "cpu_budget_milli": 4000, "emergency_bytes": 100,
              "host_floor_bytes": 100, "operator_cap": 8, "measured_knee": 4}
    telemetry = {"now_ms": 1000, "sample_ms": 900, "controllers_verified": True, "swap_zero": True,
                 "hierarchy_complete": True,
                 "psi_bp": 100, "effective_cpus": [1, 2, 3, 4], "interactive_cpus": [0],
                 "cpu_reserved_milli": 1000, "cpu_ancestors": [{"quota_us": 400000, "period_us": 100000}],
                 "memory_ancestors": [{"limit": 1000, "current": 400, "committed": 700,
                                        "pending": 100, "running_slack": 100}],
                 "host_pending": 100, "host_running_slack": 100, "host_available": 1000,
                 "live_count": 1, "startup_active": 0}
    return policy, telemetry, {"cpu_milli": 1000, "cpuset": [1], "ram_bytes": 200}


class C2LeaseContractTests(unittest.TestCase):
    def rejects(self, function, *args, rc=None, **kwargs):
        with self.assertRaises(Refusal) as caught:
            function(*args, **kwargs)
        if rc is not None:
            self.assertEqual(caught.exception.rc, rc)

    def running(self, request=None):
        plane = ControlPlane()
        row = plane.admit(request or request_fixture())
        plane.claim_launch(row["lease_id"])
        plane.recover_launch(row["lease_id"], runtime_fixture(row))
        return plane, plane.db["records"][row["lease_id"]]

    def test_public_lease_matches_unchanged_schema(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "os.cu.worker-task.v1.schema.json").read_text())
        declared = schema["$defs"]["scheduler_lease"]
        self.assertEqual(set(declared["properties"]), LEASE_FIELDS)
        self.assertEqual(set(declared["required"]), LEASE_FIELDS)
        self.assertIs(declared["additionalProperties"], False)
        self.assertEqual(set(declared["properties"]["state"]["enum"]), STATES)
        plane, row = self.running()
        good = plane.projection(row["lease_id"])
        for field in ("epoch", "capability", "owner", "launch_id"):
            with self.subTest(field=field):
                self.rejects(validate_lease, dict(good, **{field: "forbidden"}), rc=2)
        self.rejects(validate_lease, dict(good, state="SUCCEEDED"), rc=2)
        self.rejects(validate_lease, dict(good, receipt={"schema": "os.cu.worker-receipt/v1", "sha256": "a" * 64}), rc=2)

    def test_identity_is_canonical_and_not_a_capability(self):
        self.assertEqual(digest({"b": 2, "a": 1}), digest({"a": 1, "b": 2}))
        self.assertNotEqual(target_id("a", "b.c", "d"), target_id("a", "b", "c.d"))
        self.rejects(canonical, {"epoch": 1.0}, rc=2)
        self.rejects(integer, True, rc=2)
        self.rejects(total, MAX_INT, 1, rc=2)
        plane, row = self.running()
        plane.capabilities[row["lease_id"]] = object()
        self.assertNotIn("capabilities", canonical(plane.snapshot()).decode())

    def test_atomic_admission_crash_replay_and_payload_conflict(self):
        request = request_fixture()
        before = ControlPlane()
        with self.assertRaises(Crash):
            before.admit(request, "before_commit")
        self.assertEqual(before.db["records"], {})
        self.assertEqual(before.db["ram_reserved"], 0)
        self.assertEqual(before.db["cost_ledger"]["attempts"], {})
        after = ControlPlane()
        with self.assertRaises(Crash):
            after.admit(request, "after_commit")
        recovered = ControlPlane(after.snapshot())
        row = recovered.admit(request)
        self.assertEqual(len(recovered.db["records"]), 1)
        self.assertEqual((recovered.db["ram_reserved"], recovered.db["cpu_reserved"], recovered.db["cost_reserved"]), (60, 1, 100))
        self.assertEqual((recovered.db["startup_held"], recovered.db["terminal_slots"]), (1, 1))
        self.assertEqual(CostBook(snapshot=recovered.db["cost_ledger"]).pools()["held_unissued"], 100)
        bad_cap = ControlPlane()
        before_cap = bad_cap.snapshot()
        self.rejects(bad_cap.admit, dict(request, cost=99), rc=2)
        self.assertEqual(bad_cap.db, before_cap)
        self.assertEqual(recovered.db["active"][row["target_id"]], row["lease_id"])
        self.rejects(recovered.admit, dict(request, cost=101), rc=2)
        self.rejects(recovered.admit, dict(request, request_id="other", attempt_id="other"), rc=4)
        other = ControlPlane()
        first = other.admit(request)
        other.claim_launch(first["lease_id"])
        other.recover_launch(first["lease_id"], runtime_fixture(first))
        independent = request_fixture("right", "a1", "chain-independent")
        independent["request_id"] = "req-independent-a1"
        self.assertEqual(other.admit(independent)["attempt_id"], "a1")

    def test_unknown_launch_holds_and_matching_launch_is_adopted(self):
        plane = ControlPlane()
        row = plane.admit(request_fixture())
        self.assertTrue(plane.claim_launch(row["lease_id"])["execute"])
        recovered = ControlPlane(plane.snapshot())
        self.assertFalse(recovered.claim_launch(row["lease_id"])["execute"])
        self.assertEqual(recovered.recover_launch(row["lease_id"], None), "INDETERMINATE")
        self.assertEqual(recovered.db["ram_reserved"], 60)
        self.rejects(recovered.release, row["lease_id"], cleanup_fixture(row))
        self.assertEqual(recovered.recover_launch(row["lease_id"], runtime_fixture(row)), "RUNNING")
        self.assertFalse(recovered.claim_launch(row["lease_id"])["execute"])
        wrong = dict(runtime_fixture(row), generation=2)
        self.assertEqual(recovered.recover_launch(row["lease_id"], wrong), "INDETERMINATE")
        for change in ({"boot_id": "other-boot"}, {"pid": 124}, {"start_time": 457}, {"cgroup_id": "reused-cgroup"}):
            original, observed = self.running()
            restarted = ControlPlane(original.snapshot())
            old_runtime = copy.deepcopy(observed["runtime"])
            self.assertEqual(restarted.recover_launch(observed["lease_id"], dict(old_runtime, **change)), "INDETERMINATE")
            self.assertEqual(restarted.db["records"][observed["lease_id"]]["runtime"], old_runtime)
            self.assertEqual(restarted.db["ram_reserved"], 60)
        original, observed = self.running()
        original.stop(observed["lease_id"], "closed-stop", expected_epoch=observed["epoch"])
        closed = original.snapshot()
        self.rejects(original.recover_launch, observed["lease_id"], runtime_fixture(observed))
        self.assertEqual(original.snapshot(), closed)

    def test_destination_revoke_preserves_siblings_and_stale_actions_fail(self):
        plane, left = self.running()
        right = plane.admit(request_fixture("right", "b1", "chain-b"))
        sibling_before = canonical(plane.db["records"][right["lease_id"]])
        epoch = plane.stop(left["lease_id"], "stop-left", expected_epoch=left["epoch"])
        self.assertEqual(epoch, left["epoch"] + 1)
        self.assertEqual(plane.stop(left["lease_id"], "stop-left", expected_epoch=left["epoch"]), epoch)
        self.assertEqual(canonical(plane.db["records"][right["lease_id"]]), sibling_before)
        self.assertEqual(plane.db["epochs"][right["target_id"]], right["epoch"])
        self.rejects(plane.action, left["lease_id"], left["target_id"], left["epoch"], "action-a", {"verb": "gate"})
        self.assertEqual(plane.action(right["lease_id"], right["target_id"], right["epoch"], "action-b", {"verb": "status"}), "INTENT")
        self.rejects(plane.stop, right["lease_id"], "stop-left", rc=2, expected_epoch=right["epoch"])
        plane.now_ms = right["expiry_ms"]
        self.rejects(plane.action, right["lease_id"], right["target_id"], right["epoch"], "late", {"verb": "status"})

    def test_sent_action_is_not_replayed_and_quarantine_holds_pins(self):
        plane, row = self.running()
        pin = plane.pin_memory(row["lease_id"], row["target_id"], row["epoch"], "a" * 64, "b" * 64, 1)
        args = (row["lease_id"], row["target_id"], row["epoch"], "act", {"verb": "gate"})
        self.assertEqual(plane.action(*args, sent=True), "ISSUED")
        recovered = ControlPlane(plane.snapshot())
        self.rejects(recovered.action, *args, sent=True)
        self.assertEqual(recovered.recover_launch(row["lease_id"], runtime_fixture(row)), "INDETERMINATE")
        self.assertEqual(len(recovered.db["actions"]), 1)
        self.rejects(recovered.action, *args, sent=True)
        recovered.stop(row["lease_id"], "stop", expected_epoch=recovered.db["epochs"][row["target_id"]])
        self.assertEqual(recovered.projection(row["lease_id"])["state"], "INDETERMINATE")
        self.assertEqual(recovered.db["pins"][pin], 1)
        self.rejects(recovered.release, row["lease_id"], cleanup_fixture(recovered.db["records"][row["lease_id"]]))

    def test_release_survives_crash_pid_reuse_and_janitor_reentry(self):
        plane, row = self.running()
        plane.stop(row["lease_id"], "stop", expected_epoch=row["epoch"])
        proof = cleanup_fixture(row)
        for change in ({"start_time": 457}, {"boot_id": "reboot"}, {"generation": 2}, {"workspace_bytes": 1}, {"relays": 1}):
            self.rejects(plane.release, row["lease_id"], dict(proof, **change))
        with self.assertRaises(Crash):
            plane.release(row["lease_id"], proof, "after_commit")
        recovered = ControlPlane(plane.snapshot())
        before = recovered.snapshot()
        key = recovered.release(row["lease_id"], proof)
        self.assertEqual(recovered.snapshot(), before)
        self.assertEqual(key, digest(["w8-release", row["admission_tx"], row["resource_owner"], row["generation"]]))
        self.assertEqual(recovered.db["ram_reserved"], 60)

    def test_shared_outer_ownership_prevents_early_or_double_release(self):
        plane, left = self.running()
        before = plane.snapshot()
        understated = dict(request_fixture("right", "b1", "chain-b"), outer_overhead=0, ram=40)
        self.rejects(plane.admit, understated, rc=2)
        self.assertEqual(plane.snapshot(), before)
        self.rejects(plane.admit, dict(understated, outer_overhead=10), rc=4)
        self.assertEqual(plane.snapshot(), before)
        right = plane.admit(request_fixture("right", "b1", "chain-b"))
        self.assertEqual(plane.db["ram_reserved"], 60)
        plane.stop(left["lease_id"], "stop-left", expected_epoch=left["epoch"])
        plane.release(left["lease_id"], cleanup_fixture(left))
        outer_proof = {"outer_empty": True, "tmpfs_empty": True, "relays_empty": True}
        self.rejects(plane.release_cohort, "outer-a", outer_proof)
        self.assertEqual(plane.db["ram_reserved"], 60)
        plane.stop(right["lease_id"], "stop-right", expected_epoch=right["epoch"])
        plane.release(right["lease_id"], cleanup_fixture(right))
        plane.release_cohort("outer-a", outer_proof)
        plane.release_cohort("outer-a", outer_proof)
        self.assertEqual(plane.db["ram_reserved"], 0)

    def test_parent_owner_cut_does_not_count_descendants_twice(self):
        owners = {"outer": {"parent": None, "bytes": 60}, "child-a": {"parent": "outer", "bytes": 20},
                  "child-b": {"parent": "outer", "bytes": 20}, "cache": {"parent": None, "bytes": 10}}
        self.assertEqual(direct_owner_charge(owners, ["outer", "cache"]), 70)
        self.assertEqual(direct_owner_charge(owners, ["child-a", "child-b"]), 40)
        self.rejects(direct_owner_charge, owners, ["outer", "child-a"], rc=2)
        plane, left = self.running()
        right = plane.admit(request_fixture("right", "b1", "chain-b"))
        plane.claim_launch(right["lease_id"])
        plane.recover_launch(right["lease_id"], runtime_fixture(right))
        self.assertEqual(plane.db["ram_reserved"], 60)
        self.assertEqual(plane.db["cohorts"]["outer-a"]["child_reserved"], 40)
        self.rejects(plane.admit, request_fixture("third", "c1", "chain-c"), rc=4)
        self.assertEqual(plane.db["ram_reserved"], 60)

    def test_child_controller_qualification_and_closed_relay(self):
        profile = {"qualified": True, "vmm": "firecracker", "control": "vsock_uds", "daemon": "rootless",
                   "inner_count": 2, "child_limits": True, "membership_before_run": True, "child_only_stop": True}
        profile.update({name: "a" * 64 for name in ("vmm_digest", "kernel_digest", "rootfs_digest", "base_digest",
                                                   "cache_digest", "exec_digest", "relay_digest")})
        request = {"verb": "status", "mode": "ui", "target_id": "tgt-a", "generation": 1, "length": 100, "timeout_ms": 10}
        registry = {"tgt-a": {"generation": 1, "socket": "/dev/shm/outer-a/target-a/xpra.sock"}}
        self.assertEqual(qualified_channel(profile, request, registry), registry["tgt-a"]["socket"])
        for change in ({"qualified": False}, {"child_limits": False}, {"membership_before_run": False},
                       {"child_only_stop": False}, {"relay_digest": None}, {"vmm": "new-vmm"}):
            self.rejects(qualified_channel, dict(profile, **change), request, registry, rc=4)
        for change in ({"verb": "relay"}, {"mode": "shell"}, {"argv": ["sh"]}, {"path": "/host/socket"},
                       {"generation": 2}, {"length": 102401}, {"target_id": "sibling"}):
            self.rejects(qualified_channel, profile, dict(request, **change), registry)

    def test_reassignment_keeps_failed_attempt_and_new_epoch(self):
        plane, old = self.running()
        plane.stop(old["lease_id"], "duplicate-stop", expected_epoch=old["epoch"])
        plane.release(old["lease_id"], cleanup_fixture(old))
        plane.finish(old["lease_id"], 5, "cancelled")
        old_receipt = plane.projection(old["lease_id"])
        replacement = plane.admit(request_fixture(attempt="a2", generation=2))
        self.assertEqual(replacement["target_id"], old["target_id"])
        self.assertGreater(replacement["epoch"], old["epoch"])
        self.assertNotEqual(replacement["lease_id"], old["lease_id"])
        plane.stop(replacement["lease_id"], "end-replacement", expected_epoch=replacement["epoch"])
        plane.release(replacement["lease_id"], cleanup_fixture(replacement))
        plane.finish(replacement["lease_id"], 0, None, passed=True)
        self.assertEqual(plane.stop(old["lease_id"], "duplicate-stop", expected_epoch=old["epoch"]), old["epoch"] + 1)
        self.rejects(plane.finish, old["lease_id"], 6, "indeterminate")
        self.assertEqual(plane.db["records"][old["lease_id"]]["receipt"], old_receipt["receipt"])
        self.assertEqual(plane.db["records"][old["lease_id"]]["outcome"], "cancelled")
        self.assertEqual(plane.db["cost_reserved"], 200)

    def test_ensure_tab_identity_stop_and_late_harvest(self):
        plane, left = self.running()
        right = plane.admit(request_fixture("right", "b1", "chain-b"))
        cdp = FakeCDP(plane)
        cdp.register("session-left", "browser-boot-a", left["target_id"], 1)
        cdp.register("session-right", "browser-boot-a", right["target_id"], 1)
        args = dict(lease_id=left["lease_id"], target_id=left["target_id"], epoch=left["epoch"],
                    session_id="session-left", request_id="ensure-left", deadline_ms=9000)
        binding = cdp.ensure_tab(**args)
        self.assertEqual(cdp.ensure_tab(**args), binding)
        sibling = cdp.ensure_tab(lease_id=right["lease_id"], target_id=right["target_id"], epoch=right["epoch"],
                                 session_id="session-right", request_id="ensure-right", deadline_ms=9000)
        self.rejects(cdp.ensure_tab, **dict(args, request_id="wrong-tab", tab_id=sibling["tab_id"]))
        self.rejects(cdp.ensure_tab, **dict(args, request_id="wrong-session", session_id="arbitrary-endpoint"))
        self.rejects(cdp.ensure_tab, **dict(args, request_id="past-expiry", deadline_ms=10001))
        before = plane.snapshot()
        before_tabs = copy.deepcopy(cdp.tabs)
        self.rejects(cdp.stop, dict(binding, generation=2), "bad-STOP")
        self.assertEqual(plane.snapshot(), before)
        self.assertEqual(cdp.tabs, before_tabs)
        stopped_epoch = cdp.stop(binding, "STOP-tab")
        after_stop = plane.snapshot()
        self.assertEqual(cdp.stop(binding, "STOP-tab"), stopped_epoch)
        self.assertEqual(plane.snapshot(), after_stop)
        self.rejects(cdp.stop, dict(binding, epoch=binding["epoch"] + 1), "STOP-tab", rc=2)
        self.assertNotIn(binding["tab_id"], cdp.tabs)
        self.rejects(cdp.harvest, binding)
        self.assertEqual(cdp.harvest(sibling), "bounded-text")
        cdp.sessions["session-right"]["boot_id"] = "recycled-browser"
        self.rejects(cdp.harvest, sibling)
        self.rejects(cdp.ensure_tab, lease_id=right["lease_id"], target_id=right["target_id"], epoch=right["epoch"],
                     session_id="session-right", request_id="ensure-right", deadline_ms=9000)
        cdp.sessions["session-right"]["boot_id"] = "browser-boot-a"
        cdp.tabs[sibling["tab_id"]]["generation"] = 2
        self.rejects(cdp.ensure_tab, lease_id=right["lease_id"], target_id=right["target_id"], epoch=right["epoch"],
                     session_id="session-right", tab_id=sibling["tab_id"], request_id="reused-tab", deadline_ms=9000)
        other_plane, other = self.running()
        lost = FakeCDP(other_plane); lost.register("session", "boot", other["target_id"], 1)
        lost.lose_next_reply = True
        create = dict(lease_id=other["lease_id"], target_id=other["target_id"], epoch=other["epoch"],
                      session_id="session", request_id="create-lost", deadline_ms=9000)
        self.rejects(lost.ensure_tab, **create, rc=6)
        self.rejects(lost.ensure_tab, **create, rc=6)
        self.assertEqual(len(lost.tabs), 1)
        recovered_plane, recovered_row = self.running()
        stale = FakeCDP(recovered_plane)
        stale.register("session", "boot", recovered_row["target_id"], 1)
        stale_binding = stale.ensure_tab(lease_id=recovered_row["lease_id"], target_id=recovered_row["target_id"],
                                        epoch=recovered_row["epoch"], session_id="session", request_id="create", deadline_ms=9000)
        stale.plane = ControlPlane(recovered_plane.snapshot())
        self.assertEqual(stale.plane.recover_launch(recovered_row["lease_id"], runtime_fixture(recovered_row)), "RUNNING")
        before = stale.plane.snapshot()
        self.rejects(stale.stop, stale_binding, "old-epoch-STOP")
        self.assertEqual(stale.plane.snapshot(), before)
        self.assertEqual(stale.tabs[stale_binding["tab_id"]], stale_binding)

    def test_memory_publication_fenced_and_pins_released_once(self):
        plane, row = self.running()
        args = (row["lease_id"], row["target_id"], row["epoch"])
        pin = plane.pin_memory(*args, "a" * 64, "b" * 64, 1)
        self.assertEqual(plane.pin_memory(*args, "a" * 64, "b" * 64, 1), pin)
        self.assertEqual(plane.db["pins"][pin], 1)
        self.assertEqual(plane.publish_memory(*args, pin, 1)["epoch"], row["epoch"])
        self.rejects(plane.publish_memory, *args, pin, 2)
        plane.stop(row["lease_id"], "stop", expected_epoch=row["epoch"])
        self.rejects(plane.publish_memory, *args, pin, 1)
        plane.release(row["lease_id"], cleanup_fixture(row))
        plane.release(row["lease_id"], cleanup_fixture(row))
        self.assertEqual(plane.db["pins"][pin], 0)

    def test_a4_profile_good_and_must_fail_variants(self):
        good = hardened_profile()
        validate_profile(good)
        validate_profile(dict(good, phase="microvm", devices=["/dev/kvm"]))
        changes = [{"bind_mounts": ["/host:/workspace"]}, {"env_file": "/tmp/env"}, {"runtime_socket": True},
                   {"inference": "capsule"}, {"base_image": "repo@commit"}, {"workspace": "disk"},
                   {"extract": "docker-cp"}, {"text_only": False}, {"daemon": "root"}, {"read_only": False},
                   {"cap_drop": []}, {"memswap_bytes": 8192}, {"cpuset": [0]}, {"devices": ["/dev/kvm"]},
                   {"egress": "host_rules"}, {"frames_root": "/tmp"}, {"arbitrary_exec": "sh"}]
        for change in changes:
            with self.subTest(change=change):
                self.rejects(validate_profile, dict(good, **change))

    def test_ancestor_governor_avoids_double_count_and_checks_every_limit(self):
        policy, telemetry, request = governor_fixture()
        self.assertEqual(governor(policy, telemetry, request)["concurrency_cap"], 4)
        self.assertGreater(telemetry["memory_ancestors"][0]["current"] + telemetry["memory_ancestors"][0]["committed"] + request["ram_bytes"], 1000)
        for ancestor in ({"limit": 250, "current": 100, "committed": 100, "pending": 0, "running_slack": 0},
                         {"limit": 1000, "current": 900, "committed": 200, "pending": 0, "running_slack": 0}):
            bad = copy.deepcopy(telemetry); bad["memory_ancestors"].append(ancestor)
            self.rejects(governor, policy, bad, request, rc=4)
        for change in ({"sample_ms": 1001}, {"sample_ms": 0, "now_ms": 1001}, {"controllers_verified": False},
                       {"swap_zero": False}, {"startup_active": 1}, {"cpu_reserved_milli": 4000}, {"host_available": 100}):
            self.rejects(governor, policy, dict(telemetry, **change), request, rc=4)
        self.rejects(governor, policy, telemetry, dict(request, cpuset=[0]), rc=4)
        self.rejects(governor, dict(policy, measured_knee=None), telemetry, request, rc=4)
        self.assertEqual(governor(dict(policy, measured_knee=None), dict(telemetry, live_count=0), request)["concurrency_cap"], 1)

    def test_pressure_hysteresis_requires_clear_samples(self):
        gate = PressureGate()
        self.assertFalse(gate.sample(2000))
        self.assertFalse(gate.sample(500))
        self.assertFalse(gate.sample(501))
        self.assertFalse(gate.sample(500))
        self.assertFalse(gate.sample(499))
        self.assertTrue(gate.sample(500))

    def test_headless_readiness_and_startup_replay(self):
        permits = StartupPermits()
        permits.acquire("lease-a", "launch-a")
        recovered = StartupPermits(permits.db)
        self.rejects(recovered.acquire, "lease-b", "launch-b", rc=4)
        self.rejects(recovered.ready, "lease-a", "launch-a", "ready", backend_ready=False,
                     profile="worker", cdp_ready=True, rc=4)
        self.assertTrue(recovered.ready("lease-a", "launch-a", "ready", backend_ready=True,
                                        profile="worker", cdp_ready=False))
        self.assertFalse(recovered.ready("lease-a", "launch-a", "ready", backend_ready=True,
                                         profile="worker", cdp_ready=False))
        self.assertEqual(recovered.db["held"], [])
        recovered.acquire("lease-b", "launch-b")
        self.rejects(recovered.ready, "lease-a", "launch-a", "wrong", backend_ready=True,
                     profile="worker", cdp_ready=False, rc=2)
        policy, telemetry, request = governor_fixture()
        self.rejects(governor, policy, dict(telemetry, controllers_verified=False), request, rc=4)

    def test_receipt_credits_ack_fairness_and_independent_stop(self):
        credits = ReceiptCredits()
        ids = {}
        for target in ("left", "right"):
            credits.reserve_target(target, receipt_identity(target))
            for sequence in range(2):
                payload = target + str(sequence)
                ids[payload] = credits.offer(target, payload, receipt_tuple(target, payload))
        self.rejects(credits.offer, "left", "excess", receipt_tuple("left", "excess"), rc=4)
        credits.stop("left")
        terminal = credits.offer("left", "final-text", receipt_tuple("left", "final-text"), terminal=True)
        self.assertEqual(credits.stops, ["left"])
        self.assertEqual(len(credits.terminals), 1)
        self.rejects(credits.ack, terminal, receipt_tuple("left", "final-text"), rc=2)
        self.assertEqual(credits.next()["receipt_id"], ids["left0"])
        before = credits.snapshot()
        for bad in (receipt_tuple("right", "left0"), receipt_tuple("left", "wrong-digest"),
                    receipt_identity("left")[:4] + [2, 1, receipt_tuple("left", "left0")[-1]]):
            self.rejects(credits.ack, ids["left0"], bad, rc=2)
            self.assertEqual(credits.snapshot(), before)
        self.rejects(credits.ack, ids["left0"], receipt_tuple("left", "left0"), authenticated=False, rc=2)
        self.assertTrue(credits.ack(ids["left0"], receipt_tuple("left", "left0")))
        recovered = ReceiptCredits(credits.snapshot())
        self.assertFalse(recovered.ack(ids["left0"], receipt_tuple("left", "left0")))
        self.assertTrue(recovered.conservation())
        self.rejects(recovered.offer, "left", "left2", receipt_tuple("left", "left2"), rc=2)
        seen, delivered = [], []
        while recovered.inflight:
            item = recovered.next(); seen.append(item["target"]); delivered.append(item["receipt_id"])
            recovered.ack(item["receipt_id"], item["tuple"])
        self.assertEqual(set(seen[:2]), {"left", "right"})
        self.assertEqual(len(seen), 4)
        self.assertIn(terminal, delivered)
        self.assertGreater(delivered.index(terminal), delivered.index(ids["left1"]))
        self.assertTrue(recovered.conservation())
        self.rejects(recovered.offer, "left", "x" * 102401, receipt_tuple("left", "x" * 102401))
        full = ReceiptCredits()
        for index in range(32):
            target = "target-" + str(index)
            full.reserve_target(target, receipt_identity(target))
        self.rejects(full.reserve_target, "excess", receipt_identity("excess"), rc=4)
        full.stop("target-0")
        self.assertEqual(full.stops, ["target-0"])
        self.assertTrue(full.conservation())

    def test_single_expiry_bounds_collect_stop_ack_and_restart(self):
        bounds = deadlines(0, 10000, 9000)
        self.assertEqual(bounds, {"work_end": 5500, "collect_end": 6500, "stop_end": 8500, "ack_end": 9000, "expiry": 9000})
        self.assertEqual(deadlines(5000, 10000, 9000)["expiry"], 9000)
        self.rejects(deadlines, 6000, 10000, 9000, rc=4)
        saved = {"start_ms": 0, "ttl_ms": 8000, "absolute_expiry_ms": 20000,
                 "boot_id": "boot-a", "last_seen_ms": 1000, "bounds": deadlines(0, 8000, 20000)}
        resumed = resume_deadlines(saved, now_ms=5000, boot_id="boot-a")
        self.assertEqual(resumed["bounds"]["expiry"], 8000)
        self.assertFalse(resumed["start_new_work"])
        self.assertTrue(resume_deadlines(saved, now_ms=8000, boot_id="boot-a")["expired"])
        self.rejects(resume_deadlines, saved, now_ms=5000, boot_id="boot-b", rc=5)
        self.rejects(resume_deadlines, saved, now_ms=999, boot_id="boot-a", rc=5)
        changed = copy.deepcopy(saved)
        changed["bounds"]["expiry"] = 13000
        self.rejects(resume_deadlines, changed, now_ms=5000, boot_id="boot-a", rc=2)
        credits = ReceiptCredits()
        credits.reserve_target("a", receipt_identity("a"))
        credits.offer("a", "a0", receipt_tuple("a", "a0")); credits.offer("a", "a1", receipt_tuple("a", "a1"))
        credits.stop("a")
        self.assertEqual(credits.stops, ["a"])

    def test_w8_outcome_adapter_never_casts_motor_enum(self):
        self.assertTrue(adapt_outcome(0, None, clean=True, started=True, passed=True, domain="w8")["release"])
        self.rejects(adapt_outcome, 0, None, clean=True, started=True, passed=False, domain="w8")
        for rc, reason in [(2, "contract"), (5, "gate"), (5, "cancelled"), (5, "containment"),
                           (6, "indeterminate"), (7, "operator_recipe"), (7, "operator_capability")]:
            outcome = adapt_outcome(rc, reason, clean=rc != 6, started=True, domain="w8")
            self.assertFalse(outcome["retry"])
            self.assertFalse(outcome["refund_spent"])
        self.assertTrue(adapt_outcome(3, "execution", clean=True, started=True, transient=True, domain="w8")["retry"])
        self.assertFalse(adapt_outcome(3, "execution", clean=False, started=True, transient=True, domain="w8")["retry"])
        self.assertFalse(adapt_outcome(4, "unavailable", clean=True, started=False, domain="w8")["retry"])
        self.assertTrue(adapt_outcome(4, "unavailable", clean=True, started=False, prerequisite_changed=True, domain="w8")["retry"])
        self.rejects(adapt_outcome, 1, "gate", clean=True, started=True, rc=2, domain="w8")
        self.rejects(adapt_outcome, 6, "execution", clean=False, started=True, rc=2, domain="w8")
        self.rejects(adapt_outcome, 5, "gate", domain="motor", clean=True, started=True, rc=2)
        self.rejects(adapt_outcome, 5, "gate", domain="w8", clean=1, started=True, rc=2)
        self.assertFalse(adapt_outcome(3, "execution", domain="w8", clean=False,
                                      started=True)["local_receipt_allowed"])
        billing = adapt_outcome(5, "gate", clean=True, started=True, cost_known=False, domain="w8")
        self.assertEqual(billing["rc"], 5)
        self.assertTrue(billing["cost_hold"])
        for raw, signal in [(1, False), (2, False), (124, False), (9, True)]:
            self.assertEqual(normalize_gate(raw, signal), {"raw_code": raw, "signal": signal, "rc": 5})
        for raw in (True, 1.0, None, 256):
            self.rejects(normalize_gate, raw, rc=2)

    def test_unknown_execution_clean_observation_has_no_invalid_local_receipt(self):
        result = adapt_outcome(6, "indeterminate", clean=True, started=True, domain="w8")
        self.assertFalse(result["local_receipt_allowed"])
        plane, row = self.running()
        plane.finish(row["lease_id"], 6, "indeterminate")
        self.assertIsNone(plane.projection(row["lease_id"])["receipt"])
        self.assertEqual(plane.projection(row["lease_id"])["state"], "INDETERMINATE")

    def test_durable_chain_cap_survives_supervisor_death(self):
        plane = ControlPlane()
        for index in range(3):
            row = plane.admit(request_fixture(attempt="a" + str(index + 1), generation=index + 1))
            plane.stop(row["lease_id"], "stop-" + str(index), expected_epoch=row["epoch"])
            plane.release(row["lease_id"], cleanup_fixture(row))
            plane.finish(row["lease_id"], 3, "execution")
            plane = ControlPlane(plane.snapshot())
        self.assertEqual(plane.db["chains"]["chain-a"], 3)
        self.rejects(plane.admit, request_fixture(attempt="a4", generation=4), rc=4)
        poisoned, row = self.running()
        poisoned.stop(row["lease_id"], "poison-stop", expected_epoch=row["epoch"])
        poisoned.release(row["lease_id"], cleanup_fixture(row))
        poisoned.finish(row["lease_id"], 5, "containment")
        poisoned.halt_chain("chain-a", "HALT_POISON")
        poisoned.halt_chain("chain-a", "HALT_POISON")
        restarted = ControlPlane(poisoned.snapshot())
        self.assertEqual(restarted.db["chains"]["chain-a"], 1)
        self.rejects(restarted.admit, request_fixture(attempt="a2", generation=2), rc=4)

    def test_cost_liability_paid_retry_and_settlement_idempotency(self):
        policy = {"adapter_sha256": "a" * 64, "generation_semantics": "total_including_reasoning",
                  "max_requests": 2, "input_cap": 1000, "generation_cap": 100,
                  "input_rate": 1, "output_rate": 3, "denominator": 4,
                  "tool_cap": 40, "cache_cap": 25, "cancel_cap": 10}
        self.assertEqual(request_liability(policy), 400)
        self.assertEqual(cost_ceil(1, 1, 3), 1)
        book = CostBook()
        self.assertEqual(book.admit_attempt("attempt-a", policy), 400)
        self.assertEqual(book.pools(), {"spent": 0, "held_unissued": 800, "outstanding_issued": 0, "finalization": 20})
        self.assertEqual(book.admit_attempt("attempt-a", policy), 400)
        before = book.snapshot()
        self.rejects(book.admit_attempt, "attempt-a", dict(policy, max_requests=3), rc=2)
        self.rejects(book.admit_attempt, "attempt-a", dict(policy, input_rate=True), rc=2)
        self.rejects(book.admit_attempt, "attempt-b", dict(policy, max_requests=1), rc=4)
        self.assertEqual(book.db, before)  # All future calls are reserved before any issue.
        self.assertTrue(book.issue("request-a", "attempt-a", "b" * 64))
        self.assertFalse(book.issue("request-a", "attempt-a", "b" * 64))
        self.rejects(book.issue, "request-a", "attempt-a", "c" * 64, rc=2)
        self.assertEqual(book.pools()["held_unissued"], 400)
        self.assertEqual(book.pools()["outstanding_issued"], 400)
        book.cancel("request-a")
        self.assertEqual(book.settle("request-a", None, None), 0)
        self.assertEqual(book.pools()["outstanding_issued"], 400)
        self.rejects(book.close_unissued, "attempt-a", "release-a", no_dispatch_proved=False, rc=2)
        self.assertEqual(book.close_unissued("attempt-a", "release-a", no_dispatch_proved=True), 400)
        self.assertEqual(book.close_unissued("attempt-a", "release-a", no_dispatch_proved=True), 0)
        self.assertEqual(book.pools()["outstanding_issued"], 400)
        self.rejects(book.issue, "request-unused", "attempt-a", "d" * 64, rc=4)
        book = CostBook(snapshot=book.snapshot())
        self.assertFalse(book.issue("request-a", "attempt-a", "b" * 64))
        self.assertEqual(book.settle("request-a", "charge-a", 250), 150)
        self.assertEqual(book.settle("request-a", "charge-a", 250), 0)
        before = book.snapshot()
        self.rejects(book.settle, "request-a", "charge-a", 200, rc=2)
        self.rejects(book.settle, "request-a", "different-charge", 250, rc=2)
        self.assertEqual(book.db, before)
        book.admit_attempt("attempt-b", dict(policy, max_requests=1))
        book.issue("request-b", "attempt-b", "c" * 64)
        self.rejects(book.settle, "request-b", "charge-a", 250, rc=2)
        self.rejects(book.settle, "request-b", "charge-b", 401, rc=2)
        self.assertEqual(book.settle("request-b", "charge-b", 50), 350)
        self.assertEqual(book.db["spent"], 300)
        self.assertEqual(book.cancel("request-b"), 0)
        self.assertEqual(book.db["spent"], 300)
        self.rejects(CostBook, budget=2000, snapshot=book.snapshot(), rc=2)

        limited = CostBook(budget=820)
        limited.admit_attempt("retry-chain", policy)
        for name in ("original", "paid-sdk-retry"):
            self.assertTrue(limited.issue(name, "retry-chain", "d" * 64))
        self.rejects(limited.issue, "third-paid-call", "retry-chain", "d" * 64, rc=4)
        self.assertEqual(limited.pools()["outstanding_issued"], 800)
        self.assertEqual(limited.close_unissued("retry-chain", "release-b", no_dispatch_proved=True), 0)
        self.assertEqual(limited.pools()["outstanding_issued"], 800)
        recovered = CostBook(snapshot=limited.snapshot())
        self.rejects(recovered.issue, "after-restart", "retry-chain", "d" * 64, rc=4)
        zero = dict(policy, input_rate=0, output_rate=0, tool_cap=0, cache_cap=0, cancel_cap=0)
        free = CostBook(budget=0, finalization=0)
        self.assertEqual(free.admit_attempt("zero", zero), 0)
        free.issue("free-a", "zero", "e" * 64)
        self.assertEqual(free.settle("free-a", "zero-charge", 0), 0)
        free.issue("free-b", "zero", "e" * 64)
        self.rejects(free.issue, "free-c", "zero", "e" * 64, rc=4)
        self.rejects(request_liability, {}, rc=4)
        self.rejects(request_liability, dict(policy, denominator=0), rc=2)
        self.rejects(request_liability, dict(policy, generation_semantics="unknown"), rc=4)
        self.rejects(request_liability, dict(policy, input_cap=MAX_INT, input_rate=2), rc=2)
        self.rejects(CostBook().admit_attempt, "overflow", dict(policy, max_requests=MAX_INT), rc=2)
        usage = {"input_total": 1000, "cached_input": 800, "generation_total": 100, "reasoning_output": 60}
        self.assertEqual(usage_totals(usage, policy), (1000, 100))
        self.rejects(usage_totals, dict(usage, reasoning_output=101), policy, rc=2)
        self.rejects(usage_totals, dict(usage, cached_input=1001), policy, rc=2)

    def test_knee_fixture_is_simulation_not_a_host_measurement(self):
        rows = knee_rows_fixture()
        result = simulated_knee(rows)
        self.assertEqual((result["status"], result["candidate"]), ("knee_candidate", 2))
        self.assertEqual((result["evidence"], result["certified"], result["paid_cost"]), ("TARGET_SYNTHETIC", False, 162))
        self.assertEqual(result["history"], rows)
        scoped = copy.deepcopy(rows)
        scoped[0]["runs"][0]["attempts"][0]["attempt_id"] = "a1"
        scoped[0]["runs"][0]["attempts"][1]["attempt_id"] = "a1"
        self.assertEqual(simulated_knee(scoped)["candidate"], 2)
        scoped[0]["runs"][0]["attempts"].append(copy.deepcopy(scoped[0]["runs"][0]["attempts"][0]))
        self.rejects(simulated_knee, scoped, rc=2)
        duplicate = copy.deepcopy(rows)
        duplicate[1]["runs"][0]["verified_chain_ids"].append("chain-0")
        duplicate[1]["runs"][0]["attempts"].append({"attempt_id": "paid-duplicate", "chain_id": "chain-0",
                                                    "rc": 0, "verified": True, "paid_cost": 11})
        self.assertEqual(simulated_knee(duplicate)["candidate"], 2)
        self.assertEqual(simulated_knee(duplicate)["paid_cost"], 173)
        rising = copy.deepcopy(rows)
        for run in rising[1]["runs"]:
            run["elapsed_ms"] = 700
        self.assertEqual(simulated_knee(rising)["status"], "knee_not_observed")
        no_agreement = copy.deepcopy(rows)
        for run in no_agreement[1]["runs"]:
            run.update(tail_ms=400, pressure_bp=100)
        self.assertEqual(simulated_knee(no_agreement)["status"], "knee_not_observed")
        unsafe = copy.deepcopy(rows)
        unsafe[-1].update(budget_ok=False, safety_ok=False, runs=[])
        self.assertEqual((simulated_knee(unsafe)["status"], simulated_knee(unsafe)["candidate"]), ("safety_cap", 2))
        self.assertEqual(simulated_knee(rows[:1])["status"], "insufficient_admissible_evidence")
        for case in ("zero", "float", "overflow", "missing_mix", "changed_mix", "pins", "unqualified",
                     "range", "repeats", "phases", "budget", "claim_host"):
            bad = copy.deepcopy(rows)
            if case in {"zero", "float", "overflow"}:
                bad[0]["runs"][0]["elapsed_ms"] = {"zero": 0, "float": 1000.0, "overflow": MAX_INT}[case]
            elif case == "missing_mix": bad[0]["mixture"] = {}
            elif case == "changed_mix":
                bad[1]["mixture"]["manifest"]["chain-99"] = "different-class"
                bad[1]["mixture"]["sha256"] = digest(bad[1]["mixture"]["manifest"])
            elif case == "pins": bad[1]["pins"]["cache"] = "f" * 64
            elif case == "unqualified": bad[0]["topology"]["qualified"] = False
            elif case == "range": bad[1]["concurrency"] = 17
            elif case == "repeats": bad[1]["runs"].pop()
            elif case == "phases": del bad[0]["runs"][0]["phase_ms"]["ingest"]
            elif case == "budget": bad[1]["topology"]["active_cap"] = 2
            elif case == "claim_host": bad[0]["kind"] = "host_measured"
            with self.subTest(case=case), self.assertRaises(Refusal):
                simulated_knee(bad)


if __name__ == "__main__":
    if "--selftest-fail" in sys.argv:
        class DeliberateFailure(unittest.TestCase):
            def runTest(self):
                self.fail("deliberate failure: direct runner must return rc 5")
        result = unittest.TextTestRunner(verbosity=2).run(DeliberateFailure())
    else:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(C2LeaseContractTests))
    raise SystemExit(0 if result.wasSuccessful() else 5)
