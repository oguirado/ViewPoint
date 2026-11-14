#!/usr/bin/env python3
"""viewpoint-ops: MCP stdio server that exposes the `vp` command line as tools.

Stdlib only. JSON-RPC 2.0 over newline-delimited stdin/stdout
(initialize, ping, tools/list, tools/call, resources/list,
resources/templates/list, resources/read, prompts/list, prompts/get,
notifications/progress).

Besides the tools the server publishes bounded resources (the AI docs and the
live state of a runtime root) and prompts (ready workflows); see README.md.

Every tool is one `vp <noun> <verb>` call. The server adds four things on top
of the command line and nothing else:

  1. the tool table comes from MCP_TOOLS.json (ops-design), not from code;
  2. arguments are checked against the tool's inputSchema before any process
     is started;
  3. a tool marked destructive is refused unless `confirm` is given or
     `dry_run` is true;
  4. the answer is bounded: the human text is cut to a byte ceiling, the full
     output is spooled to a file and the reply names the newton-py command
     that pages it.

Environment (all optional):
  VP_OPS_TOOLS       which tools tools/list shows: `core` (default) or `all`.
                     Every tool is callable in both modes.
  VP_OPS_TOOLS_PATH  path of MCP_TOOLS.json
  VP_OPS_CLI         path of the vp executable
  VP_OPS_SPOOL       spool directory (default $XDG_RUNTIME_DIR/vp-ops/spool)
  VP_OPS_MAX_BYTES   ceiling of the human text per reply (default 4096)
  VP_OPS_TIMEOUT_S   default time limit of one vp call (default 120)
  VP_OPS_NPY         path of the newton-py executable used in paging hints
  VP_OPS_EXTRAS      path of MCP_EXTRAS.json (examples, follow-up tools, prompts)
  VP_OPS_DOCS        directory of the AI docs (default ../docs)
  VP_OPS_RES_MAX_BYTES  ceiling of one resources/read reply (default 12288)
  VP_OPS_RT_BASE     directory holding named runtime roots (default /dev/shm/vqa-cu)
  VP_SWARM_RT        runtime root of the viewpoint://state/* resources
  VP_OPS_PROGRESS_S  seconds between progress notifications (default 2)
  VP_OPS_AUDIT       file that gets one JSON line per tools/call, resources/read
                     and prompts/get (what was asked, the receipt code); off when unset
"""
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_NAME = "viewpoint-ops"
SERVER_VERSION = "0.2.0"

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_INTERFACE = os.path.dirname(HERE)

# Workspace root holding the sibling lane worktrees before the merge; no absolute host path lives here.
WORKSPACE = os.environ.get("VP_OPS_ROOT") or os.path.normpath(os.path.join(AGENT_INTERFACE, "..", "..", ".."))

TOOLS_CANDIDATES = (
    os.path.join(AGENT_INTERFACE, "design", "MCP_TOOLS.json"),
    os.path.join(WORKSPACE, "vp-wt-fable-ai1", "product", "agent_interface", "design", "MCP_TOOLS.json"),
    os.path.join(HERE, "MCP_TOOLS.fallback.json"),
)
CLI_CANDIDATES = (
    os.path.join(AGENT_INTERFACE, "cli", "vp"),
    os.path.join(WORKSPACE, "vp-wt-fable-ai2", "product", "agent_interface", "cli", "vp"),
    os.path.join(HERE, "stub_vp"),
)
NPY_DEFAULT = os.path.join(os.path.expanduser("~"), ".local", "share", "newton-py", "venv", "bin", "newton")

# rc of the server-side refusals, inside the closed rc classes of vpops/1.1:
# 2 USAGE, 4 CONFIRM_REQUIRED, 8 DEPENDENCY, 9 UPSTREAM, 124 TIMEOUT.
RC_SCHEMA = 2
RC_CONFIRM = 4
RC_TIMEOUT = 124
RC_NO_CLI = 8
RC_NO_RECEIPT = 9

# Keys of a tool entry that the server reads but does not publish.
PUBLIC_KEYS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# Runtime roots no resource may read (the live demo lanes).
FORBIDDEN_RT = ("/dev/shm/vqa-cu/hybrid", "/dev/shm/vqa-cu/swarm")
RT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STATE_KINDS = ("queue-head", "last-frame", "events-tail", "microtasks")
URI = "viewpoint://"
RES_NOT_FOUND = -32002


class ResourceError(Exception):
    def __init__(self, message, code=RES_NOT_FOUND):
        Exception.__init__(self, message)
        self.code = code


def compact_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _first_existing(candidates, need_exec=False):
    for path in candidates:
        if path and os.path.isfile(path) and (not need_exec or os.access(path, os.X_OK)):
            return path
    return None


# ---------------------------------------------------------------- tool table

class ToolTableError(ValueError):
    pass


def _argv_of(entry):
    """Words placed after `vp` for this tool."""
    for key in ("argv", "command", "cli", "vp"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            words = value.split()
        elif isinstance(value, list) and value and all(isinstance(w, str) for w in value):
            words = list(value)
        else:
            continue
        if words and os.path.basename(words[0]) == "vp":
            words = words[1:]
        if words:
            return words
    name = entry["name"]
    stem = name[3:] if name.startswith("vp_") else name
    words = [w for w in re.split(r"[_.]", stem, maxsplit=1) if w]
    if len(words) == 2:
        words[1] = words[1].replace("_", "-")
    return words


def _is_destructive(entry):
    if isinstance(entry.get("destructive"), bool):
        return entry["destructive"]
    notes = entry.get("annotations")
    if isinstance(notes, dict) and isinstance(notes.get("destructiveHint"), bool):
        return notes["destructiveHint"]
    return False


def load_tools(path):
    """Read MCP_TOOLS.json. Returns (published tool list, private spec by name)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ToolTableError(f"cannot read tool table {path}: {exc}")
    entries = doc.get("tools") if isinstance(doc, dict) else doc
    if not isinstance(entries, list) or not entries:
        raise ToolTableError(f"tool table {path}: expected a non-empty list or {{\"tools\": [...]}}")
    published, specs = [], {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ToolTableError(f"tool table {path}: entry {i} has no name")
        name = entry["name"]
        if not TOOL_NAME.match(name):
            raise ToolTableError(f"tool table {path}: bad tool name {name!r}")
        if name in specs:
            raise ToolTableError(f"tool table {path}: duplicate tool {name}")
        schema = entry.get("inputSchema", entry.get("input_schema"))
        if schema is None:
            schema = {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise ToolTableError(f"tool table {path}: {name} inputSchema is not an object")
        positional = entry.get("positional") or []
        if not isinstance(positional, list) or not all(isinstance(p, str) for p in positional):
            raise ToolTableError(f"tool table {path}: {name} positional must be a list of names")
        destructive = _is_destructive(entry)
        specs[name] = {
            "argv": _argv_of(entry),
            "positional": positional,
            "destructive": destructive,
            "schema": schema,
            "timeout_s": entry.get("timeout_s"),
            "stdin": entry.get("stdin") if isinstance(entry.get("stdin"), str) else None,
        }
        public = {k: v for k, v in entry.items() if k in PUBLIC_KEYS}
        public["inputSchema"] = schema
        specs[name]["tier"] = entry.get("tier") if isinstance(entry.get("tier"), str) else "core"
        public.setdefault("description", "vp " + " ".join(specs[name]["argv"]))
        notes = dict(public.get("annotations") or {})
        notes.setdefault("destructiveHint", destructive)
        public["annotations"] = notes
        published.append(public)
    return published, specs


# ------------------------------------------------------------ schema checking

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate(value, schema, where="arguments"):
    """Subset of JSON Schema used by tool tables. Returns a list of problems."""
    problems = []
    if not isinstance(schema, dict):
        return problems
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES.get(k, lambda v: True)(value) for k in kinds):
            return [f"{where}: expected {'|'.join(kinds)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{where}: must equal {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    problems.append(f"{where}: does not match {schema['pattern']}")
            except re.error:
                pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where}: above maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                problems += validate(item, schema["items"], f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{where}: missing required '{key}'")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                problems += validate(item, props[key], f"{where}.{key}")
            elif extra is False:
                problems.append(f"{where}: unknown argument '{key}'")
            elif isinstance(extra, dict):
                problems += validate(item, extra, f"{where}.{key}")
    return problems


# -------------------------------------------------------------- argv building

def build_argv(spec, arguments):
    """Tool arguments -> words after `vp`. Positionals first, then flags in
    sorted order so the same call always gives the same command."""
    words = list(spec["argv"])
    rest = dict(arguments)
    if spec.get("stdin"):
        rest.pop(spec["stdin"], None)
    for key in spec["positional"]:
        if key in rest:
            value = rest.pop(key)
            for item in value if isinstance(value, list) else [value]:
                words.append(_word(item))
    flags = []
    for key in sorted(rest):
        value = rest[key]
        flag = "--" + key.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            flags.append(flag)
        elif isinstance(value, list):
            for item in value:
                flags += [flag, _word(item)]
        else:
            flags += [flag, _word(value)]
    if spec["positional"] and any(w.startswith("-") for w in words[len(spec["argv"]):]):
        # A positional value that looks like a flag must not be read as one.
        return words[:len(spec["argv"])] + flags + ["--"] + words[len(spec["argv"]):]
    return words + flags


def _word(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return compact_json(value)


# ------------------------------------------------------------------ receipts

def find_receipt(stdout):
    """Last stdout line that is a JSON object with ok, code and rc.
    Returns (receipt or None, stdout without that line)."""
    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ok"), bool) and "code" in obj and "rc" in obj:
            return obj, "\n".join(lines[:i] + lines[i + 1:])
    return None, stdout


def secret_values(env):
    out = []
    for key, value in env.items():
        if SECRET_NAME.search(key) and isinstance(value, str) and len(value) >= 8:
            out.append(value)
    return sorted(out, key=len, reverse=True)


def redact(text, secrets):
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    return text


def clip(text, limit):
    """Cut to at most `limit` UTF-8 bytes on a line boundary when possible."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    cut = raw[:limit]
    nl = cut.rfind(b"\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.decode("utf-8", errors="ignore"), True


class OpsServer:
    def __init__(self, tools_path=None, cli=None, env=None):
        self.env = dict(os.environ if env is None else env)
        mode = self.env.get("VP_OPS_TOOLS") or "core"
        if mode not in ("core", "all"):
            raise ToolTableError(f"VP_OPS_TOOLS must be core or all, got {mode!r}; "
                                 "the table path goes in VP_OPS_TOOLS_PATH")
        self.tools_path = (tools_path or self.env.get("VP_OPS_TOOLS_PATH")
                           or _first_existing(TOOLS_CANDIDATES))
        if not self.tools_path:
            raise ToolTableError("no MCP_TOOLS.json found; set VP_OPS_TOOLS_PATH")
        self.all_tools, self.specs = load_tools(self.tools_path)
        self.extras_path = self.env.get("VP_OPS_EXTRAS") or os.path.join(HERE, "MCP_EXTRAS.json")
        self.extras = load_extras(self.extras_path)
        for tool in self.all_tools:
            extra = self.extras["tools"].get(tool["name"])
            if not extra:
                continue
            example = extra.get("example")
            if isinstance(example, dict) and not validate(example, tool["inputSchema"]):
                tool["inputSchema"] = dict(tool["inputSchema"], examples=[example])
                tool["description"] += "\nExample arguments: " + compact_json(example)
            follow = [n for n in extra.get("next_tools") or [] if n in self.specs]
            if follow:
                self.specs[tool["name"]]["follow"] = follow
                tool["description"] += "\nUsually followed by: " + ", ".join(follow)
        self.docs_dir = self.env.get("VP_OPS_DOCS") or os.path.join(AGENT_INTERFACE, "docs")
        self.design_dir = os.path.join(AGENT_INTERFACE, "design")
        self.res_max = max(512, int(self.env.get("VP_OPS_RES_MAX_BYTES", "12288")))
        self.rt_base = self.env.get("VP_OPS_RT_BASE") or "/dev/shm/vqa-cu"
        self.progress_s = max(0.05, float(self.env.get("VP_OPS_PROGRESS_S", "2")))
        self.emit = None  # set by serve(): writes one notification line
        self.audit_path = self.env.get("VP_OPS_AUDIT") or None
        self.audit_seq = 0
        self.list_mode = mode
        self.tools = [t for t in self.all_tools
                      if mode == "all" or self.specs[t["name"]]["tier"] == "core"]
        self.cli = cli or self.env.get("VP_OPS_CLI") or _first_existing(CLI_CANDIDATES, need_exec=True)
        self.max_bytes = max(256, int(self.env.get("VP_OPS_MAX_BYTES", "4096")))
        self.timeout_s = float(self.env.get("VP_OPS_TIMEOUT_S", "120"))
        runtime = self.env.get("XDG_RUNTIME_DIR") or "/tmp"
        self.spool_dir = self.env.get("VP_OPS_SPOOL") or os.path.join(runtime, "vp-ops", "spool")
        self.npy = self.env.get("VP_OPS_NPY") or NPY_DEFAULT
        self.secrets = secret_values(self.env)
        self.seq = 0

    # -- JSON-RPC ----------------------------------------------------------

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return compact_json({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
        response = self.handle_request(obj)
        return None if response is None else compact_json(response)

    def handle_request(self, obj):
        def error(req_id, code, message):
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

        if not isinstance(obj, dict):
            return error(None, -32600, "invalid request")
        req_id = obj.get("id")
        method = obj.get("method")
        if obj.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return error(req_id, -32600, "invalid request")
        if req_id is None:
            return None  # notification
        if method == "initialize":
            params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
            wanted = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Each tool runs one `vp <noun> <verb>` command and returns bounded text "
                    "followed by one JSON receipt line {ok, code, rc, data, next}. Pass "
                    "dry_run=true to preview. Destructive tools need confirm=<id>; the id "
                    "comes from the dry run. When the text is cut, run the printed "
                    "newton-py read command to page the spool file. mcp.next_tools names "
                    "the tools to call next. Resources: viewpoint://docs/agent-guide (start "
                    "here), viewpoint://tools/index (all tools; tools/list shows tier core "
                    "unless VP_OPS_TOOLS=all, every tool is callable), viewpoint://state/* "
                    "and viewpoint://rt/{rt}/* (live state, bounded). Prompts hold the five "
                    "ready workflows. Send _meta.progressToken to get progress notifications."),
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.tools}}
        if method == "tools/call":
            params = obj.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return error(req_id, -32602, "invalid params: tools/call requires name")
            name = params["name"]
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return error(req_id, -32602, "invalid params: arguments must be an object")
            if name not in self.specs:
                return error(req_id, -32602, f"unknown tool: {name}")
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            token = meta.get("progressToken")
            if not isinstance(token, (str, int)) or isinstance(token, bool):
                token = None
            text, receipt = self.call_tool(name, arguments, token)
            self.audit("tools/call", tool=name, arguments=arguments, dry_run=arguments.get("dry_run") is True,
                       destructive=self.specs[name]["destructive"], ok=bool(receipt.get("ok")),
                       code=receipt.get("code"), rc=receipt.get("rc"),
                       started=(receipt.get("data") or {}).get("started", True),
                       next_tools=(receipt.get("mcp") or {}).get("next_tools", []))
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": receipt,
                "isError": not receipt.get("ok", False),
            }}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": self.resource_list()}}
        if method == "resources/templates/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resourceTemplates": self.resource_templates()}}
        if method == "resources/read":
            params = obj.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
                return error(req_id, -32602, "invalid params: resources/read requires uri")
            try:
                content = self.resource_read(params["uri"])
            except ResourceError as exc:
                self.audit("resources/read", uri=params["uri"], ok=False, code=exc.code)
                return error(req_id, exc.code, str(exc))
            self.audit("resources/read", uri=params["uri"], ok=True, bytes=len(content["text"].encode("utf-8")))
            return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [content]}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": p["name"], "title": p.get("title", p["name"]), "description": p["description"],
                 "arguments": [{k: a[k] for k in ("name", "description", "required") if k in a}
                               for a in p["arguments"]]}
                for p in self.extras["prompts"]]}}
        if method == "prompts/get":
            params = obj.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return error(req_id, -32602, "invalid params: prompts/get requires name")
            try:
                result = self.prompt_get(params["name"], params.get("arguments"))
            except ResourceError as exc:
                self.audit("prompts/get", prompt=params["name"], ok=False, code=exc.code)
                return error(req_id, exc.code, str(exc))
            self.audit("prompts/get", prompt=params["name"], ok=True)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        return error(req_id, -32601, f"method not found: {method}")

    def audit(self, method, **fields):
        """Append one line to the audit file. A write failure never fails the call."""
        if not self.audit_path:
            return
        self.audit_seq += 1
        row = dict(fields, seq=self.audit_seq, method=method, ts_ms=int(time.time() * 1000), pid=os.getpid())
        try:
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(redact(compact_json(_short(row, 400)), self.secrets) + "\n")
        except OSError:
            pass

    # -- prompts -----------------------------------------------------------

    def prompt_get(self, name, arguments):
        prompt = next((p for p in self.extras["prompts"] if p["name"] == name), None)
        if prompt is None:
            raise ResourceError(f"unknown prompt: {name}", -32602)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict) or not all(isinstance(v, str) for v in arguments.values()):
            raise ResourceError("invalid params: prompt arguments must be an object of strings", -32602)
        known = {a["name"]: a for a in prompt["arguments"]}
        unknown = sorted(set(arguments) - set(known))
        if unknown:
            raise ResourceError(f"invalid params: unknown prompt argument {unknown[0]!r}", -32602)
        values = {}
        for key, arg in known.items():
            value = arguments.get(key, arg.get("default"))
            if value is None or value == "":
                if arg.get("required"):
                    raise ResourceError(f"invalid params: prompt {name} requires argument {key!r}", -32602)
                value = f"<{key}>"
            if len(value) > 512 or "{{" in value or any(ord(c) < 32 for c in value):
                raise ResourceError(f"invalid params: prompt argument {key!r} must be one line of at most 512 characters", -32602)
            values[key] = value
        text = re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), prompt["text"])
        tools = []
        for tool in re.findall(r"\bvp_[a-z_]+", text):
            if tool in self.specs and tool not in tools:
                tools.append(tool)
        return {"description": prompt["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
                "_meta": {"tools": tools, "arguments": values}}

    # -- resources ---------------------------------------------------------

    def doc_files(self):
        """uri suffix -> (path, title). Only fixed names inside the docs and design directories."""
        out = {"docs/agent-guide": (os.path.join(self.docs_dir, "AGENT_GUIDE.md"), "Agent guide: start here"),
               "docs/troubleshooting": (os.path.join(self.docs_dir, "TROUBLESHOOTING.md"), "Failures by receipt code and what to do"),
               "docs/scenarios": (os.path.join(self.design_dir, "E2E_SCENARIOS.md"), "Five end-to-end command sequences"),
               "docs/commands": (os.path.join(self.design_dir, "COMMANDS.md"), "Complete argument reference of every vp command")}
        try:
            names = sorted(os.listdir(self.docs_dir))
        except OSError:
            names = []
        for name in names:
            m = re.match(r"^FAMILY_([a-z0-9]+)\.md$", name)
            if m:
                out["docs/family/" + m.group(1)] = (os.path.join(self.docs_dir, name), f"vp {m.group(1)}: commands, receipts, traps")
        return {k: v for k, v in out.items() if os.path.isfile(v[0])}

    def resource_list(self):
        out = []
        for suffix, (path, title) in self.doc_files().items():
            out.append({"uri": URI + suffix, "name": suffix, "title": title, "mimeType": "text/markdown",
                        "size": os.path.getsize(path)})
        out.append({"uri": URI + "tools/index", "name": "tools/index", "mimeType": "application/json",
                    "title": "Every tool with its tier, safety class and vp command"})
        out.append({"uri": URI + "state/capsules", "name": "state/capsules", "mimeType": "application/json",
                    "title": "Live: the take (vp capsule status) and the jobs this session owns (vp sys jobs)"})
        for kind in STATE_KINDS:
            out.append({"uri": URI + "state/" + kind, "name": "state/" + kind, "mimeType": "application/json",
                        "title": f"Live: {kind} of the default runtime root (VP_SWARM_RT)"})
        return out

    def resource_templates(self):
        out = [{"uriTemplate": URI + "docs/family/{noun}", "name": "docs/family", "mimeType": "text/markdown",
                "title": "Family page of one vp noun (append ?offset=N to page)"}]
        for kind in STATE_KINDS:
            out.append({"uriTemplate": URI + "rt/{rt}/" + kind, "name": "rt/" + kind, "mimeType": "application/json",
                        "title": f"Live: {kind} of one runtime root; {{rt}} is a name under "
                                 f"{self.rt_base} or a percent-encoded absolute path"})
        return out

    def resolve_rt(self, token):
        """Name under rt_base or absolute path -> real directory. The live demo lanes are refused."""
        if token is None:
            path = self.env.get("VP_SWARM_RT") or os.path.join(self.rt_base, "ref")
        elif token.startswith("/"):
            path = token
        elif RT_NAME.match(token):
            path = os.path.join(self.rt_base, token)
        else:
            raise ResourceError("bad runtime root: use a name or a percent-encoded absolute path", -32602)
        real = os.path.realpath(path)
        for bad in FORBIDDEN_RT:
            if real == bad or real.startswith(bad + "/"):
                raise ResourceError(f"FORBIDDEN_PATH: runtime root {real} belongs to a live lane and is not readable here", -32602)
        if not os.path.isdir(real):
            raise ResourceError(f"runtime root not found: {real}")
        return real

    def resource_read(self, uri):
        from urllib.parse import unquote
        if not uri.startswith(URI):
            raise ResourceError(f"unknown resource: {uri}")
        body, _, query = uri[len(URI):].partition("?")
        offset = 0
        if query:
            m = re.match(r"^offset=(\d{1,12})$", query)
            if not m:
                raise ResourceError("invalid params: the only query is ?offset=<bytes>", -32602)
            offset = int(m.group(1))
        base_uri = URI + body
        docs = self.doc_files()
        if body in docs:
            try:
                with open(docs[body][0], "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                raise ResourceError(f"cannot read {body}: {exc.strerror}")
            return self.page(base_uri, "text/markdown", raw, offset)
        if body == "tools/index":
            rows = [{"name": n, "tier": s["tier"], "destructive": s["destructive"], "vp": " ".join(s["argv"])}
                    for n, s in self.specs.items()]
            return self.page(base_uri, "application/json", compact_json({"tools": rows, "count": len(rows)}).encode(), offset)
        if body == "state/capsules":
            data = {"take": self.vp_read(["capsule", "status"]), "jobs": self.vp_read(["sys", "jobs"])}
            return self.page(base_uri, "application/json", compact_json(data).encode(), offset)
        m = re.match(r"^(?:state|rt/([^/]+))/([a-z-]+)$", body)
        if m and m.group(2) in STATE_KINDS:
            rt = self.resolve_rt(unquote(m.group(1)) if m.group(1) is not None else None)
            data = rt_state(rt, m.group(2), self.res_max)
            return self.page(base_uri, "application/json", compact_json(data).encode(), offset)
        raise ResourceError(f"unknown resource: {uri}")

    def page(self, uri, mime, raw, offset):
        """One bounded page. The trailer names the uri of the next page."""
        total = len(raw)
        if offset > total:
            raise ResourceError(f"invalid params: offset {offset} is past the end ({total} bytes)", -32602)
        chunk = raw[offset:offset + self.res_max]
        if offset + len(chunk) < total:
            nl = chunk.rfind(b"\n")
            if nl > len(chunk) // 2:
                chunk = chunk[:nl + 1]
        text = redact(chunk.decode("utf-8", errors="ignore"), self.secrets)
        end = offset + len(chunk)
        out = {"uri": uri if not offset else f"{uri}?offset={offset}", "mimeType": mime, "text": text,
               "_meta": {"bytes": [offset, end], "total_bytes": total, "sha256": hashlib.sha256(raw).hexdigest()}}
        if end < total:
            out["_meta"]["next"] = f"{uri}?offset={end}"
            out["text"] += f"\n[cut: bytes {offset}..{end} of {total}; next page: {uri}?offset={end}]"
        return out

    def vp_read(self, words):
        """Run one read-only vp command and return its receipt."""
        if not self.cli or not os.access(self.cli, os.X_OK):
            return {"ok": False, "code": "CLI_MISSING", "rc": RC_NO_CLI, "data": {}, "next": []}
        try:
            proc = subprocess.run([self.cli] + words + ["--json"], stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, errors="replace", env=self.env, timeout=20, start_new_session=True)
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "TIMEOUT", "rc": RC_TIMEOUT, "data": {}, "next": []}
        except OSError as exc:
            return {"ok": False, "code": "CLI_MISSING", "rc": RC_NO_CLI, "data": {"error": exc.strerror}, "next": []}
        receipt, _ = find_receipt(redact(proc.stdout, self.secrets))
        return receipt or {"ok": False, "code": "NO_RECEIPT", "rc": RC_NO_RECEIPT, "data": {}, "next": []}

    # -- tool calls --------------------------------------------------------

    def refusal(self, code, rc, message, data=None, nxt=None):
        receipt = {"ok": False, "code": code, "rc": rc,
                   "data": dict(data or {}, error=message), "next": nxt or []}
        return message + "\n" + compact_json(receipt), receipt

    def progress(self, token, step, message, total=None):
        if token is None or self.emit is None:
            return
        params = {"progressToken": token, "progress": step, "message": message}
        if total is not None:
            params["total"] = total
        self.emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    def call_tool(self, name, arguments, token=None):
        spec = self.specs[name]
        problems = validate(arguments, spec["schema"])
        if problems:
            return self.refusal(
                "SCHEMA", RC_SCHEMA, "arguments rejected: " + "; ".join(problems[:8]),
                {"tool": name, "problems": problems[:8], "started": False})
        dry = arguments.get("dry_run") is True
        confirm = arguments.get("confirm")
        if spec["destructive"] and not dry and not (isinstance(confirm, str) and confirm.strip()):
            return self.refusal(
                "CONFIRM_REQUIRED", RC_CONFIRM,
                f"{name} is destructive: call it with dry_run=true to get the confirm id, "
                "then again with confirm=<id>",
                {"tool": name, "started": False},
                ["vp " + " ".join(shlex.quote(w) for w in build_argv(spec, dict(arguments, dry_run=True)))])
        if not self.cli or not os.access(self.cli, os.X_OK):
            return self.refusal("CLI_MISSING", RC_NO_CLI,
                                "vp command line not found; set VP_OPS_CLI",
                                {"tool": name, "started": False})
        words = build_argv(spec, arguments)
        stdin_text = arguments.get(spec["stdin"]) if spec.get("stdin") else None
        limit_s = spec.get("timeout_s") or self.timeout_s
        if isinstance(arguments.get("timeout_s"), (int, float)):
            limit_s = max(limit_s, float(arguments["timeout_s"]) + 10)
        started = time.monotonic()
        try:
            child = subprocess.Popen(
                [self.cli] + words,
                stdin=subprocess.PIPE if isinstance(stdin_text, str) else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", env=self.env, start_new_session=True)
        except OSError as exc:
            return self.refusal("CLI_MISSING", RC_NO_CLI, f"cannot start vp: {exc.strerror}",
                                {"tool": name, "started": False})
        label = "vp " + " ".join(spec["argv"])
        self.progress(token, 0, f"started {label} (pid {child.pid}, limit {limit_s:g} s)")
        beats, out, err = 0, None, None
        try:
            while True:
                left = limit_s - (time.monotonic() - started)
                if left <= 0:
                    raise subprocess.TimeoutExpired(label, limit_s)
                try:
                    out, err = child.communicate(stdin_text if isinstance(stdin_text, str) else None,
                                                 timeout=min(self.progress_s, left) if token is not None else left)
                    break
                except subprocess.TimeoutExpired:
                    if token is None:
                        raise
                    beats += 1
                    self.progress(token, beats, f"{label} running, {time.monotonic() - started:.0f} s of {limit_s:g} s")
        except subprocess.TimeoutExpired:
            # The child leads its own session, so this group holds only what it started.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except OSError:
                    break
                try:
                    child.communicate(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return self.refusal("TIMEOUT", RC_TIMEOUT,
                                f"vp {' '.join(spec['argv'])} passed the {limit_s:g} s limit and was stopped",
                                {"tool": name, "started": True, "stopped_pid": child.pid})
        proc = subprocess.CompletedProcess(words, child.returncode, out, err)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = redact(proc.stdout, self.secrets)
        stderr = redact(proc.stderr, self.secrets)
        receipt, human = find_receipt(stdout)
        if receipt is None:
            receipt = {"ok": False, "code": "NO_RECEIPT", "rc": RC_NO_RECEIPT if proc.returncode == 0 else proc.returncode,
                       "data": {"error": "vp printed no receipt line", "exit_status": proc.returncode},
                       "next": []}
        elif proc.returncode != 0 and receipt.get("ok"):
            # The exit status wins over a receipt that claims success.
            receipt = dict(receipt, ok=False, code="RC_MISMATCH", rc=proc.returncode)
        if not isinstance(receipt.get("next"), list):
            receipt["next"] = [] if receipt.get("next") in (None, "") else [receipt["next"]]
        if not isinstance(receipt.get("data"), dict):
            receipt["data"] = {} if receipt.get("data") is None else {"value": receipt["data"]}
        human = human.strip("\n")
        if stderr.strip() and not receipt.get("ok"):
            human = (human + "\n[stderr]\n" + stderr.strip("\n")).strip("\n")
        self.progress(token, beats + 1, f"{label} finished: {receipt.get('code')} rc {receipt.get('rc')} in {elapsed_ms} ms",
                      total=beats + 1)
        return self.bound(name, human, receipt, elapsed_ms)

    def bound(self, name, human, receipt, elapsed_ms):
        """Cut the human text and the receipt to the byte ceiling; spool what
        does not fit and say how to page it."""
        meta = {"tool": name, "ms": elapsed_ms}
        full = human + ("\n" if human else "") + compact_json(receipt) + "\n"
        receipt_line = compact_json(receipt)
        text, cut = clip(human, self.max_bytes)
        big_receipt = len(receipt_line.encode("utf-8")) > self.max_bytes
        if cut or big_receipt:
            path = self.spool(name, full)
            if path:
                read_cmd = f"{self.npy} read {path} 1:60"
                meta["spool"] = {"path": path, "bytes": len(full.encode("utf-8")),
                                 "lines": full.count("\n"),
                                 "sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
                                 "read": read_cmd}
                receipt["next"] = list(receipt["next"]) + [read_cmd]
            else:
                meta["spool"] = {"path": None, "error": "spool directory not writable"}
            if big_receipt:
                data_text, _ = clip(compact_json(receipt.get("data")), self.max_bytes // 2)
                receipt["data"] = {"truncated": True, "preview": data_text}
            if cut:
                text += f"\n[cut: {len(human.encode('utf-8'))} bytes of text, shown {len(text.encode('utf-8'))}]"
                if path:
                    text += f"\n[full output: {meta['spool']['read']}]"
        hints = self.next_tools(receipt["next"])
        if receipt.get("ok"):
            # After a success the usual follow-ups of the workflow come after the ones vp named.
            hints += [n for n in self.specs[name].get("follow") or [] if n not in hints]
        if hints:
            meta["next_tools"] = hints
        receipt["mcp"] = meta
        return (text + "\n" if text else "") + compact_json(receipt), receipt

    def next_tools(self, hints):
        """Tool names for the `vp <noun> <verb> ...` lines of `next`."""
        by_argv = {tuple(spec["argv"]): name for name, spec in self.specs.items()}
        out = []
        for hint in hints:
            words = hint.split() if isinstance(hint, str) else []
            if len(words) >= 3 and words[0] == "vp":
                name = by_argv.get(tuple(words[1:3]))
                if name and name not in out:
                    out.append(name)
        return out

    def spool(self, name, content):
        try:
            os.makedirs(self.spool_dir, mode=0o700, exist_ok=True)
            self.seq += 1
            path = os.path.join(self.spool_dir, f"{name}.{os.getpid()}.{self.seq}.out")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            return path
        except OSError:
            return None


def load_extras(path):
    """MCP_EXTRAS.json; a missing or broken file gives no examples and no prompts."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    tools = doc.get("tools") if isinstance(doc, dict) and isinstance(doc.get("tools"), dict) else {}
    prompts = []
    for p in (doc.get("prompts") if isinstance(doc, dict) and isinstance(doc.get("prompts"), list) else []):
        if (isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("text"), str)
                and isinstance(p.get("description"), str) and isinstance(p.get("arguments"), list)
                and all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in p["arguments"])):
            prompts.append(p)
    return {"tools": {k: v for k, v in tools.items() if isinstance(v, dict)}, "prompts": prompts}


def _tail_lines(path, max_bytes):
    """Last complete lines of a file within max_bytes, plus the file size."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        raw = fh.read()
    if start:
        raw = raw.partition(b"\n")[2]
    return raw.decode("utf-8", errors="replace").splitlines(), size, start > 0


def _short(value, limit=160):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[{len(value)} chars]"
    if isinstance(value, list) and len(value) > 12:
        return [_short(v, limit) for v in value[:12]] + [f"...[{len(value)} items]"]
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    return value


def _events(rt, budget):
    path = os.path.join(rt, "events.jsonl")
    if not os.path.isfile(path):
        return [], 0, False, 0
    lines, size, partial = _tail_lines(path, budget)
    out, bad = [], 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out, size, partial, bad


def rt_state(rt, kind, max_bytes):
    """Read-only view of one runtime root (COMMON.md contract). Never opens a PNG, never writes."""
    base = {"rt": rt, "kind": kind}
    if kind == "last-frame":
        frames = os.path.join(rt, "frames")
        best = -1
        try:
            names = os.listdir(frames)
        except OSError:
            names = []
        for name in names:
            m = re.match(r"^frame_(\d+)\.json$", name)
            if m:
                best = max(best, int(m.group(1)))
        if best < 0:
            return dict(base, found=False, hint="no frame record yet: start the capsule and run a turn")
        path = os.path.join(frames, f"frame_{best}.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.loads(fh.read(65536))
        except (OSError, ValueError) as exc:
            return dict(base, found=False, path=path, error=type(exc).__name__)
        return dict(base, found=True, path=path, frames=len([n for n in names if n.endswith(".json")]),
                    record=_short(record))
    # The window read from the log: the reply keeps well under the ceiling after trimming.
    events, size, partial, bad = _events(rt, 262144)
    base.update(events_bytes=size, window_partial=partial, unparsed_lines=bad)
    if not size:
        base["hint"] = "no events.jsonl yet: the broker has not run in this runtime root"
    if kind == "events-tail":
        rows = []
        spent = 0
        for rec in reversed(events):
            row = _short(rec)
            cost = len(compact_json(row))
            if rows and (spent + cost > max_bytes // 2 or len(rows) >= 40):
                break
            rows.append(row)
            spent += cost
        return dict(base, shown=len(rows), events=list(reversed(rows)),
                    more="vp_queue_events {rt, range:\"1:40\"} pages the whole log")
    closed = {r.get("event_id") for r in events if r.get("rec") == "close"}
    if kind == "microtasks":
        rows = [{"event_id": r.get("event_id"), "turn": r.get("turn"), "tick": r.get("tick"),
                 "verdict": r.get("verdict"), "frame": r.get("verify_frame_ref")}
                for r in events if r.get("rec") == "close"][-30:]
        verdicts = {}
        for r in events:
            if r.get("rec") == "close":
                verdicts[str(r.get("verdict"))] = verdicts.get(str(r.get("verdict")), 0) + 1
        return dict(base, source="one microtask per close record of events.jsonl (what projection posts as post_step)",
                    shown=len(rows), by_verdict=verdicts, microtasks=rows)
    # queue-head
    pending = [{"event_id": r.get("event_id"), "agent_id": r.get("agent_id"), "lane": r.get("lane"),
                "kind": r.get("kind"), "based_on_frame": r.get("based_on_frame")}
               for r in events if r.get("rec") == "enqueue" and r.get("event_id") not in closed]
    last = {}
    for r in events:
        if r.get("rec") in ("turn", "close", "control"):
            last[r["rec"]] = _short(r)
    mode = "RUNNING"
    if last.get("control"):
        mode = {"stop": "FROZEN", "park": "FROZEN", "reconcile": "RECONCILE"}.get(last["control"].get("op"), "RUNNING")
    waiting = {}
    intents = os.path.join(rt, "intents")
    try:
        agents = sorted(os.listdir(intents))
    except OSError:
        agents = []
    for agent in agents[:16]:
        try:
            waiting[agent] = len([n for n in os.listdir(os.path.join(intents, agent)) if n.endswith(".json")])
        except OSError:
            continue
    return dict(base, mode_from_last_control=mode, last_turn=last.get("turn"), last_close=last.get("close"),
                last_control=last.get("control"), enqueued_not_closed=pending[:12],
                enqueued_not_closed_count=len(pending), intent_files=waiting,
                note="derived from the log tail; vp_queue_status is the broker's own answer")


def serve(server, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def emit(message):
        stdout.write(compact_json(message) + "\n")
        stdout.flush()
    server.emit = emit
    for line in stdin:
        response = server.handle_line(line)
        if response is not None:
            stdout.write(response + "\n")
            stdout.flush()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        server = OpsServer()
    except ToolTableError as exc:
        sys.stderr.write(f"viewpoint-ops: {exc}\n")
        return 78
    if argv and argv[0] == "--check":
        sys.stdout.write(compact_json({
            "ok": True, "code": "OK", "rc": 0, "next": [],
            "data": {"server": SERVER_NAME, "version": SERVER_VERSION,
                     "tools_path": server.tools_path, "tools": len(server.all_tools), "listed": len(server.tools), "list_mode": server.list_mode,
                     "destructive": sorted(n for n, s in server.specs.items() if s["destructive"]),
                     "cli": server.cli, "cli_is_stub": bool(server.cli) and os.path.basename(server.cli) == "stub_vp",
                     "spool_dir": server.spool_dir, "max_bytes": server.max_bytes,
                     "resources": len(server.resource_list()), "resource_templates": len(server.resource_templates()),
                     "prompts": [p["name"] for p in server.extras["prompts"]],
                     "examples": sum(1 for t in server.all_tools if "examples" in t["inputSchema"]),
                     "res_max_bytes": server.res_max}}) + "\n")
        return 0
    serve(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
