# @module {"id":"wa.scripts.ast_gate","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Restricted syntax, without execution or plan authority.

Admission's keyword ports are trusted host injections, not a frozen production ABI.
header_codec(source, policy) returns normalized body bytes after closed header checks.
reservation.__enter__() returns exact True only with trusted aggregate peak
accounting for source copies, tokenization, AST, literals, output, transport,
allocator overhead and child peaks. Its owner retains denied cleanup reservations.
verify_supervisor(reference, supervisor, policy) checks retained identity AND live
restriction/probe evidence; only exact True authorizes dispatch. supervisor.parse
owns deadline/memory/output/cancellation enforcement; close terminates, joins and
drains on every exit, returning exact True only after observing completion.
Supplying the supervisor transfers transient ownership at admission entry; cleanup
precedes reservation release once capacity is acquired.
resolve(role, immutable_literal, policy) must return exact
True after closed-schema, slot authority and recursive predicate-budget checks.
No port is discovered from candidate data. Production implementations are external.
Returned tuples are private compiler input, never validated-plan authority.
"""
import ast as _ast
import sys as _sys
import re as _re
import io as _io
import tokenize as _tokenize
from types import MappingProxyType as _Map
from unicodedata import normalize as _normalize
from .types import ContractError as _Error, Reason as _Reason, SourcePolicy as _Source, ParserPolicy as _Parser

_FIELDS = _Map({
    _ast.Module: ('body', 'type_ignores'),
    _ast.FunctionDef: ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment') +
        (('type_params',) if 'type_params' in _ast.FunctionDef._fields else ()),
    _ast.arguments: ('posonlyargs', 'args', 'vararg', 'kwonlyargs', 'kw_defaults', 'kwarg', 'defaults'),
    _ast.arg: ('arg', 'annotation', 'type_comment'), _ast.Assign: ('targets', 'value', 'type_comment'),
    _ast.Name: ('id', 'ctx'), _ast.Load: (), _ast.Store: (), _ast.Expr: ('value',),
    _ast.Call: ('func', 'args', 'keywords'), _ast.Attribute: ('value', 'attr', 'ctx'),
    _ast.keyword: ('arg', 'value'), _ast.Constant: ('value', 'kind'),
    _ast.Dict: ('keys', 'values'), _ast.List: ('elts', 'ctx'),
    _ast.Tuple: ('elts', 'ctx'), _ast.Return: ('value',),
})
_ALLOWED = frozenset(_FIELDS)
_VERBS = _Map({'find': (1, (), 'query'), 'click': (1, ('expect',), 'handle'),
    'type': (1, ('slot', 'expect'), 'handle'), 'key': (1, ('keysym', 'expect'), 'handle'),
    'wait_until': (1, ('timeout_ms',), 'predicate'),
    'assert_tag': (1, ('code',), 'predicate'), 'harvest': (1, ('slot_out', 'max_bytes'), 'handle')})
_NAME = _re.compile(r'[A-Za-z][A-Za-z0-9_]*\Z', _re.ASCII)


def _need(ok, reason=_Reason.SCHEMA):
    if not ok:
        raise _Error(reason)


def _guard(operation):
    reason = None
    try:
        return operation()
    except _Error as error:
        reason = error.reason
    except Exception:
        reason = _Reason.SCHEMA
    # Outside the native handler: no private exception context survives.
    raise _Error(reason)


def _policy(policy, cls):
    _need(policy is not None, _Reason.MISSING)
    _need(type(policy) is cls, _Reason.TYPE)
    numeric = ('unicode_scalars', 'utf8_bytes', 'header_lines') if cls is _Source else (
        'ast_nodes', 'ast_depth', 'predicate_nodes', 'timeout_ms', 'incremental_memory_bytes')
    _need(all(type(getattr(policy, field)) is int for field in numeric), _Reason.TYPE)
    _need(all(getattr(policy, field) > 0 for field in numeric), _Reason.BOUNDS)
    if cls is _Parser:
        _need(type(policy.supervisor_reference) is str and bool(policy.supervisor_reference), _Reason.MISSING)
        _text(policy.supervisor_reference)
    policy._validate()


def _text(value):
    _need(type(value) is str, _Reason.TYPE)
    _need('\x00' not in value)
    value.encode('utf-8', 'strict')
    return value


# Field shapes are checked before traversal, including scalar fields that must
# never smuggle descendants into a syntactically unrelated location.
_LIST_FIELDS = frozenset(('body', 'type_ignores', 'decorator_list', 'type_params',
    'posonlyargs', 'args', 'kwonlyargs', 'kw_defaults', 'defaults', 'targets',
    'keywords', 'keys', 'values', 'elts'))
_TEXT_FIELDS = frozenset(('name', 'id', 'attr', 'arg'))
_OPTIONAL_AST_FIELDS = frozenset(('returns', 'annotation', 'vararg', 'kwarg'))


def _field_shape(cls, field, value):
    if cls is _ast.Constant:
        _need(value is None if field == 'kind' else
              value is None or type(value) in (str, int, bool))
    elif field in _TEXT_FIELDS:
        _need(type(value) is str or (cls is _ast.keyword and value is None))
    elif field == 'type_comment':
        _need(value is None or type(value) is str)
    elif field in _LIST_FIELDS and not (cls is _ast.FunctionDef and field == 'args'):
        _need(type(value) is list)
    elif field in _OPTIONAL_AST_FIELDS or (cls is _ast.Return and field == 'value'):
        _need(value is None or type(value) in _ALLOWED)
    else:
        _need(type(value) in _ALLOWED)


def _whole(tree, policy):
    _policy(policy, _Parser)
    stack = [(tree, 0, False)]
    active = set()
    count = 0
    while stack:
        node, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(node))
            continue
        count += 1
        _need(count <= policy.ast_nodes and depth <= policy.ast_depth, _Reason.BOUNDS)
        cls = type(node)
        _need(cls in _ALLOWED and cls._fields == _FIELDS[cls])
        _need(id(node) not in active)
        _need(not (set(vars(node)) - set(_FIELDS[cls]) - set(cls._attributes)))
        for attribute in cls._attributes:
            if hasattr(node, attribute):
                location = getattr(node, attribute)
                _need(location is None or (type(location) is int and location >= 0))
        active.add(id(node))
        stack.append((node, depth, True))
        children = []
        for field in _FIELDS[cls]:
            # Class defaults must not stand in for omitted instance fields.
            _need(field in vars(node))
            value = getattr(node, field)
            _field_shape(cls, field, value)
            if type(value) is list:
                _need(len(children) + len(value) <= policy.ast_nodes - count, _Reason.BOUNDS)
                for child in value:
                    _need(type(child) in _ALLOWED)
                    children.append(child)
            elif isinstance(value, _ast.AST):
                children.append(value)
            else:
                _need(value is None or type(value) in (str, int, bool))
        _need(len(stack) + len(children) <= 2 * policy.ast_nodes + 1, _Reason.BOUNDS)
        stack.extend((child, depth + 1, False) for child in reversed(children))


def _literal(root):
    results = {}
    stack = [(root, False)]
    while stack:
        node, exiting = stack.pop()
        cls = type(node)
        if cls is _ast.Constant:
            value = node.value
            _need(type(value) in (str, int, bool, type(None)), _Reason.TYPE)
            _need(node.kind is None)
            if type(value) is str:
                _text(value)
            results[id(node)] = value
            continue
        _need(cls in (_ast.Dict, _ast.List, _ast.Tuple))
        if cls is _ast.Dict:
            _need(type(node.keys) is list and type(node.values) is list and len(node.keys) == len(node.values))
            keys = set()
            for key in node.keys:
                _need(type(key) is _ast.Constant and type(key.value) is str and key.kind is None)
                _text(key.value)
                _need(key.value not in keys)
                keys.add(key.value)
            children = node.values
        else:
            _need(type(node.ctx) is _ast.Load and type(node.elts) is list)
            children = node.elts
        if not exiting:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(children))
        elif cls is _ast.Dict:
            results[id(node)] = ('dict', tuple((key.value, results[id(value)]) for key, value in zip(node.keys, children)))
        else:
            results[id(node)] = ('list' if cls is _ast.List else 'tuple', tuple(results[id(child)] for child in children))
    return results[id(root)]


def _symbol(value):
    _need(type(value) is str and _NAME.fullmatch(value) is not None)
    return value


def _role(role, node, handles):
    if role == 'handle':
        _need(type(node) is _ast.Name and type(node.ctx) is _ast.Load and node.id in handles)
        return ('handle', node.id)
    value = _literal(node)
    if role in ('timeout_ms', 'max_bytes'):
        _need(type(value) is int, _Reason.TYPE)
        _need(0 < value <= (30000 if role == 'timeout_ms' else 16384), _Reason.BOUNDS)
    elif role in ('slot', 'slot_out'):
        _symbol(value)
    elif role == 'keysym':
        # Key symbols belong to the retained host vocabulary, not handle names.
        _need(type(value) is str)
    elif role == 'code':
        _need(type(value) is str and value in frozenset(r.value for r in _Reason))
    return value


def _representation_bound(tree, budget):
    # Deterministic representation guard, NOT aggregate peak accounting.
    # This uses the caller's budget,
    # never an undocumented per-string or integer limit. Whole-tree validation
    # has already bounded occurrences and excluded cycles.
    pending = [tree]
    charge = 0
    while pending:
        node = pending.pop()
        charge += 128
        for field in _FIELDS[type(node)]:
            value = getattr(node, field)
            if type(value) is list:
                pending.extend(value)
            elif isinstance(value, _ast.AST):
                pending.append(value)
            elif type(value) is str:
                charge += 12 * len(value) + 2
                _need(charge <= budget, _Reason.BOUNDS)
                _text(value)
            elif type(value) is int:
                charge += value.bit_length() + 2
        _need(charge <= budget, _Reason.BOUNDS)


def _validate(tree, policy, representation_bytes=None):
    _whole(tree, policy)
    if representation_bytes is not None:
        _representation_bound(tree, representation_bytes)
    _need(type(tree) is _ast.Module and type(tree.body) is list and len(tree.body) == 1 and tree.type_ignores == [])
    fn = tree.body[0]
    _need(type(fn) is _ast.FunctionDef and fn.name == 'run' and fn.decorator_list == [] and
          fn.returns is None and fn.type_comment is None and getattr(fn, 'type_params', []) == [])
    args = fn.args
    _need(type(args) is _ast.arguments and args.posonlyargs == [] and args.kwonlyargs == [] and
          args.kw_defaults == [] and args.defaults == [] and args.vararg is None and args.kwarg is None and
          type(args.args) is list and len(args.args) == 2)
    for arg, name in zip(args.args, ('act', 'params')):
        _need(type(arg) is _ast.arg and arg.arg == name and arg.annotation is None and arg.type_comment is None)
    _need(type(fn.body) is list and bool(fn.body))
    handles, collisions, checked = set(), {'act', 'params'}, []
    for index, statement in enumerate(fn.body):
        if type(statement) is _ast.Return:
            _need(index == len(fn.body) - 1 and type(statement.value) is _ast.Constant and
                  statement.value.value is None and statement.value.kind is None)
            continue
        assignment = type(statement) is _ast.Assign
        _need(assignment or type(statement) is _ast.Expr)
        name = None
        if assignment:
            _need(type(statement.targets) is list and len(statement.targets) == 1 and statement.type_comment is None)
            target = statement.targets[0]
            _need(type(target) is _ast.Name and type(target.ctx) is _ast.Store)
            name = _symbol(target.id)
            key = _normalize('NFKC', name).casefold()
            _need(key not in collisions)
        call = statement.value
        _need(type(call) is _ast.Call and type(call.func) is _ast.Attribute)
        func = call.func
        _need(type(func.value) is _ast.Name and func.value.id == 'act' and
              type(func.value.ctx) is _ast.Load and type(func.ctx) is _ast.Load and type(func.attr) is str and func.attr in _VERBS)
        verb = func.attr
        arity, required, role = _VERBS[verb]
        _need((verb == 'find') == assignment and type(call.args) is list and len(call.args) == arity and type(call.keywords) is list)
        keywords = {}
        for kw in call.keywords:
            _need(type(kw) is _ast.keyword and type(kw.arg) is str and kw.arg not in keywords)
            keywords[kw.arg] = kw.value
        _need(set(keywords) == set(required))
        positional = _role(role, call.args[0], handles)
        values = tuple((kw, _role('predicate' if kw == 'expect' else kw, keywords[kw], handles)) for kw in required)
        checked.append((name, verb, role, positional, values))
        if assignment:
            handles.add(name)
            collisions.add(key)
    return tuple(checked)


# @anchor:wa.scripts.ast_gate.validate_restricted_ast
def validate_restricted_ast(tree, policy):
    """Return exact True only after the whole grammar has passed."""
    _guard(lambda: _validate(tree, policy))
    return True


def _source_bytes(source, policy):
    _need(type(source) is bytes, _Reason.TYPE)
    _need(len(source) <= policy.utf8_bytes, _Reason.BOUNDS)
    text = source.decode('utf-8', 'strict')
    _need(len(text) <= policy.unicode_scalars, _Reason.BOUNDS)
    _need('\x00' not in text)
    _need(not text.startswith('\ufeff'))
    # PEP 263 declarations occupy a comment on the first line, or the
    # second line when the first contains only whitespace/a comment.
    lines = _io.StringIO(text, newline=None)
    first, second = lines.readline(), lines.readline()
    cookie = r'^[ \t\f]*\#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+'
    _need(_re.match(cookie, first) is None)
    if _re.match(r'^[ \t\f]*(?:\#.*)?(?:\r?\n)?$', first):
        _need(_re.match(cookie, second) is None)
    return source.replace(b'\r\n', b'\n').replace(b'\r', b'\n')


def _original_identifiers(body):
    # Python's parser normalizes identifiers with NFKC. Token spellings retain
    # the original bytes, while comments and string literal content stay passive.
    for token in _tokenize.tokenize(_io.BytesIO(body).readline):
        if token.type == _tokenize.COMMENT:
            # Ordinary ast.parse discards these directives. Reject before the
            # header port can remove comments or the parser can normalize them.
            _need(_re.match(r'\#\s*type\s*:', token.string) is None)
        if token.type in (_tokenize.NAME, _tokenize.ERRORTOKEN):
            _need(token.string.isascii())


def _admit_owned(source, source_policy, parser_policy, header_codec, reservation,
           supervisor, verify_supervisor, resolve, representation_bytes, close):
    _policy(source_policy, _Source)
    _policy(parser_policy, _Parser)
    _need(representation_bytes is not None, _Reason.MISSING)
    _need(type(representation_bytes) is int, _Reason.TYPE)
    _need(representation_bytes > 0, _Reason.BOUNDS)
    _need(type(source) is bytes, _Reason.TYPE)
    _need(len(source) <= source_policy.utf8_bytes, _Reason.BOUNDS)
    _need(all(port is not None for port in (header_codec, reservation, supervisor, verify_supervisor, resolve)), _Reason.MISSING)
    # No source decoding, encoding or scratch copies precede capacity evidence.
    cleanup_complete = False
    try:
        _need(reservation.__enter__() is True, _Reason.MISSING)
        try:
            raw = _source_bytes(source, source_policy)
            _original_identifiers(raw)
            body = header_codec(raw, source_policy)
            _need(type(body) is bytes, _Reason.TYPE)
            _need(len(body) <= source_policy.utf8_bytes and len(body) <= representation_bytes, _Reason.BOUNDS)
            _need(_source_bytes(body, source_policy) == body)
            _original_identifiers(body)
            _need(verify_supervisor(parser_policy.supervisor_reference, supervisor, parser_policy) is True, _Reason.MISSING)
            tree = supervisor.parse(body, parser_policy, representation_bytes)
            checked = _validate(tree, parser_policy, representation_bytes)
            # The deterministic representation guard precedes literal copies;
            # aggregate peak coverage belongs to the trusted reservation.
            # Never materialize a diagnostic repr here.
            for _, _, role, value, keywords in checked:
                if role != 'handle':
                    _need(resolve(role, value, parser_policy) is True)
                for name, literal in keywords:
                    _need(resolve('predicate' if name == 'expect' else name, literal, parser_policy) is True)
            _need(verify_supervisor(parser_policy.supervisor_reference, supervisor, parser_policy) is True, _Reason.MISSING)
            return checked
        finally:
            close()
            cleanup_complete = True
    finally:
        # Outstanding resources keep their capacity charged. The trusted owner
        # must recover them and release later; calling close is not evidence.
        if cleanup_complete:
            reservation.__exit__(*_sys.exc_info())


def _admit(source, source_policy, parser_policy, header_codec, reservation,
           supervisor, verify_supervisor, resolve, representation_bytes):
    # Passing a supervisor transfers transient ownership immediately. This
    # covers even policy/preflight/reservation failures before parser dispatch.
    closed = False
    def _close():
        nonlocal closed
        if not closed and supervisor is not None:
            closed = True
            _need(supervisor.close() is True, _Reason.MISSING)
    try:
        return _admit_owned(source, source_policy, parser_policy, header_codec,
            reservation, supervisor, verify_supervisor, resolve, representation_bytes, _close)
    finally:
        _close()




# @anchor:wa.scripts.ast_gate.admit_source
def admit_source(source, source_policy, parser_policy, *, header_codec=None,
                 reservation=None, supervisor=None, verify_supervisor=None,
                 resolve=None, representation_bytes=None):
    """Conditionally admit private immutable syntax through trusted host ports."""
    return _guard(lambda: _admit(source, source_policy, parser_policy, header_codec,
        reservation, supervisor, verify_supervisor, resolve, representation_bytes))
