"""Start the retention service in the foreground."""

import argparse
from contextlib import contextmanager
from datetime import timedelta
import errno
from importlib import import_module
import os
from pathlib import Path
import resource
import signal
import socket
import stat
import sys
from threading import Event

from ..contracts.validation import ContractError, ContractValidator, strict_json
from ..store import IntegrityError, Policy, StorageFailure
from ..store.files import Files
from ..transport.access import ACL, ComparisonGrant, Credential, Grant, Scope
from ..transport.routes import Unavailable
from ..transport.server import Approval, Server, effective_config
from ..transport.tls import Signer, TLSContext
from .backend import StoreBackend
from .local import FROZEN_DIR, load_local


def read_config(path):
    with Path(path).open("rb") as stream:
        data = stream.read((1 << 20) + 1)
    if len(data) > 1 << 20:
        raise ContractError("deployment input exceeds the configuration limit")
    return strict_json(data)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ContractError("unexpected deployment configuration fields")
    return value


def integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContractError("invalid deployment integer")
    return value


def absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
        raise ContractError("deployment requires absolute file paths")
    return Path(value).resolve()


def acl_loader(path, validator):
    def scope(doc):
        fields(doc, (), ("request_id", "attempt_id"))
        for key, schema in (("request_id", "Id"), ("attempt_id", "Attempt")):
            if key in doc:
                validator.validate(schema, doc[key])
        return Scope(**doc)

    def items(doc, key):
        if not isinstance(doc[key], list):
            raise ContractError("expected ACL list")
        return doc[key]

    def load():
        doc = fields(read_config(path), ("revision", "credentials", "grants", "comparisons"))
        validator.validate("Id", doc["revision"])
        credentials = []
        for item in items(doc, "credentials"):
            fields(item, ("fingerprint", "uri_san", "principal"))
            validator.validate("Hash", item["fingerprint"])
            validator.validate("Id", item["principal"])
            if (not isinstance(item["uri_san"], str) or not item["uri_san"].isascii()
                    or not item["uri_san"] or any(ord(char) < 33 or ord(char) > 126 for char in item["uri_san"])):
                raise ContractError("invalid client URI SAN")
            credentials.append(Credential(**item))
        principals = {entry.principal for entry in credentials}
        grants = []
        for item in items(doc, "grants"):
            fields(item, ("principal", "tenant", "verb", "scope"))
            for key, schema in (("principal", "Id"), ("tenant", "Id"), ("verb", "Verb")):
                validator.validate(schema, item[key])
            if item["principal"] not in principals:
                raise ContractError("grant has no configured certificate principal")
            grants.append(Grant(**{**item, "scope": scope(item["scope"])}))
        comparisons = []
        for item in items(doc, "comparisons"):
            fields(item, ("writer", "inspector", "tenant", "destination", "reference", "candidate"))
            for key in ("writer", "inspector", "tenant"):
                validator.validate("Id", item[key])
            if not {item["writer"], item["inspector"]} <= principals:
                raise ContractError("comparison has an unrecognized principal")
            scopes = {key: scope(item[key]) for key in ("destination", "reference", "candidate")}
            comparisons.append(ComparisonGrant(**{**item, **scopes}))
        return ACL(doc["revision"], tuple(credentials), tuple(grants), tuple(comparisons))

    def current():
        try:
            return load()
        except (OSError, ValueError, TypeError) as exc:
            raise Unavailable("current ACL is unavailable") from exc

    return current


def load_policy(revision, doc):
    sizes = ("object_bytes", "attempt_bytes", "tenant_bytes", "snapshot_bytes")
    seconds = ("retention", "purge_window", "snapshot_lifetime")
    fields(doc, (*sizes, *(key + "_seconds" for key in seconds)))
    values = {key: integer(doc[key], 64 << 30) for key in sizes}
    values.update({key: timedelta(seconds=integer(doc[key + "_seconds"], 30 * 86400)) for key in seconds})
    return Policy(revision, **values)


def remove_stale_socket(path, owner, *, private=False):
    """Recover an exclusively assigned endpoint while holding its deployment lock."""
    parent = path.parent.stat()
    if (parent.st_uid not in {0, os.geteuid()} or parent.st_mode & 0o022
            or (private and (parent.st_uid != os.geteuid() or parent.st_mode & 0o077))):
        raise ContractError("Unix listener requires a protected parent directory")
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISSOCK(entry.st_mode) or (entry.st_uid, entry.st_gid) != owner[:2]
            or (not private and stat.S_IMODE(entry.st_mode) != owner[2])):
        raise ContractError("occupied Unix endpoint differs from the approved socket")
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        try:
            probe.connect(os.fspath(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise
        else:
            raise ContractError("approved Unix endpoint is active")
    current = path.lstat()
    if (entry.st_dev, entry.st_ino) != (current.st_dev, current.st_ino):
        raise ContractError("Unix endpoint changed while checking it")
    path.unlink()


@contextmanager
def open_server(config, approval_doc, *, local_only=False, fault=None):
    fields(approval_doc, (
        "config", "unix_uid", "unix_gid", "unix_mode", "signer_uid", "library_sha256", "chain_sha256",
        "client_networks", "library", "frozen_dir", "acl_file", "witness_module", "policy",
    ))
    validator = ContractValidator(absolute(approval_doc["frozen_dir"]))
    if local_only and isinstance(config, dict):
        config = {**config, "local_only": True}
    config = effective_config(validator, config)
    approved = effective_config(validator, approval_doc["config"])
    if config != approved:
        raise ContractError("configuration differs from named approval")

    for key in ("library_sha256", "chain_sha256"):
        validator.validate("Hash", approval_doc[key])
    for key in ("unix_uid", "unix_gid", "signer_uid"):
        integer(approval_doc[key], (1 << 32) - 2)
    integer(approval_doc["unix_mode"], 0o770)
    networks = approval_doc["client_networks"]
    if not isinstance(networks, list) or not all(isinstance(value, str) for value in networks):
        raise ContractError("client networks must be an explicit list")
    approval = Approval(
        approved, approval_doc["unix_uid"], approval_doc["unix_gid"], approval_doc["unix_mode"],
        approval_doc["signer_uid"], approval_doc["library_sha256"], approval_doc["chain_sha256"],
        tuple(networks),
    )
    policy = load_policy(config["policy_revision"], approval_doc["policy"])
    get_acl = acl_loader(absolute(approval_doc["acl_file"]), validator)
    if get_acl().revision != config["acl_revision"]:
        raise ContractError("ACL revision differs from approval")
    witnesses = load_witnesses(approval_doc["witness_module"])
    signer = Signer(Path(config["signer_socket"]), approval.signer_uid, config["key_handle"])
    with TLSContext(
        absolute(approval_doc["library"]), config["public_chain"], config["trust_mount"],
        config["server_dns_name"], signer, server=True, library_sha256=approval.library_sha256,
        chain_sha256=approval.chain_sha256,
    ) as tls, StoreBackend(config["store_root"], validator, policy, witnesses=witnesses, fault=fault) as backend:
        server = Server(config=config, approval=approval, tls=tls, validator=validator,
                        acl=get_acl, backend=backend)
        backend.store.sweep()
        remove_stale_socket(Path(config["unix_socket"]), server.unix_owner)
        with server:
            yield server


def load_witnesses(witness_module):
    if not isinstance(witness_module, str) or not all(part.isidentifier() for part in witness_module.split(".")):
        raise ContractError("expected an installed witness module")
    witnesses = import_module(witness_module)
    if not all(callable(getattr(witnesses, name, None)) for name in ("witness", "verify_witness", "validate_profile")):
        raise ContractError("witness module is missing a required callback")
    return witnesses


@contextmanager
def open_local_server(*, frozen_dir=FROZEN_DIR, witness_module=None, fault=None):
    validator = ContractValidator(absolute(os.fspath(frozen_dir)))
    witnesses = load_witnesses(witness_module) if witness_module is not None else None
    deployment = load_local()
    config = {key: deployment.defaults[key] for key in ("policy_revision", "acl_revision")}
    policy = load_policy(config["policy_revision"], deployment.defaults["policy"])
    acl = deployment.acl()
    paths = {
        deployment.socket_path(role): endpoint["principal"]
        for role, endpoint in deployment.defaults["endpoints"].items()
    }
    # Separate the socket-directory lock from the durable store lock.
    endpoint_lock = Files(deployment.runtime, lambda *_: None)
    try:
        with StoreBackend(deployment.store, validator, policy, witnesses=witnesses, fault=fault) as backend:
            server = Server(config=config, local=paths, validator=validator, acl=lambda: acl, backend=backend)
            backend.store.sweep()
            for path in paths:
                remove_stale_socket(path, server.unix_owner, private=True)
            with server:
                yield server
    finally:
        endpoint_lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="explicit TLS deployment configuration")
    parser.add_argument("--approval", type=Path, help="named approval for the TLS deployment")
    parser.add_argument("--local-only", action="store_true", help="disable TCP for the TLS deployment")
    parser.add_argument("--frozen-dir", type=Path, help="override packaged frozen schemas in local-open")
    parser.add_argument("--witness-module", help="installed REA witness provider for local-open")
    args = parser.parse_args(argv)
    if (args.config is None) != (args.approval is None):
        parser.error("TLS opt-in requires both --config and --approval")
    if args.config is None and args.local_only:
        parser.error("--local-only is a TLS deployment option")
    if args.config is not None and (args.frozen_dir is not None or args.witness_module is not None):
        parser.error("TLS deployment inputs belong in the named approval")
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    _, hard = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
    try:
        if args.config is None:
            deployment = open_local_server(
                frozen_dir=args.frozen_dir or FROZEN_DIR, witness_module=args.witness_module,
            )
        else:
            deployment = open_server(read_config(args.config), read_config(args.approval), local_only=args.local_only)
        with deployment as server:
            elapsed = 0
            while not stopped.wait(1):
                if not server.healthy():
                    raise OSError("required retention listener is unavailable")
                elapsed += 1
                if elapsed >= 60:
                    server.backend.store.sweep()
                    elapsed = 0
    except (OSError, ValueError, ImportError, IntegrityError, StorageFailure, Unavailable):
        print("retention service failed startup or a required health check", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
