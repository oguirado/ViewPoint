"""Static privacy/process controls; planted source is parsed, never executed."""
import ast
import json
from pathlib import Path
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_REMOTE = re.compile(r"(?<![\w:.-])([A-Za-z][A-Za-z0-9_-]{0,63}):([A-Za-z0-9_][A-Za-z0-9_./-]*)")
_FOLDER_ID = re.compile(r"1[A-Za-z0-9_-]{24,}")


def private_literals(value):
    violations = []
    if _EMAIL.search(value):
        violations.append("email_literal")
    for match in _REMOTE.finditer(value):
        remote, path = match.groups()
        if remote == "YOUR_REMOTE" and path == "YOUR_ASSET_ROOT/":
            continue
        if remote == "urn" and path == "af" and value[match.end():].startswith(":"):
            continue
        violations.append("remote_literal")
    if _FOLDER_ID.fullmatch(value):
        violations.append("folder_id_literal")
    # A root name without a remote is still private; preserve the old control.
    if "ViewPoint_" + "Assets3D" in value:
        violations.append("private_root")
    return violations


# Closed capability inventory for this surface. New imports or module members
# require an explicit review here; an unrecognised spelling never gains rights.
_CAPABILITIES = {
    'argparse': {'ArgumentParser'},
    'ast': {'AnnAssign', 'Assign', 'Attribute', 'Call', 'Constant', 'Dict',
            'Import', 'ImportFrom', 'List', 'Load', 'Name', 'NamedExpr', 'Set', 'Starred',
            'Subscript', 'Tuple', 'iter_child_nodes', 'parse', 'walk'},
    'contextlib': {'contextmanager'},
    'ctypes': {'CDLL', 'byref', 'c_char_p', 'c_int', 'c_uint', 'c_void_p',
               'create_string_buffer', 'get_errno'},
    'datetime': {'datetime', 'datetime.now', 'timezone', 'timezone.utc'},
    'errno': {'EEXIST', 'ENOTEMPTY'},
    'fcntl': {'LOCK_EX', 'LOCK_NB', 'LOCK_UN', 'flock'},
    'hashlib': {'md5', 'sha256'},
    'json': {'loads'},
    'os': {'O_CREAT', 'O_DIRECTORY', 'O_EXCL', 'O_NOFOLLOW', 'O_NONBLOCK',
           'O_RDONLY', 'O_RDWR', 'O_WRONLY', 'close', 'devnull', 'dup', 'environ',
           'environ.get', 'fdopen', 'fsencode', 'fstat', 'fsync', 'geteuid',
           'link', 'listdir', 'mkdir', 'open', 'read', 'replace', 'rmdir', 'scandir', 'stat', 'unlink'},
    'pathlib': {'Path', 'Path.home', 'PurePosixPath'},
    're': {'compile', 'fullmatch'},
    'secrets': {'token_hex'},
    'shlex': {'quote'},
    'shutil': {'which', 'rmtree'},
    'signal': {'SIGKILL'},
    'stat': {'S_IMODE', 'S_ISDIR', 'S_ISREG'},
    'struct': {'Struct'},
    'subprocess': {'DEVNULL', 'TimeoutExpired'},
    'tempfile': {'TemporaryDirectory', 'TemporaryFile'},
    'time': {'monotonic', 'monotonic_ns', 'sleep'},
    'asset_factory': {'core', 'proc'},
    'asset_factory.core': {'ContractError', 'RC', 'STATUS', 'VERDICT',
                           'atomic_write_json', 'canonical_json', 'load_json',
                           'sha256_json', 'validate'},
    'asset_factory.proc': {'popen', 'signal_group', 'signal_pid', 'register_group'},
    'tools.publish.local_store': {'CredentialPathRefusal', 'LocalStore', 'LocalPathRefusal',
                                  'LocalBudgetExceeded', 'bound_directory', 'check_names',
                                  'child_directory', 'directory_identity', 'mount_id',
                                  'remove_tree', 'rename_noreplace'},
    'tools.publish.publisher': {'main'},
    'native.libc': {'renameat2', 'renameat2.argtypes', 'renameat2.restype',
                    'statx', 'statx.argtypes', 'statx.restype'},
    'owned.child': {'pid', 'wait'},
}
_ALLOWED_VALUES = set(_CAPABILITIES) | {
    module + '.' + member for module, members in _CAPABILITIES.items() for member in members
}
_DYNAMIC = {'exec', 'eval', 'compile', '__import__', 'getattr', 'setattr',
            'delattr', 'vars', 'globals', 'locals', '__builtins__'}
# Receiver-independent signalling is also forbidden: only the shared guarded
# helper above owns process authority, never an arbitrary object supplied to us.
_PROCESS_MEMBERS = {'kill', 'killpg', 'terminate', 'send_signal', 'Popen',
                    'pthread_kill', 'raise_signal', 'pidfd_send_signal', 'kill_process'}


_SPAWN_KWARGS = {
    'start_new_session', 'memory_limits', 'cwd', 'env', 'stdin', 'stdout',
    'stderr', 'input', 'text', 'encoding', 'runtime_s', 'close_fds',
    'bufsize', 'errors', 'universal_newlines',
}


def static_violations(source):
    tree = ast.parse(source)
    aliases, violations = {}, []

    def symbols(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, {node.id})
        if isinstance(node, ast.Attribute):
            return {name + '.' + node.attr for name in symbols(node.value)}
        if isinstance(node, ast.Call):
            names = symbols(node.func)
            result = set()
            if 'ctypes.CDLL' in names:
                result.add('native.libc')
            if 'process_helper' in names:
                result.add('asset_factory.proc')
            if 'asset_factory.proc.popen' in names:
                result.add('owned.child')
            return result
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*(symbols(item) for item in node.elts))
        if isinstance(node, ast.Dict):
            return set().union(*(symbols(item) for item in node.values))
        if isinstance(node, ast.Subscript):
            # Track a module/capability even when hidden in a literal container.
            return symbols(node.value) - {'os.environ'}
        return set()

    def bind(target, value):
        if isinstance(target, ast.Name):
            return {target.id: symbols(value)}
        if isinstance(target, (ast.Tuple, ast.List)):
            # Over-approximation is deliberate: every possible source authority
            # survives unpacking and later reassignment, never last-write-wins.
            result = {}
            for child in target.elts:
                if isinstance(child, ast.Starred):
                    child = child.value
                result.update(bind(child, value))
            return result
        return {}

    nodes = list(ast.walk(tree))
    parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    # Sets grow monotonically. Preserve aliases from every assignment so a later
    # harmless rebind cannot erase a forbidden capability earlier in the source.
    for _ in range(len(nodes) + 1):
        changed = False
        for node in nodes:
            bindings = {}
            if isinstance(node, ast.Import):
                bindings = {a.asname or a.name: {a.name} for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                bindings = {a.asname or a.name: {(node.module or '') + '.' + a.name} for a in node.names}
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    bindings.update(bind(target, node.value))
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                bindings = bind(node.target, node.value)
            for name, targets in bindings.items():
                old = aliases.get(name, set())
                if not targets <= old:
                    aliases[name], changed = old | targets, True
        if not changed:
            break

    def refuse():
        violations.append('unguarded_process')

    def is_call_target(node):
        parent = parents.get(node)
        return isinstance(parent, ast.Call) and parent.func is node

    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            violations.extend(private_literals(node.value))
        if isinstance(node, ast.Assign):
            names = [n.id.lower() for n in node.targets if isinstance(n, ast.Name)]
            if any('folder_id' in name for name in names) and isinstance(node.value, ast.Constant):
                if node.value.value and not str(node.value.value).startswith('YOUR_'):
                    violations.append('folder_id_literal')
        if isinstance(node, ast.Import):
            if any(a.name not in _CAPABILITIES for a in node.names):
                refuse()
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in _CAPABILITIES or any(
                    a.name not in _CAPABILITIES[node.module] for a in node.names):
                refuse()
        if isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
            for name in symbols(node):
                parent = parents.get(node)
                member_base = isinstance(parent, ast.Attribute) and parent.value is node
                if name == 'asset_factory.proc':
                    if not member_base or not is_call_target(parent):
                        refuse()
                    continue
                if name.startswith('asset_factory.proc.') and not is_call_target(node):
                    refuse()
                if isinstance(node, ast.Call):
                    continue
                leaf = name.rsplit('.', 1)[-1]
                if leaf in _DYNAMIC and name not in _ALLOWED_VALUES or leaf in _PROCESS_MEMBERS or \
                        leaf.startswith('__') and leaf != '__init__':
                    # Module-level __file__/__name__ variables are ordinary data.
                    if not isinstance(node, ast.Name) or name not in {'__file__', '__name__', '__doc__'}:
                        refuse()
                roots = [root for root in _CAPABILITIES if name == root or name.startswith(root + '.')]
                if roots and name not in _ALLOWED_VALUES:
                    refuse()
                # Module objects cannot escape into arbitrary functions/returns
                # where their member accesses would lose a statically known root.
                if name in _CAPABILITIES and name != 'owned.child' and (
                        not isinstance(node, ast.Name) or isinstance(node.ctx, ast.Load)):
                    native_feature = name == 'native.libc' and isinstance(parent, ast.Call) and \
                        isinstance(parent.func, ast.Name) and parent.func.id == 'hasattr' and \
                        len(parent.args) == 2 and isinstance(parent.args[1], ast.Constant) and \
                        parent.args[1].value in ('renameat2', 'statx')
                    if not member_base and not native_feature:
                        refuse()
                if name == 'ctypes.CDLL' and not is_call_target(node):
                    refuse()
        if isinstance(node, ast.Call):
            names = symbols(node.func)
            if 'ctypes.CDLL' in names and not (
                    len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and
                    node.args[0].value is None and all(
                        k.arg == 'use_errno' and isinstance(k.value, ast.Constant) and
                        k.value.value is True for k in node.keywords)):
                refuse()
            if 'asset_factory.proc.popen' in names and (
                    len(node.args) != 1 or not isinstance(node.args[0], (ast.List, ast.Tuple)) or
                    any(k.arg not in _SPAWN_KWARGS for k in node.keywords)):
                refuse()
            if any(name.startswith('subprocess.') for name in names) and any(
                    k.arg is None or k.arg == 'shell' and not (
                        isinstance(k.value, ast.Constant) and k.value.value is False)
                    for k in node.keywords):
                refuse()
            if 'shutil.rmtree' in names and not (
                    len(node.args) == 1 and not isinstance(node.args[0], ast.Starred) and
                    len(node.keywords) == 1 and node.keywords[0].arg == 'dir_fd'):
                refuse()
        if isinstance(node, ast.Subscript) and any(
                name in _CAPABILITIES for name in symbols(node.value)):
            refuse()
    return violations

def scan_surface(directory):
    """Inspect every source/config/document file, including nested paths."""
    violations = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and path.suffix in (".py", ".json", ".md", ".markdown", ".sh"):
            source = path.read_text()
            found = static_violations(source) if path.suffix == ".py" else private_literals(source)
            if path.suffix == ".json":
                def folders(value):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if "folder_id" in key.lower() and child and not str(child).startswith("YOUR_"):
                                found.append("folder_id_literal")
                            folders(child)
                    elif isinstance(value, list):
                        for child in value:
                            folders(child)
                folders(json.loads(source))
            violations.extend((str(path.relative_to(directory)), reason) for reason in found)
    return violations
