"""Project registered C1 capabilities onto mvctl verbs."""

from control.core.control import digest


TRANSPORT_FLAGS = (
    "--socket", "--session-id", "--agent-instance", "--core-instance",
    "--timeout-ms", "--max-line-bytes", "--max-reply-bytes",
)
REQUEST_FILE_FLAG = "--json-arg"


def registration_manifest(registry):
    entries = registry.entries()
    return {
        "registry_sha256": digest(entries),
        "required_flags": [*TRANSPORT_FLAGS, REQUEST_FILE_FLAG],
        "dry_run_flag": "--dry-run",
        "verbs": [
            {
                "verb": entry["cli"],
                "tool": entry["tool"],
                "json_op": entry["json_op"],
                "supported": entry["supported"],
                "operator_only": entry["operator_only"],
                "ops": entry["ops"],
                "op_table": [
                    {key: row[key] for key in ("op", "supported", "operator_only")}
                    for row in entry.get("op_table", ())
                ],
            }
            for entry in entries
        ],
    }
