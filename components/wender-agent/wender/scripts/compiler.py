# @module {"id":"wa.scripts.compiler","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.scripts.ast_gate"],"book":"HB-WA-s4-learn"}
"""Restricted source compiler and exact retained plan custody.

Derived from scripts_mesh/SCRIPTS_DESIGN_v2.md and S1_CONTRACT_FREEZE.md;
reviewed against r1/wt/s-s1_w/wender/scripts/compiler.py (not its codec).
Host interfaces below are compiler-local, supplied out of band, never from source.
Owners implement check_scope(scope, episode), check_query(query),
check_predicate(predicate, action_or_none, query_or_none),
check_slot(descriptor, declaration, episode), check_key(symbol, descriptor),
check_harvest(spec), check_terminal(goal), check_export(selector), and
check_screen(digest). Each returns exact AdmissionCheck(TRUE, None).
Allocation owners implement reserve(bytes, deadline)->RegistryRef,
check(token, bytes, deadline)->AdmissionCheck, release(token)->AdmissionCheck.
Reserve accounts overlapping Python representations, allocator overhead and all
retained references; release authenticates actual drain, never elapsed time.
No input capability is consumed by these interfaces.
"""
import ast as _ast
import dataclasses as _dc
import enum as _enum
import types as _types
from . import types as _t, header as _h, ast_gate as _g

_need, _check, _closed = _h._need, _h._check, _h._closed
_RECORDS = frozenset(v for v in vars(_t).values()
                     if isinstance(v, type) and issubclass(v, _t._Record))
_AST = {cls.__name__: cls for cls in _g._FIELDS}


# @anchor:wa.scripts.compiler.CompilerInputs
@_dc.dataclass(frozen=True, slots=True, repr=False)
class CompilerInputs:
    """Exact immutable host bindings and explicit allocation authority.

    effects: (action, query_name, precondition, verification) tuples.
    waits: (registered predicate literal, WaitPolicy) pairs.
    harvests: (result_name, ResultDeclaration, RegistryRef, QueryDeclaration,
    ScopeIdentity) tuples. All other binding tables contain literal/value pairs.
    work_bytes is a host-accounted peak, not a production default.
    """
    _queries: tuple
    _predicates: tuple
    _effects: tuple
    _descriptors: tuple
    _keys: tuple
    _waits: tuple
    _harvests: tuple
    _terminal: _t.TerminalGoalRef
    _screen: _t.PlanDigest
    _episode: str
    _owners: object
    _allocation: object
    _work_bytes: int


# @anchor:wa.scripts.compiler.CompilerRegistry
class CompilerRegistry:
    """Host-created registry; no capacity or retention defaults.

    entry_bytes must cover the complete retained Python graph, association,
    canonical bytes and registry bookkeeping, certified by allocation.check.
    Unreleased failed allocations remain available through outstanding().
    """
    # @anchor:wa.scripts.compiler.CompilerRegistry.__init__
    @_closed
    def __init__(self, *, compiler, incarnation, generation, max_entries,
                 max_bytes, entry_bytes, allocation):
        _need(type(compiler) is _t.PrivateRef, _t.Reason.TYPE)
        _need(type(incarnation) is str and incarnation == compiler.incarnation,
              _t.Reason.FOREIGN)
        for value in (generation, max_entries, max_bytes, entry_bytes):
            _need(type(value) is int and value > 0, _t.Reason.BOUNDS)
        _need(entry_bytes <= max_bytes and allocation is not None, _t.Reason.MISSING)
        self._compiler, self._incarnation, self._generation = compiler, incarnation, generation
        self._max_entries, self._max_bytes, self._entry_bytes = max_entries, max_bytes, entry_bytes
        self._allocation = allocation
        self._entries, self._recovery, self._transactions = {}, [], {}
        self._serial = 0

    def _complete(self, definition, session, admission, inputs):
        canonical = _definition(definition, session, admission, inputs)
        witness = _snapshot(definition, self._entry_bytes)
        transaction = object()
        self._transactions[id(transaction)] = (transaction, definition, canonical,
                                               session, admission, inputs, witness)
        return transaction

    def _publish(self, transaction):
        completed = self._transactions.pop(id(transaction), None)
        _need(completed is not None and completed[0] is transaction, _t.Reason.FOREIGN)
        _, definition, canonical, session, admission, inputs, witness = completed
        admission._lifetime(session)
        _need(len(self._entries) < self._max_entries and
              (len(self._entries) + 1) * self._entry_bytes <= self._max_bytes, _t.Reason.CAPACITY)
        _need(len(canonical) <= self._entry_bytes, _t.Reason.CAPACITY)
        deadline = definition.limits.absolute_deadline_ns
        token = self._allocation.reserve(self._entry_bytes, deadline)
        _need(type(token) is _t.RegistryRef, _t.Reason.TYPE)
        self._recovery.append((self._allocation, token))
        try:
            _check(self._allocation.check(token, self._entry_bytes, deadline))
            self._serial += 1
            handle = _t.PrivateRef(self._compiler.owner, self._incarnation, str(self._serial))
            reference = _t.RegistryRef('compiler', self._incarnation, self._generation, handle)
            association = _t.CanonicalPlanAssociation(definition, canonical, reference)
            plan = _t.ValidatedPlan(definition.nodes, definition.symbols, definition.slots,
                definition.entry, definition.terminal_goal, definition.limits, definition.header.abis,
                definition.identity, definition.header.scope, handle, definition, canonical,
                reference, definition.queries)
            admission._lifetime(session)
            _check(inputs._owners.check_scope(plan.scope, inputs._episode))
            _need(_snapshot(definition, self._entry_bytes) == witness, _t.Reason.STALE)
            snapshot = _snapshot(plan, self._entry_bytes)
            self._entries[id(plan)] = (plan, association, handle, reference, token, snapshot)
            self._recovery.remove((self._allocation, token))
            return plan
        except Exception:
            _check(self._allocation.release(token))
            self._recovery.remove((self._allocation, token))
            raise

    # @anchor:wa.scripts.compiler.CompilerRegistry.check
    @_closed
    def check(self, plan, compiler, identity, scope, abis):
        _need(type(plan) is _t.ValidatedPlan, _t.Reason.TYPE)
        row = self._entries.get(id(plan))
        _need(row is not None and row[0] is plan, _t.Reason.FOREIGN)
        _, association, handle, reference, token, snapshot = row
        _need(_snapshot(plan, self._entry_bytes) == snapshot, _t.Reason.STALE)
        _need(compiler is self._compiler and plan.compiler_handle is handle
              and plan.compiler_registry is reference and association.compiler is reference
              and reference.owner_incarnation == self._incarnation
              and reference.generation == self._generation, _t.Reason.FOREIGN)
        _need(plan.definition is association.plan and plan.canonical_bytes is association.canonical_bytes
              and plan.identity == identity == association.plan.identity
              and plan.scope == scope == association.plan.header.scope
              and plan.abis == abis == association.plan.header.abis, _t.Reason.STALE)
        for name in ('nodes', 'symbols', 'slots', 'entry', 'terminal_goal', 'limits', 'queries'):
            _need(getattr(plan, name) is getattr(association.plan, name), _t.Reason.STALE)
        _check(self._allocation.check(token, self._entry_bytes, plan.limits.absolute_deadline_ns))
        _need(_snapshot(plan, self._entry_bytes) == snapshot, _t.Reason.STALE)
        return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.scripts.compiler.CompilerRegistry.release
    @_closed
    def release(self, plan):
        self.check(plan, self._compiler, plan.identity, plan.scope, plan.abis)
        return self._revoke(plan)

    def _revoke(self, plan):
        row = self._entries.get(id(plan))
        _need(row is not None and row[0] is plan, _t.Reason.FOREIGN)
        del self._entries[id(plan)]
        self._recovery.append((self._allocation, row[4]))
        _check(self._allocation.release(row[4]))
        self._recovery.remove((self._allocation, row[4]))
        return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.scripts.compiler.CompilerRegistry.outstanding
    def outstanding(self):
        return tuple(self._entries.values()), tuple(self._recovery)


def _issue(definition, session, admission, inputs, registry):
    transaction = registry._complete(definition, session, admission, inputs)
    try:
        return registry._publish(transaction)
    finally:
        registry._transactions.pop(id(transaction), None)


def _snapshot(root, budget):
    """Bounded private, immutable custody witness, including omitted authority."""
    stack, active, built = [(root, 0, False)], set(), {}
    count, charge = 0, 0
    while stack:
        value, depth, exit_ = stack.pop()
        cls = type(value)
        if exit_:
            children = tuple(getattr(value, f.name) for f in _dc.fields(value)) if cls in _RECORDS else value
            built[id(value)] = (cls, id(value), tuple(built[id(child)] for child in children))
            active.remove(id(value))
            continue
        count += 1
        charge += 128 + (12 * len(value) if cls is str else len(value) if cls is bytes else 0)
        _need(count <= budget // 128 and charge <= budget and depth <= 64, _t.Reason.CAPACITY)
        if cls in _RECORDS or cls is tuple:
            _need(id(value) not in active, _t.Reason.SCHEMA)
            children = tuple(getattr(value, f.name) for f in _dc.fields(value)) if cls in _RECORDS else value
            _need(len(children) + len(stack) <= budget // 128, _t.Reason.CAPACITY)
            active.add(id(value))
            stack.append((value, depth, True))
            stack.extend((child, depth + 1, False) for child in reversed(children))
        else:
            _need(value is None or cls in (str, int, bool, bytes) or
                  (isinstance(value, _enum.Enum) and cls.__module__ == _t.__name__), _t.Reason.TYPE)
            built[id(value)] = (cls, value)
    return built[id(root)]


def _same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _binding(table, literal):
    matches = [value for key, value in table if _same(key, literal)]
    _need(len(matches) == 1, _t.Reason.FOREIGN if not matches else _t.Reason.CONFLICT)
    return matches[0]


def _walk(root, admission, session):
    """Bound and reject cycles/mutable objects before recursive S0 checks."""
    state = admission._lifetime(session)
    stack, active, records = [(root, 0, False)], set(), []
    count, charge = 0, 0
    while stack:
        value, depth, exit_ = stack.pop()
        if exit_:
            active.remove(id(value))
            if type(value) in _RECORDS:
                records.append(value)
            continue
        admission._lifetime(session)
        count += 1
        charge += 1024
        _need(count <= state.grant.charged_bytes // 1024 and depth <= 64, _t.Reason.BOUNDS)
        cls = type(value)
        if cls is str:
            charge += 12 * len(value)
        elif cls is int:
            charge += value.bit_length()
        elif cls is bytes:
            charge += len(value)
        _need(charge <= state.grant.charged_bytes, _t.Reason.CAPACITY)
        if cls in _RECORDS or cls is tuple:
            _need(id(value) not in active, _t.Reason.SCHEMA)
            children = tuple(getattr(value, f.name) for f in _dc.fields(value)) if cls in _RECORDS else value
            _need(len(children) + count + len(stack) <= state.grant.charged_bytes // 1024,
                  _t.Reason.BOUNDS)
            active.add(id(value))
            stack.append((value, depth, True))
            stack.extend((child, depth + 1, False) for child in reversed(children))
        else:
            _need(value is None or cls in (str, int, bool, bytes) or
                  (isinstance(value, _enum.Enum) and cls.__module__ == _t.__name__
                   and any(value is member for member in cls)), _t.Reason.TYPE)
    for record in records:
        if type(record) is _t.PredicateSpec:
            pending, seen = [record], 0
            while pending:
                p = pending.pop()
                seen += 1
                _need(seen <= admission._parser.predicate_nodes, _t.Reason.BOUNDS)
                _need(type(p) is _t.PredicateSpec and type(p.children) is tuple, _t.Reason.TYPE)
                pending.extend(p.children)
        _t._Record.__post_init__(record)
    return charge


def _inputs(inputs, admission, session):
    _need(type(inputs) is CompilerInputs, _t.Reason.TYPE)
    for name in ('_queries', '_predicates', '_effects', '_descriptors', '_keys', '_waits', '_harvests'):
        _need(type(getattr(inputs, name)) is tuple, _t.Reason.TYPE)
        _walk(getattr(inputs, name), admission, session)
    _need(type(inputs._work_bytes) is int and inputs._work_bytes > 0, _t.Reason.MISSING)
    _need(type(inputs._episode) is str and bool(inputs._episode)
          and inputs._owners is not None and inputs._allocation is not None, _t.Reason.MISSING)
    for table in (inputs._queries, inputs._predicates, inputs._descriptors, inputs._keys, inputs._waits):
        _need(all(type(row) is tuple and len(row) == 2 for row in table), _t.Reason.SCHEMA)
        for i, (key, _) in enumerate(table):
            _need(not any(_same(key, other[0]) for other in table[:i]), _t.Reason.CONFLICT)
    _need(all(len(row) == 4 for row in inputs._effects)
          and all(len(row) == 5 for row in inputs._harvests), _t.Reason.SCHEMA)
    _check(inputs._owners.check_scope(admission._scope, inputs._episode))


def _decode_ast(result, admission, session):
    """Verify all field/index associations before constructing descendants."""
    policy = admission._parser
    _walk(result, admission, session)
    stack, count = [(result, 0)], 0
    while stack:
        item, depth = stack.pop()
        admission._lifetime(session)
        count += 1
        _need(count <= policy.ast_nodes and depth <= policy.ast_depth, _t.Reason.BOUNDS)
        _need(type(item) is tuple and len(item) == 3, _t.Reason.SCHEMA)
        kind, children, fields = item
        _need(type(kind) is str and kind in _AST and type(children) is tuple
              and type(fields) is tuple, _t.Reason.SCHEMA)
        cls = _AST[kind]
        _need(tuple(pair[0] for pair in fields if type(pair) is tuple and len(pair) == 2)
              == _g._FIELDS[cls], _t.Reason.SCHEMA)
        refs, pending, visits = [], [pair[1] for pair in fields], 0
        while pending:
            tagged = pending.pop()
            visits += 1
            _need(visits <= policy.ast_nodes * 8, _t.Reason.BOUNDS)
            _need(type(tagged) is tuple and len(tagged) == 2, _t.Reason.SCHEMA)
            tag, value = tagged
            if tag == 'node':
                _need(type(value) is int and 0 <= value < len(children), _t.Reason.SCHEMA)
                refs.append(value)
            elif tag == 'list':
                _need(type(value) is tuple and len(value) <= policy.ast_nodes, _t.Reason.SCHEMA)
                _need(all(type(v) is tuple and len(v) == 2 and v[0] == 'node' for v in value),
                      _t.Reason.SCHEMA)
                pending.extend(value)
            else:
                scalar = {'none': type(None), 'str': str, 'bool': bool, 'int': int}
                _need(type(tag) is str and tag in scalar and type(value) is scalar[tag], _t.Reason.TYPE)
        _need(sorted(refs) == list(range(len(children))), _t.Reason.SCHEMA)
        _need(count + len(stack) + len(children) <= policy.ast_nodes, _t.Reason.BOUNDS)
        stack.extend((child, depth + 1) for child in children)

    def build(item):
        kind, children, fields = item
        nodes = tuple(build(child) for child in children)
        def value(tagged):
            tag, data = tagged
            if tag == 'node':
                return nodes[data]
            if tag == 'list':
                return [value(v) for v in data]
            return data
        return _AST[kind](**{name: value(data) for name, data in fields})
    return build(result)


class _Bridge:
    # @anchor:wa.scripts.compiler._Bridge.__init__
    def __init__(self, session, admission, inputs):
        self.session, self.admission, self.inputs = session, admission, inputs
        self.tree = None

    # @anchor:wa.scripts.compiler._Bridge.__enter__
    def __enter__(self):
        self.admission._lifetime(self.session)
        return True

    # @anchor:wa.scripts.compiler._Bridge.__exit__
    def __exit__(self, *args):
        return False

    # @anchor:wa.scripts.compiler._Bridge.parse
    def parse(self, body, policy, representation_bytes):
        _need(body == self.session._body and policy is self.admission._parser, _t.Reason.FOREIGN)
        self.tree = _decode_ast(self.session._result, self.admission, self.session)
        return self.tree

    # @anchor:wa.scripts.compiler._Bridge.close
    def close(self):
        self.tree = None
        return True

    def _verify(self, reference, supervisor, policy):
        state = self.admission._lifetime(self.session)
        _need(supervisor is self and policy is self.admission._parser, _t.Reason.FOREIGN)
        _need(self.admission._resolver.resolve(reference) is state.backend, _t.Reason.STALE)
        _check(self.admission._verifier.check(state.evidence, state.backend, reference, policy))
        return True

    def _resolve(self, role, literal, policy):
        if role in ('query', 'predicate'):
            record = _binding(self.inputs._queries if role == 'query' else self.inputs._predicates, literal)
            _need(type(record) is (_t.QueryDeclaration if role == 'query' else _t.PredicateSpec), _t.Reason.TYPE)
        elif role == 'slot':
            _binding(self.inputs._descriptors, literal)
        elif role == 'keysym':
            _binding(self.inputs._keys, literal)
        elif role == 'slot_out':
            _need(sum(row[0] == literal for row in self.inputs._harvests) == 1, _t.Reason.FOREIGN)
        return True

    def _header(self, raw, policy):
        state = self.admission._lifetime(self.session)
        _need(raw == state.source.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
              and policy is self.admission._source, _t.Reason.FOREIGN)
        return self.session._body


def _checked(session, admission, inputs):
    bridge = _Bridge(session, admission, inputs)
    state = admission._lifetime(session)
    return _g.admit_source(state.source, admission._source, admission._parser,
        header_codec=bridge._header, reservation=bridge, supervisor=bridge,
        verify_supervisor=bridge._verify, resolve=bridge._resolve,
        representation_bytes=state.grant.charged_bytes)


def _node(index, kind, **payload):
    return _t.StepPlan('step' + str(index), kind, payload.get('effect'), None, None,
        payload.get('terminal'), payload.get('find'), payload.get('wait'),
        payload.get('assertion'), payload.get('harvest'))


def _lower(checked, header, inputs, admission, session):
    nodes, handles = [], {}
    for name, verb, role, literal, keywords in checked:
        admission._lifetime(session)
        _need(len(nodes) + 1 < header.limits.max_nodes, _t.Reason.BOUNDS)
        kw = dict(keywords)
        action = _t.ActionKind(verb)
        if verb == 'find':
            query = _binding(inputs._queries, literal)
            handles[name] = query
            node = _node(len(nodes), action, find=_t.FindSpec(query))
        elif verb in ('click', 'type', 'key'):
            query = handles.get(literal[1])
            _need(query is not None, _t.Reason.FOREIGN)
            rules = [r for r in inputs._effects if r[0] is action and r[1] == query.name]
            _need(len(rules) == 1, _t.Reason.CONFLICT if rules else _t.Reason.MISSING)
            post = _binding(inputs._predicates, kw['expect'])
            arguments = ()
            if verb == 'type':
                arguments = (_binding(inputs._descriptors, kw['slot']),)
            elif verb == 'key':
                arguments = (_binding(inputs._keys, kw['keysym']),)
                _check(inputs._owners.check_key(kw['keysym'], arguments[0]))
            node = _node(len(nodes), action, effect=_t.EffectSpec(action, query.selector,
                arguments, rules[0][2], post, rules[0][3]))
        elif verb == 'wait_until':
            predicate = _binding(inputs._predicates, literal)
            profile = _binding(inputs._waits, literal)
            _need(kw['timeout_ms'] <= profile.semantic_wait_ms, _t.Reason.BOUNDS)
            node = _node(len(nodes), action, wait=_t.WaitSpec(predicate,
                _dc.replace(profile, semantic_wait_ms=kw['timeout_ms'])))
        elif verb == 'assert_tag':
            node = _node(len(nodes), action, assertion=_t.AssertSpec(
                _binding(inputs._predicates, literal), _t.Reason(kw['code'])))
        else:
            query = handles.get(literal[1])
            rows = [r for r in inputs._harvests if r[0] == kw['slot_out']]
            _need(query is not None and len(rows) == 1, _t.Reason.FOREIGN)
            _, result, auth, bound_query, scope = rows[0]
            _need(query is bound_query, _t.Reason.FOREIGN)
            node = _node(len(nodes), action, harvest=_t.HarvestSpec(query.name, result,
                auth, scope, kw['max_bytes']))
        nodes.append(node)
    nodes.append(_node(len(nodes), _t.NodeKind.TERMINAL, terminal=inputs._terminal))
    queries = tuple(row[1] for row in inputs._queries)
    selectors = []
    for query in queries:
        if not any(query.selector is selector for selector in selectors):
            selectors.append(query.selector)
    return dict(nodes=tuple(nodes), selectors=tuple(selectors),
        symbols=tuple(row[1] for row in inputs._harvests), slots=header.arguments,
        entry=header.entry, terminal_goal=inputs._terminal, limits=header.limits,
        queries=queries, structural_screen_digest=inputs._screen)


def _predicate(predicate, queries, scope, inputs, admission, session, action=None, query=None):
    _check(inputs._owners.check_predicate(predicate, action, query))
    stack, count = [predicate], 0
    while stack:
        current = stack.pop()
        count += 1
        _need(count <= admission._parser.predicate_nodes, _t.Reason.BOUNDS)
        _need(current.scope == scope, _t.Reason.FOREIGN)
        for operand in current.operands:
            declared = next((q for q in queries if q.name == operand.query_id), None)
            _need(declared is not None and current.operator in declared.operators, _t.Reason.FOREIGN)
        stack.extend(current.children)
        admission._lifetime(session)
    if action is not None:
        def relevant(p):
            if p.operator is _t.PredicateOperator.ALL:
                return any(relevant(c) for c in p.children)
            if p.operator is _t.PredicateOperator.ANY:
                return bool(p.children) and all(relevant(c) for c in p.children)
            return p.operator in (_t.PredicateOperator.DECLARED_TRANSITION,
                _t.PredicateOperator.VALUE_LEN_DELTA, _t.PredicateOperator.SCOPED_TERMINAL,
                _t.PredicateOperator.TASK_GOAL) and bool(p.operands) and all(
                    o.query_id == query.name for o in p.operands)
        _need(relevant(predicate), _t.Reason.MISSING)


def _validate(components, header, inputs, admission, session):
    values = tuple(getattr(components, name) for name in (
        'nodes', 'selectors', 'symbols', 'slots', 'entry', 'terminal_goal', 'limits', 'queries',
        'structural_screen_digest'))
    _walk(values + (header,), admission, session)
    scope = admission._scope
    expected_queries = tuple(q for _, q in inputs._queries)
    _need(len(components.queries) == len(expected_queries) and
          all(a is b for a, b in zip(components.queries, expected_queries)), _t.Reason.FOREIGN)
    expected_selectors = []
    for query in expected_queries:
        if not any(query.selector is s for s in expected_selectors):
            expected_selectors.append(query.selector)
    _need(len(components.selectors) == len(expected_selectors) and
          all(a is b for a, b in zip(components.selectors, expected_selectors)), _t.Reason.FOREIGN)
    _need(len(components.symbols) == len(inputs._harvests) and
          all(a is row[1] for a, row in zip(components.symbols, inputs._harvests)), _t.Reason.FOREIGN)
    _need(header.abis == _h._ABIS, _t.Reason.SCHEMA)
    _need(components.slots == header.arguments and components.entry == header.entry
          and components.limits == header.limits and header.scope == scope, _t.Reason.CONFLICT)
    _need(components.terminal_goal is inputs._terminal and
          components.terminal_goal.reference == header.terminal_goal, _t.Reason.FOREIGN)
    _check(inputs._owners.check_terminal(components.terminal_goal))
    _check(inputs._owners.check_screen(components.structural_screen_digest))
    _need(components.structural_screen_digest == inputs._screen, _t.Reason.FOREIGN)
    limits = components.limits
    state = admission._lifetime(session)
    authority_deadline = min(limits.absolute_deadline_ns, admission._deadline,
        state.grant.absolute_deadline_ns, state.grant.request.absolute_deadline_ns)
    _need(admission._now() < limits.absolute_deadline_ns, _t.Reason.DEADLINE)
    _need(len(components.slots) <= admission._slots.slots, _t.Reason.BOUNDS)
    for slot in components.slots:
        _need(slot.normalization_abi == 'wender-args/verbatim-v1', _t.Reason.SCHEMA)
        _h._names((slot.name,))
        if slot.kind in (_t.ValueKind.TEXT, _t.ValueKind.KEY):
            _need(slot.maximum <= admission._slots.input_slot_bytes
                  and slot.maximum <= limits.episode_input_bytes, _t.Reason.BOUNDS)
    _t._plan_structure(components.nodes, components.symbols, components.slots,
        components.entry, components.terminal_goal, limits, scope, components.queries)
    for q in components.queries:
        _need(any(q is v for _, v in inputs._queries), _t.Reason.FOREIGN)
        _check(inputs._owners.check_query(q))
        _need(any(q.selector is s for s in components.selectors), _t.Reason.FOREIGN)
    for selector in components.selectors:
        _need(any(q.selector is selector for q in components.queries), _t.Reason.FOREIGN)
        _check(inputs._owners.check_export(selector))
    _need(len({id(s) for s in components.selectors}) == len(components.selectors), _t.Reason.CONFLICT)
    _predicate(components.entry, components.queries, scope, inputs, admission, session)
    found, written = [], set()
    for node in components.nodes:
        admission._lifetime(session)
        if node.find is not None:
            _need(any(node.find.query is q for q in components.queries), _t.Reason.FOREIGN)
            found.append(node.find.query)
        if node.effect is not None:
            effect = node.effect
            candidates = [q for q in found if q.selector is effect.selector]
            _need(len({id(q) for q in candidates}) == 1, _t.Reason.FOREIGN)
            query = candidates[0]
            _need(effect.action is effect.selector.action, _t.Reason.SCHEMA)
            _need(any(s is effect.selector for s in components.selectors), _t.Reason.FOREIGN)
            rules = [r for r in inputs._effects if r[0] is effect.action and r[1] == query.name]
            _need(len(rules) == 1, _t.Reason.CONFLICT if rules else _t.Reason.MISSING)
            _need(effect.precondition is rules[0][2] and effect.verification == rules[0][3], _t.Reason.FOREIGN)
            _predicate(effect.precondition, components.queries, scope, inputs, admission, session)
            _predicate(effect.postcondition, components.queries, scope, inputs, admission, session, effect.action, query)
            budget = effect.verification
            _need(budget.max_acquisitions <= limits.max_acquisitions and
                  admission._now() + budget.timeout_ms * 1000000 <= budget.absolute_deadline_ns <= authority_deadline,
                  _t.Reason.DEADLINE)
            _need(effect.action is not _t.ActionKind.CLICK or not effect.arguments, _t.Reason.SCHEMA)
            for descriptor in effect.arguments:
                spec = next((s for s in components.slots if s.name == descriptor.slot_name), None)
                _need(spec is not None and descriptor.episode_id == inputs._episode
                      and descriptor.scope == scope, _t.Reason.FOREIGN)
                _need(any(descriptor is d for _, d in inputs._descriptors + inputs._keys), _t.Reason.FOREIGN)
                _check(inputs._owners.check_slot(descriptor, spec, inputs._episode))
                if effect.action is _t.ActionKind.KEY:
                    symbols = [k for k, d in inputs._keys if d is descriptor]
                    _need(len(symbols) == 1 and descriptor.key_vocabulary is not None
                          and symbols[0] in descriptor.key_vocabulary.symbols, _t.Reason.FOREIGN)
                    _need(type(symbols[0]) is str and symbols[0].isascii() and bool(symbols[0])
                          and spec.minimum <= len(symbols[0].encode('utf-8')) <= spec.maximum, _t.Reason.BOUNDS)
                    _check(inputs._owners.check_key(symbols[0], descriptor))
        if node.wait is not None:
            _predicate(node.wait.predicate, components.queries, scope, inputs, admission, session)
            profiles = [p for literal, p in inputs._waits
                        if _binding(inputs._predicates, literal) is node.wait.predicate]
            _need(len(profiles) == 1, _t.Reason.FOREIGN)
            profile, budget = profiles[0], node.wait.budget
            _need(_dc.replace(budget, semantic_wait_ms=profile.semantic_wait_ms) == profile
                  and budget.semantic_wait_ms <= profile.semantic_wait_ms
                  and budget.acquisition_attempts <= limits.max_acquisitions, _t.Reason.BOUNDS)
            _need(admission._now() + budget.semantic_wait_ms * 1000000 <= budget.absolute_deadline_ns <= authority_deadline,
                  _t.Reason.DEADLINE)
        if node.assertion is not None:
            _predicate(node.assertion.predicate, components.queries, scope, inputs, admission, session)
        if node.harvest is not None:
            harvest = node.harvest
            _need(harvest.result.name not in written, _t.Reason.CONFLICT)
            written.add(harvest.result.name)
            rows = [r for r in inputs._harvests if r[0] == harvest.result.name]
            _need(len(rows) == 1 and rows[0][1] is harvest.result and rows[0][2] is harvest.authorization
                  and rows[0][3].name == harvest.query_id and rows[0][4] == harvest.scope
                  and any(q is rows[0][3] for q in found), _t.Reason.FOREIGN)
            _need(harvest.max_bytes <= admission._slots.harvest_result_bytes, _t.Reason.BOUNDS)
            _check(inputs._owners.check_harvest(harvest))
    _need(all(any(s is r[1] for r in inputs._harvests) for s in components.symbols), _t.Reason.FOREIGN)


def _configuration(admission):
    _need(type(admission) is _h.SourceAdmission, _t.Reason.TYPE)
    for name, cls in (('_source', _t.SourcePolicy), ('_parser', _t.ParserPolicy),
                      ('_slots', _t.SlotPolicy), ('_resource', _t.ResourcePolicy)):
        value = getattr(admission, name)
        _need(value is not None, _t.Reason.MISSING)
        _need(type(value) is cls, _t.Reason.TYPE)
        _t._Record.__post_init__(value)


def _work(operation, *, session, admission, inputs, registry=None):
    _configuration(admission)
    admission._lifetime(session)
    _need(type(inputs) is CompilerInputs and inputs._allocation is not None
          and type(inputs._work_bytes) is int and inputs._work_bytes > 0, _t.Reason.MISSING)
    token = inputs._allocation.reserve(inputs._work_bytes, session._header.limits.absolute_deadline_ns)
    _need(type(token) is _t.RegistryRef, _t.Reason.TYPE)
    if registry is not None:
        registry._recovery.append((inputs._allocation, token))
    result = None
    try:
        _check(inputs._allocation.check(token, inputs._work_bytes, session._header.limits.absolute_deadline_ns))
        _inputs(inputs, admission, session)
        result = operation()
        return result
    finally:
        try:
            _check(inputs._allocation.release(token))
        except Exception:
            if registry is not None and type(result) is _t.ValidatedPlan:
                registry._revoke(result)
            raise
        if registry is not None:
            registry._recovery.remove((inputs._allocation, token))


def _definition(definition, session, admission, inputs):
    _need(type(definition) is _t.PlanDefinition, _t.Reason.TYPE)
    before = _snapshot(definition, admission._lifetime(session).grant.charged_bytes)
    _walk(definition, admission, session)
    _need(definition.header == session._header, _t.Reason.FOREIGN)
    _validate(definition, session._header, inputs, admission, session)
    _, canonical = _h.verify_pins(definition, session=session, admission=admission,
                                 screen_check=inputs._owners.check_screen)
    _need(_snapshot(definition, admission._lifetime(session).grant.charged_bytes) == before, _t.Reason.STALE)
    return canonical


# @anchor:wa.scripts.compiler.canonical_plan
@_closed
def canonical_plan(definition, *, session, admission, inputs):
    return _work(lambda: _definition(definition, session, admission, inputs),
                 session=session, admission=admission, inputs=inputs)


# @anchor:wa.scripts.compiler.validate_plan
@_closed
def validate_plan(definition, *, session, admission, inputs, registry):
    _need(type(registry) is CompilerRegistry, _t.Reason.TYPE)
    def validate():
        return _issue(definition, session, admission, inputs, registry)
    plan = None
    try:
        plan = _work(validate, session=session, admission=admission, inputs=inputs, registry=registry)
        registry.check(plan, registry._compiler, plan.identity, plan.scope, plan.abis)
        return plan
    except Exception:
        # Publication stays charged if authenticated release fails.
        if plan is not None:
            registry._revoke(plan)
        raise


# @anchor:wa.scripts.compiler.verify_pairing
@_closed
def verify_pairing(plan, *, session, admission, inputs, registry):
    _need(type(registry) is CompilerRegistry and type(plan) is _t.ValidatedPlan, _t.Reason.TYPE)
    def verify():
        _definition(plan.definition, session, admission, inputs)
        _walk(plan, admission, session)
        return _h.verify_registered_plan(plan, compiler=registry._compiler, registry=registry,
            session=session, admission=admission, screen_check=inputs._owners.check_screen)
    accepted = _work(verify, session=session, admission=admission, inputs=inputs, registry=registry)
    registry.check(accepted, registry._compiler, accepted.identity, accepted.scope, accepted.abis)
    return accepted


# @anchor:wa.scripts.compiler.compile_draft
@_closed
def compile_draft(source, *, admission, inputs, registry):
    _need(type(registry) is CompilerRegistry, _t.Reason.TYPE)
    _configuration(admission)
    session, plan = None, None
    try:
        session = _h.admit_source(source, admission=admission)
        def compile_():
            checked = _checked(session, admission, inputs)
            parts = _lower(checked, session._header, inputs, admission, session)
            _validate(_types.SimpleNamespace(**parts), session._header, inputs, admission, session)
            identity, _ = _h.canonical_identity(**parts, session=session, admission=admission,
                                               screen_check=inputs._owners.check_screen)
            definition = _t.PlanDefinition(header=session._header, identity=identity,
                                          source_digest=identity.source_digest, **parts)
            return _issue(definition, session, admission, inputs, registry)
        plan = _work(compile_, session=session, admission=admission, inputs=inputs, registry=registry)
    finally:
        if session is not None:
            try:
                _check(admission.finish(session))
            except Exception:
                if plan is not None:
                    registry._revoke(plan)
                raise
    try:
        registry.check(plan, registry._compiler, plan.identity, plan.scope, plan.abis)
    except Exception:
        registry._revoke(plan)
        raise
    return plan
