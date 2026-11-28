# SPDX-License-Identifier: AGPL-3.0-only
"""Installed JSON entry point for the ViewPoint studio contract inventory."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT.parent.parent / "bin/viewpoint-studio"
RELEASE_SEAL_SHA256 = "c9c8e1d307c8c7126991e0acb77d3815e29e1f3172520fbdb01907a9391245d3"
MAX_JSON = 1024 * 1024


def unique_keys(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return value


def invalid_constant(_value):
    raise ValueError("non-finite JSON number")


def decode(raw):
    if len(raw) > MAX_JSON:
        raise ValueError("JSON document too large")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=invalid_constant)


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def installed_registry():
    seal_raw = (ROOT / "release-seal.json").read_bytes()
    if sha_bytes(seal_raw) != RELEASE_SEAL_SHA256:
        raise ValueError("release seal does not match the caller")
    seal = decode(seal_raw)
    if sha_bytes(LAUNCHER.read_bytes()) != seal["launcher_sha256"]:
        raise ValueError("launcher does not match the release seal")
    installed = set()
    for path in ROOT.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("unexpected entry in the installed payload")
        if path.is_file():
            installed.add(str(path.relative_to(ROOT)))
    if installed != set(seal["files"]) | {"studio.py", "release-seal.json"}:
        raise ValueError("installed payload file set does not match the release seal")
    for relative, expected in seal["files"].items():
        if sha_bytes((ROOT / relative).read_bytes()) != expected:
            raise ValueError("installed file does not match the release seal: " + relative)
    sys.path.insert(0, str(ROOT))
    from registry import inventory, mcp_manifest

    registry_raw = (ROOT / "registry.json").read_bytes()
    registry = decode(registry_raw)
    registry_sha256 = sha_bytes(registry_raw)
    manifest = decode((ROOT / "mcp-manifest.json").read_bytes())
    if (registry != inventory() or manifest != mcp_manifest(registry_sha256, registry["slots"])):
        raise ValueError("registry does not match installed contract and handler discovery")
    return registry, registry_sha256, manifest


def refusal(reason, retry_when, command, detail=None):
    response = {"reason": reason, "retry_when": retry_when, "next_command": command}
    if detail is not None:
        response["detail"] = detail
    return response


def emit(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")))


class Studio:
    def __init__(self, options, registry, registry_sha256, manifest):
        self.options = options
        self.registry = registry
        self.registry_sha256 = registry_sha256
        self.manifest = manifest

    def profile(self):
        from providers import Profile

        return Profile.load(self.options.profile, self.options.profile_sha256,
                            self.registry["slots"], decode)

    def capabilities(self):
        profile = self.profile()
        for provider in dict.fromkeys(profile.providers.values()):
            provider.probe(decode)
        slots = {}
        for name, entry in self.registry["slots"].items():
            provider = profile.providers.get(name)
            qualification = profile.qualification(name, self.registry_sha256, decode)
            slots[name] = {
                **entry,
                "declared": True,
                "dry_run": True,
                "runnable": provider is not None,
                "qualified": qualification is not None,
                "provider": provider.identity() if provider else None,
                "qualification": qualification,
                "absence_reasons": {
                    **({"provider": "AUSENTE — no provider configured"} if provider is None else {}),
                    **({"qualification": "AUSENTE — no approved qualification receipt"} if qualification is None else {}),
                },
            }
        return {
            "schema": "vp.studio.capabilities/v1",
            "registry_sha256": self.registry_sha256,
            "profile_id": profile.profile_id,
            "slots": slots,
            "factory_operations": self.registry["factory_operations"],
        }

    def validate_slot(self, record, name, kind):
        from asset_factory import contracts, core
        from providers import Refusal

        try:
            contracts.validate_slot(record, name, kind)
        except core.ContractError as exc:
            reason = "request_invalid" if kind == "request" else "provider_result_invalid"
            raise Refusal(reason, f"the {kind} matches the installed slot schema", exc.as_dict()) from exc

    def dry_run(self, request):
        name = request.get("slot") if isinstance(request, dict) else None
        self.validate_slot(request, name, "request")
        from registry import canonical

        return {"schema": "vp.studio.dry-run/v1", "slot": name, "request_id": request["request_id"],
                "registry_sha256": self.registry_sha256, "request_sha256": sha_bytes(canonical(request)),
                "status": "VALIDATED"}

    def run(self, request):
        from providers import Refusal
        from registry import sha256

        validation = self.dry_run(request)
        name = validation["slot"]
        profile = self.profile()
        provider = profile.providers.get(name)
        if provider is None:
            raise Refusal("provider_unbound", "an approved profile binds a provider to the requested slot")
        if request["provider_id"] != provider.provider_id or request["provider_revision"] != provider.revision:
            raise Refusal("provider_revision_mismatch", "the request names the provider and immutable installed revision")
        qualification = profile.qualification(name, self.registry_sha256, decode)
        if self.options.require_qualified and qualification is None:
            raise Refusal("qualification_absent", "an independent ACCEPT receipt is pinned by the approved profile")
        try:
            root = self.options.artifact_root.resolve(strict=True) if self.options.artifact_root is not None else None
            if root is None or not root.is_dir():
                raise ValueError("artifact root is required")
        except (OSError, ValueError, RuntimeError) as exc:
            raise Refusal("artifact_root_required", "an existing studio artifact directory is supplied") from exc

        def verify_artifacts(items, role):
            for item in items:
                if "path" not in item:
                    continue
                try:
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("nonrelative reference")
                    artifact = (root / relative).resolve(strict=True)
                    if not artifact.is_relative_to(root) or not artifact.is_file() or sha256(artifact) != item["sha256"]:
                        raise ValueError("artifact hash or root mismatch")
                except (OSError, RuntimeError, ValueError) as exc:
                    raise Refusal(role + "_artifact_mismatch",
                                  "artifact paths stay within the root and match their declared digests",
                                  {"role": item.get("role", item.get("schema"))}) from exc

        provider.probe(decode)
        verify_artifacts(request["inputs"], "input")
        result = provider.invoke("--run", decode, request=request, root=root)
        self.validate_slot(result, name, "result")
        if any(result[key] != request[key] for key in ("request_id", "provider_id", "provider_revision")):
            raise Refusal("provider_result_binding_mismatch", "the result identifies the submitted request and provider")
        if result["rc"] != 0:
            raise Refusal("provider_refused", "the provider completes the requested operation", {"provider_rc": result["rc"]})
        verify_artifacts(result["outputs"] + result["receipts"], "output")
        return {"schema": "vp.studio.run/v1", "registry_sha256": self.registry_sha256,
                "request_sha256": validation["request_sha256"], "provider": provider.identity(),
                "qualification": qualification, "result": result}

    def factory_dry_run(self, request):
        from providers import Refusal
        from viewpoint.handlers import LocalDryRun

        try:
            receipt = LocalDryRun(allowed_root=self.options.artifact_root).handle(request, dry_run=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise Refusal("request_invalid", "the legacy request and its artifact root are valid") from exc
        if not receipt["ok"]:
            raise Refusal(receipt["code"], "the legacy request is eligible for the installed dry-run handler", receipt)
        return receipt


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        emit(refusal("invalid_cli_arguments", "the command supplies a valid mode and options",
                     ["viewpoint-studio", "--help"], message))
        raise SystemExit(2)


def main():
    parser = JsonParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("list-capabilities", "dry-run", "run", "factory-dry-run", "mcp-manifest", "mcp"):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--profile-sha256")
    parser.add_argument("--expect-registry")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--require-qualified", action="store_true")
    options = parser.parse_args()
    command = ["viewpoint-studio", *sys.argv[1:]]
    if sys.version_info < (3, 10):
        emit(refusal("runtime_unusable", "Python 3.10 or newer is available on PATH", command))
        return 1
    try:
        registry, registry_sha256, manifest = installed_registry()
    except (OSError, ValueError, RuntimeError, ImportError, KeyError, RecursionError) as exc:
        emit(refusal("registry_stale", "the complete pinned contract release is reinstalled", command,
                     str(exc) if isinstance(exc, ValueError) else None))
        return 1
    if options.expect_registry is not None and options.expect_registry != registry_sha256:
        emit(refusal("registry_stale", "the installed inventory matches the harness release pin", command))
        return 1
    from providers import Refusal

    studio = Studio(options, registry, registry_sha256, manifest)
    try:
        if options.list_capabilities:
            result = studio.capabilities()
        elif options.mcp_manifest:
            result = manifest
        elif options.mcp:
            from mcp_stdio import serve

            return serve(studio, decode, refusal)
        else:
            try:
                request = decode(sys.stdin.buffer.read(MAX_JSON + 1))
            except (ValueError, RecursionError) as exc:
                raise Refusal("request_invalid", "stdin contains one bounded UTF-8 JSON request") from exc
            operation = studio.run if options.run else studio.factory_dry_run if options.factory_dry_run else studio.dry_run
            result = operation(request)
    except Refusal as exc:
        emit(refusal(exc.reason, exc.retry_when, command, exc.detail))
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
