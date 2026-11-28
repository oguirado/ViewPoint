# SPDX-License-Identifier: AGPL-3.0-only
"""JSON execution for the installed factory's local episode capability."""
import sys

from . import common as C
from . import episode

MAX_REQUEST = 65536
OPS = ("af.episode", "af.episode.status")
TRANSIENT = {"render_interrupted", "archive_ack_lost", "ram_admission_hold", "ram_checkpoint_hold"}


def capabilities():
    return [{"name": op, "handler": "viewpoint.episode_local.entry:main", "status": "local_execution",
             "args_schema": "viewpoint.episode-request/v1", "profile": "local-private-archive",
             "operator_only": False} for op in OPS]


def validate(request):
    episode.keys(request, "schema request_id op args", "invalid_request")
    C.require(request["schema"] == "viewpoint.episode-request/v1"
              and isinstance(request["request_id"], str) and C.IDENTIFIER.fullmatch(request["request_id"])
              and request["op"] in OPS, "invalid_request")
    episode.keys(request["args"], "recipe sha256", "invalid_request")
    episode.reference({"path": request["args"]["recipe"], "sha256": request["args"]["sha256"]})


def main(options, stdin, stdout):
    original_argv = ["viewpoint-studio", "--episode", *sys.argv[1:]]
    rc = 0
    try:
        C.require(all((options.artifact_root, options.episode_state_root, options.episode_policy,
                       options.episode_policy_sha256, options.episode_archive_root,
                       options.episode_restore_root)), "local_profile_unconfigured")
        assets, state, archive, restore = (path.resolve(strict=True) for path in
            (options.artifact_root, options.episode_state_root, options.episode_archive_root,
             options.episode_restore_root))
        roots = (assets, state, archive, restore)
        C.require(all(root.is_dir() for root in roots) and
                  not any(left.is_relative_to(right) or right.is_relative_to(left)
                          for index, left in enumerate(roots) for right in roots[index+1:]),
                  "unsafe_root_overlap")
        policy_path = options.episode_policy.resolve(strict=True)
        policy_bytes = policy_path.read_bytes()
        policy_sha = options.episode_policy_sha256
        C.require(C.digest(policy_bytes) == policy_sha, "policy_digest_mismatch")
        policy = C.decode(policy_bytes)
        source = getattr(stdin, "buffer", stdin)
        raw = source.read(MAX_REQUEST + 1)
        encoded = raw.encode() if isinstance(raw, str) else raw
        C.require(len(encoded) <= MAX_REQUEST, "invalid_request")
        request = C.decode(encoded)
        validate(request)
        result = episode.execute(state, archive, restore, assets, request, policy, policy_sha, False)
    except C.Refusal as exc:
        rc = 1
        result = {"reason": exc.reason, "detail": str(exc),
                  "retry_when": ("with the identical JSON request after the recorded transient clears"
                                 if exc.reason in TRANSIENT else "after correcting the rejected inputs"),
                  "next_command": original_argv}
    except (ValueError, TypeError, OSError, KeyError, RuntimeError) as exc:
        rc = 2
        result = {"reason": "harness_error", "detail": str(exc),
                  "retry_when": "after resolving the reported input or runtime error",
                  "next_command": original_argv}
    stdout.write(C.encoded(result).decode())
    return rc
