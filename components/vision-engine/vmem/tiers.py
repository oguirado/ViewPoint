# @module {"id":"vm.tiers","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.errors"],"book":"HB-VC-s7-vmem"}
"""@module vm.tiers - L0/L1(/dev/shm)/L2 tiers, caps, FG growth state (SPEC_VMEM §2, §4). Stdlib only."""
import os
import shutil

from visioncore.errors import VqaError

MIB = 1 << 20
TIERS = ("L0", "L1", "L2")
L1_BASE = "/dev/shm"
# vmem may not import visioncore.params (g_layer); mirror of the R5 rows, pinned by tests/test_vmem_compact.py
DEFAULTS = {"L1_CAP_MIB": 512, "L2_CAP_MIB": 2048, "FG_WARN_PERMILLE": 250, "FG_BLOCK_PERMILLE": 900,
            "COMPACT_K_CLEAN": 5}
DIGEST_MAX_B = 6000


def l1_cap_bytes(table=None):
    return (table or DEFAULTS)["L1_CAP_MIB"] * MIB


def l2_cap_bytes(table=None):
    return (table or DEFAULTS)["L2_CAP_MIB"] * MIB


def fg_state(fg_bytes, table=None):  # @anchor:vm.tiers.fg_state
    """-> "OK" | "FG_WARN" | "FG_BLOCK" (integer permille of the L2 cap)."""
    t = table or DEFAULTS
    cap = l2_cap_bytes(t)
    if fg_bytes * 1000 > t["FG_BLOCK_PERMILLE"] * cap:
        return "FG_BLOCK"
    if fg_bytes * 1000 > t["FG_WARN_PERMILLE"] * cap:
        return "FG_WARN"
    return "OK"


def check_l2(total_bytes, table=None):
    """Ingest is never refused for FG; non-FG growth beyond the cap is rc 6."""
    if total_bytes > l2_cap_bytes(table):
        raise VqaError("E_BUDGET", {"reason": "l2_cap", "bytes": total_bytes})
    return True


def check_digest(data):
    if len(data) > DIGEST_MAX_B:
        raise VqaError("E_BUDGET", {"reason": "digest", "bytes": len(data)})
    return True


class L1Stage:
    """Volatile staging dir <base>/vmem_<run_id>; never acknowledged data (MEM §3.7). Cap enforced here."""

    def __init__(self, run_id, base=L1_BASE, cap_bytes=None):
        self.path = os.path.join(base, "vmem_" + run_id)
        self.cap = l1_cap_bytes() if cap_bytes is None else cap_bytes
        self.used = 0
        os.makedirs(self.path, exist_ok=True)

    def put(self, name, data):
        if "/" in name or name in ("", ".", ".."):
            raise VqaError("E_USAGE", {"name": name})
        if self.used + len(data) > self.cap:
            raise VqaError("E_BUDGET", {"reason": "l1_cap", "used": self.used, "add": len(data)})
        with open(os.path.join(self.path, name), "wb") as f:
            f.write(data)
        self.used += len(data)
        return os.path.join(self.path, name)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cleanup()
