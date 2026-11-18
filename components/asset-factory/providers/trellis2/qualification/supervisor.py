"""Host-only authority and device policy for one TRELLIS qualification job.

The issuer is scoped to the job's host supervisor. The legacy Docker issuer
continues to authenticate only peers from its owned inference container.
"""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import time

from asset_factory import core, proc
from providers.trellis2 import admission
from tools.qualification import qualify
from tools.qualification.gpu import AUTHORITY_SCHEMA, GpuLaunch, SCOPE_LIMITS
from tools.qualification.harness import DEFAULT_LIMITS
from tools.qualification.manifest import JSON_LIMIT, Pins, Refusal, pin_path, SHA256
from tools.qualification import offline
from tools.qualification.sandbox import GPU_DEVICE_PATHS, device_identity
from tools.scheduler import scheduler

DOCKER_SOCKET = "/run/docker.sock"
NVIDIA_SMI = Path("/usr/bin/nvidia-smi")
GPU_UUID = re.compile(r"GPU-[0-9a-f-]{36}", re.ASCII)


def require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


class DockerConnection(http.client.HTTPConnection):
    """Use a fixed local transport. No CLI config or registry credentials."""

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCKET)


def check_docker(cid, image_id, ceiling):
    require(type(cid) is str and SHA256.fullmatch(cid) is not None, 2, "runtime_container_unproven")
    connection = DockerConnection("localhost", timeout=3)
    try:
        connection.request("GET", "/containers/" + cid + "/json")
        response = connection.getresponse()
        require(response.status == 200, 3, "runtime_container_unproven")
        data = response.read(JSON_LIMIT + 1)
        require(len(data) <= JSON_LIMIT, 6, "docker_identity_budget")
        value = json.loads(data)
        host, state = value["HostConfig"], value["State"]
        require(value["Id"] == cid and value["Image"] == image_id and
                value["Name"].startswith("/af-") and state["Status"] == "created" and
                state["Running"] is False and state["Pid"] == 0 and
                host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True and
                host["Memory"] == host["MemorySwap"] == ceiling, 5, "runtime_container_unproven")
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, Refusal):
            raise
        raise Refusal(3, "runtime_container_unproven") from exc
    finally:
        connection.close()


def query_gpu(*options):
    """Run a GPU inventory query under the existing owned-process API."""
    job = None
    try:
        job = proc.popen([NVIDIA_SMI, *options], start_new_session=True, memory_limits=DEFAULT_LIMITS,
                         runtime_s=10, env={"PATH": os.defpath, "LC_ALL": "C"})
        returncode = job.wait(timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Refusal(3, "gpu_inventory_unavailable") from exc
    finally:
        if job is not None and proc.cleanup(job):
            raise Refusal(3, "gpu_inventory_cleanup_unproven")
    require(returncode == 0 and job.result is not None, 3, "gpu_inventory_unavailable")
    return job.stdout_path.read_text().strip()


def observe_idle_gpu(uuid):
    require(type(uuid) is str and GPU_UUID.fullmatch(uuid) is not None, 2, "gpu_uuid_invalid")
    require(query_gpu("--query-gpu=uuid", "--format=csv,noheader") == uuid, 4, "gpu_uuid_changed")
    active = query_gpu("--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader")
    require(not active, 5, "resident_present")
    return {"gpu_uuid": uuid, "compute_idle": True}


def private_runtime_dir():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    require(runtime.is_absolute() and runtime.resolve() == runtime,
            3, "runtime_authority_directory_unavailable")
    for directory in (runtime, runtime / "af-native"):
        if directory != runtime:
            directory.mkdir(mode=0o700, exist_ok=True)
        observed = directory.lstat()
        require(stat.S_ISDIR(observed.st_mode) and observed.st_uid == os.getuid() and
                not observed.st_mode & 0o077, 5, "runtime_authority_path_untrusted")
    return directory


def peer_matches(pid, scope):
    if pid != scope["pid"]:
        return False
    try:
        return offline.read_peer_identity(pid) == (scope["cgroup_path"], scope["start_ticks"])
    except (OSError, RuntimeError, ValueError, IndexError):
        return False


def verify_main_peer(job, scope, deadline):
    owned = proc._owned(job)
    reply = proc._unit_call(
        owned._runner, "show", owned.unit_name,
        "--property=MainPID,ControlGroup,ActiveState,SubState", deadline=deadline)
    state = dict(row.split("=", 1) for row in reply.stdout.splitlines() if "=" in row)
    require(reply.returncode == 0 and state.get("ActiveState") == "active" and
            state.get("SubState") == "running" and state.get("MainPID") == str(scope["pid"]) and
            state.get("ControlGroup") == scope["cgroup_path"] and peer_matches(scope["pid"], scope),
            5, "runtime_peer_unproven")


class PilotAuthority:
    """Issue and redeem a fresh permit after the owned unit proves its limits."""

    def __init__(self, *, candidate_sha256, container_id, input_sha256, seed, lease, gpu_uuid, timeout_s):
        self.candidate_sha256, self.container_id = candidate_sha256, container_id
        self.input_sha256, self.seed = input_sha256, seed
        self.lease, self.gpu_uuid, self.timeout_s = lease, gpu_uuid, timeout_s
        self.unit = None
        self.permit_sha256 = None

    def authorize(self, job, scope, folder, deadline):
        require(self.unit is None, 5, "permit_replayed")
        unit = job.unit_name
        offline.validate_gpu_scope(scope, unit)
        try:
            runtime = private_runtime_dir()
            verify_main_peer(job, scope, deadline)
            approval, approval_sha256 = admission._read(admission.approval_path().with_name("RUNTIME_APPROVAL_1117.json"), authority=True)
            binding = admission.bindings()
            admission.approved(approval, binding)
            require(approval["ceiling_bytes"] == SCOPE_LIMITS["maximum"] and
                    self.timeout_s <= approval["timeout_s"],
                    4, "runtime_operation_unapproved")
            require(self.lease.held(), 5, "lease_not_held")
            check_docker(self.container_id, binding["image_id"], approval["ceiling_bytes"])
            observe_idle_gpu(self.gpu_uuid)
            provider, _ = admission._read(admission.PROVIDER_PATH)
            params = {"seed": self.seed, "pipeline_type": "512"}
            receipt = scheduler.native_admission(
                provider, params, lease=self.lease, ceiling_bytes=approval["ceiling_bytes"],
                min_free_bytes=approval["min_free_bytes"], image_id=binding["image_id"],
                provider_path=admission.PROVIDER_PATH)
            require(receipt["rc"] == 0, receipt["rc"], "scheduler_admission_unavailable")
            permit = admission.make_permit(
                approval, approval_sha256, binding, seed=self.seed, pipeline_type="512",
                input_sha256=self.input_sha256, container_id=self.container_id, receipt=receipt)
            self.unit = unit
            permit_sha256 = core.sha256_json(permit)
            with tempfile.TemporaryDirectory(prefix="q-", dir=runtime) as directory:
                with admission.Authority(directory, permit, self.lease,
                                         lambda pid: peer_matches(pid, scope)) as issuer:
                    redemption_deadline = min(deadline, time.monotonic() + 5)
                    core.atomic_write_json(folder / "gpu-challenge.json", {
                        "socket": str(issuer.path), "permit_sha256": permit_sha256,
                    })
                    while not issuer.used and time.monotonic() < redemption_deadline:
                        time.sleep(0.01)
                    require(issuer.used and time.monotonic() < redemption_deadline,
                            5, "runtime_peer_unproven")
            verify_main_peer(job, scope, deadline)
            authorized_ns = time.monotonic_ns()
            expires_ns = receipt["admitted_monotonic_ns"] + offline.MAX_HANDOFF_AGE_NS
            require(receipt["admitted_monotonic_ns"] <= authorized_ns <= expires_ns,
                    4, "admission_stale")
            self.lease.arm()
            self.permit_sha256 = permit_sha256
        except admission.Refusal as exc:
            raise Refusal(exc.rc, exc.code) from exc
        except subprocess.TimeoutExpired as exc:
            raise Refusal(6, "candidate_time_budget") from exc
        except (core.ContractError, OSError, KeyError, TypeError) as exc:
            raise Refusal(3, "runtime_authority_unavailable") from exc
        return {
            "schema": AUTHORITY_SCHEMA, "candidate_sha256": self.candidate_sha256, "unit": unit,
            "permit_sha256": self.permit_sha256, "input_sha256": self.input_sha256, "seed": self.seed,
            "admitted_monotonic_ns": receipt["admitted_monotonic_ns"], "boot_id": receipt["boot_id"],
            "authorized_monotonic_ns": authorized_ns, "expires_monotonic_ns": expires_ns,
            "peer_pid": scope["pid"], "peer_start_ticks": scope["start_ticks"],
            "gpu_uuid": self.gpu_uuid,
        }, permit

    def release(self, unit):
        require(self.unit == unit and self.permit_sha256 is not None and self.lease.held(),
                3, "lease_release_unproven")
        try:
            observed = observe_idle_gpu(self.gpu_uuid)
            self.lease.release_verified()
        except admission.Refusal as exc:
            raise Refusal(exc.rc, exc.code) from exc
        return {"unit": unit, "permit_sha256": self.permit_sha256,
                "fence_removed": True, **observed}


def qualify_pilot(candidate, evidence_dir, *, timeout_s, container_id, gpu_uuid, devices):
    """Run one pilot against a stopped, explicitly pinned Docker reference."""
    require(type(candidate) is dict and candidate.get("provider") == "trellis2" and
            pin_path(candidate["code"]["entrypoint"]["path"]) == Path(__file__).resolve().with_name("adapter.py"),
            5, "qt_candidate_identity_invalid")
    try:
        observed = [device_identity(Path(name)) for name in GPU_DEVICE_PATHS]
    except OSError as exc:
        raise Refusal(3, "gpu_device_unavailable") from exc
    require(observed == devices, 4, "gpu_device_policy_invalid")
    pins = Pins()
    request = core.load_json(pins.check(candidate["request"]), max_bytes=JSON_LIMIT)
    image = Path(request["image"])
    try:
        with admission.Lease(admission.campaign_path(admission.LEASE_RELATIVE)) as lease:
            authority = PilotAuthority(
                candidate_sha256=core.sha256_json(candidate), container_id=container_id,
                input_sha256=core.sha256_file(image), seed=request["seed"],
                lease=lease, gpu_uuid=gpu_uuid, timeout_s=timeout_s)
            return qualify(candidate, evidence_dir, timeout_s=timeout_s, memory_limits=proc.MemoryLimits(**SCOPE_LIMITS),
                           gpu=GpuLaunch(devices, authority.authorize, authority.release, gpu_uuid))
    except admission.Refusal as exc:
        raise Refusal(exc.rc, exc.code) from exc
