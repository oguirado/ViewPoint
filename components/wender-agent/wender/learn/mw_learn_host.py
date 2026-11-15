# @module {"id":"wa.learn.mw_learn_host","layer":4,"owner":"LEARN","deps":["wa.learn.mw_learn","wa.learn.mw_profiles","wa.learn.demonstrations","wa.perceive.srf_identity","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Local draft host. Provisioning is reviewed data, never executable policy.

mw.learn_host.v1 has no implicit budgets or declarations. recorded_date is the
operator-supplied recording date, never the invocation date. Semantic capability
registrations map reviewed IDs to exact closed predicate objects. Unsupported
focus/drag/ordering evidence refuses; draft admission grants no promotion review.
"""
from dataclasses import dataclass, fields, replace
from pathlib import Path
import hashlib
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time

from tools import memoryweb_gate as gate
from wender.learn import demonstrations as demos, mw_learn as learn, mw_profiles as profiles
from wender.perceive.srf_identity import BINARY


# @anchor:wa.s4.mw_learn_host.HostConfiguration
@dataclass(frozen=True)
class HostConfiguration:
    path: Path
    identity: tuple
    profile: object
    profile_path: Path
    profile_identity: tuple
    acknowledgement: tuple
    memoryweb_root: Path
    site: bytes
    execution_policy: bytes
    postconditions: bytes
    protected: tuple
    slot_templates: bytes
    semantic_capabilities: bytes
    recorded_date: str
    policy: learn.LearnPolicy
    capacity_bytes: int
    probe_bytes: int
    probe_ns: int
    store_bytes: int
    router_root: Path | None
    router_query: str | None
    ve_root: Path | None


# @anchor:wa.s4.mw_learn_host.RecorderIntake
@dataclass(frozen=True)
class RecorderIntake:
    root: Path
    root_identity: tuple
    identities: tuple
    files: tuple
    projection: bytes
    projection_sha256: str
    explanation: bytes
    explanation_identity: tuple
    deadline: int
    symbolic_slots: tuple
    manifest: bytes


# @anchor:wa.s4.mw_learn_host.ProducerEvidence
@dataclass(frozen=True)
class ProducerEvidence:
    executable: str
    executable_identity: tuple
    output_sha256: str
    operations: frozenset
    observed_ns: int
    deadline: int
    cleaned: bool


# @anchor:wa.s4.mw_learn_host.HostResources
class HostResources:
    # Strict output custody belongs to the production host, not legacy owners.
    private_output = True

    # @anchor:wa.s4.mw_learn_host.HostResources.__init__
    def __init__(self, capacity):
        learn._need(type(capacity) is int and capacity > 0, 'CAPACITY', 2)
        self.capacity, self.reserved, self.overhead = capacity, 0, 0
        self.lock = threading.Lock()
        self.children, self.allocations, self.transients = set(), set(), set()
        self.cancelled = False
        self.deadline = None

    # @anchor:wa.s4.mw_learn_host.HostResources.reserve
    def reserve(self, amount):
        with self.lock:
            if self.cancelled or self.reserved or type(amount) is not int or not 0 < amount <= self.capacity - self.overhead:
                return gate.GateResult(2, 'CAPACITY')
            self.reserved = amount + self.overhead
            return gate.GateResult(0, 'PASS')

    # @anchor:wa.s4.mw_learn_host.HostResources.release
    def release(self):
        with self.lock:
            learn._need(not self.children and not self.allocations, 'DRAIN')
            self.reserved = 0

    # @anchor:wa.s4.mw_learn_host.HostResources.drain
    def drain(self, timeout_ns):
        end = time.monotonic_ns() + timeout_ns
        for child in tuple(self.children):
            try:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=max(0, (end - time.monotonic_ns()) / 2e9))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=max(0, (end - time.monotonic_ns()) / 1e9))
                self.children.remove(child)
            except (OSError, subprocess.TimeoutExpired):
                return gate.GateResult(5, 'DRAIN')
        return gate.GateResult(5, 'DRAIN') if self.allocations else gate.GateResult(0, 'PASS')


# @anchor:wa.s4.mw_learn_host._hash
def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


# @anchor:wa.s4.mw_learn_host._metadata
def _metadata(path):
    st = Path(path).lstat()
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


# @anchor:wa.s4.mw_learn_host._configuration_file
def _configuration_file(path):
    path = Path(path).absolute()
    learn._private(path, directory=False)
    learn._need(not any((parent / '.git').exists() for parent in path.parents), 'HOST_CONFIGURATION', 2)
    with profiles._guard(path):
        return learn._read(path, 65536)


# @anchor:wa.s4.mw_learn_host.load_host_configuration
def load_host_configuration(site, *, configuration_path, profile):
    """Load the closed, private, non-git provisioning contract."""
    try:
        learn._need(type(profile) is profiles.Profile and profile.site == site, 'HOST_CONFIGURATION', 2)
        learn._need(Path(configuration_path).exists(), 'HOST_CONFIGURATION', 2)
        raw, identity = _configuration_file(configuration_path)
        value = gate.strict_load(raw)
        keys = {'schema', 'memoryweb_root', 'site', 'execution_policy', 'postconditions', 'protected',
                'slot_templates', 'semantic_capabilities', 'recorded_date', 'policy', 'demo_limits',
                'capacity_bytes', 'probe_bytes', 'probe_ns', 'store_bytes', 'router_root', 'router_query', 've_root'}
        learn._need(type(value) is dict and set(value) == keys and value['schema'] == 'mw.learn_host.v1', 'HOST_CONFIGURATION', 2)
        defs = gate.compile_bundle(gate.strict_load((gate.DOCS / 'mw.v1.schema.json').read_bytes()))
        learn._need(gate.conforms(value['site'], defs['site'], defs) and value['site']['id'] == site
                    and value['site']['status'] == 'draft', 'HOST_CONFIGURATION', 2)
        learn._need(gate.conforms(value['execution_policy'], defs['execution_policy'], defs)
                    and value['site']['execution_policy'] == value['execution_policy']
                    and type(value['postconditions']) is list and value['postconditions']
                    and all(gate.conforms(p, defs['predicate'], defs) for p in value['postconditions']), 'HOST_CONFIGURATION', 2)
        learn._need(type(value['protected']) is list and all(type(n) is int and n > 0 for n in value['protected'])
                    and len(set(value['protected'])) == len(value['protected']), 'HOST_CONFIGURATION', 2)
        from datetime import date
        date.fromisoformat(value['recorded_date'])
        registrations = value['semantic_capabilities']
        learn._need(type(registrations) is dict and not (set(registrations) & {'paste', 'type-stdin', 'scroll', 'drag', 'downloads'})
                    and all(type(k) is str and k and type(ps) is list
                    and all(gate.conforms(p, defs['predicate'], defs) and p['capability_id'] == k for p in ps)
                    for k, ps in registrations.items()), 'HOST_CONFIGURATION', 2)
        learn._need(type(value['slot_templates']) is list, 'HOST_CONFIGURATION', 2)
        learn._slots([dict(t, max_length=t.get('max_length', 4096)) for t in value['slot_templates']])
        demo_values, policy_values = value['demo_limits'], value['policy']
        learn._need(set(demo_values) == {f.name for f in fields(demos.DemoLimits)} and
                    all(type(v) is int and v > 0 for v in demo_values.values()), 'HOST_CONFIGURATION', 2)
        learn._need(set(policy_values) == {f.name for f in fields(learn.LearnPolicy)} - {'demo_limits', 'effort'}
                    and all(type(v) is int and v > 0 for v in policy_values.values()), 'HOST_CONFIGURATION', 2)
        policy = learn.LearnPolicy(**policy_values, demo_limits=demos.DemoLimits(**demo_values), effort='medium')
        for k in ('capacity_bytes', 'probe_bytes', 'probe_ns', 'store_bytes'):
            learn._need(type(value[k]) is int and value[k] > 0, 'HOST_CONFIGURATION', 2)
        learn._need(value['probe_bytes'] <= 65536, 'HOST_CONFIGURATION', 2)
        root = Path(value['memoryweb_root'])
        learn._need(root.is_absolute(), 'HOST_CONFIGURATION', 2)
        learn._private_tree(root)
        for k in ('router_root', 've_root'):
            learn._need(value[k] is None or (type(value[k]) is str and Path(value[k]).is_absolute()), 'HOST_CONFIGURATION', 2)
        learn._need(value['router_query'] is None or (type(value['router_query']) is str and
                    len(value['router_query'].encode()) <= 512), 'HOST_CONFIGURATION', 2)
        profile_path = Path(configuration_path).parent / (site + '.json')
        # The runner binds the selected profile directory separately from an explicit configuration path.
        profile_identity = ()
        return HostConfiguration(Path(configuration_path), identity, profile, profile_path, profile_identity,
            profiles._token_digest(profile), root, learn._bytes(value['site']), learn._bytes(value['execution_policy']),
            learn._bytes(value['postconditions']), tuple(value['protected']), learn._bytes(value['slot_templates']),
            learn._bytes(registrations), value['recorded_date'], policy, value['capacity_bytes'],
            value['probe_bytes'], value['probe_ns'], value['store_bytes'],
            Path(value['router_root']) if value['router_root'] else None, value['router_query'],
            Path(value['ve_root']) if value['ve_root'] else None)
    except learn._Stop:
        raise
    except Exception:
        raise learn._Stop('HOST_CONFIGURATION', 2) from None


# @anchor:wa.s4.mw_learn_host._fence
def _fence(cancelled, deadline, clock):
    learn._need(cancelled() is False, 'CANCELLED')
    learn._need(clock() < deadline, 'DEADLINE')


# @anchor:wa.s4.mw_learn_host._probe
def _probe(configuration, resources, cancelled, deadline, clock):
    deadline = min(deadline, clock() + configuration.probe_ns)
    executable = str(Path(BINARY).absolute())
    learn._need(Path(executable).resolve() == Path(executable), 'PRODUCER')
    identity = _metadata(executable)
    learn._need(stat.S_ISREG(identity[2]) and identity[3] in (0, os.getuid())
                and not identity[2] & 0o022, 'PRODUCER')
    child = subprocess.Popen([BINARY, 'capabilities'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    resources.children.add(child)
    selector = selectors.DefaultSelector()
    stdout, stderr = bytearray(), bytearray()
    try:
        for stream in (child.stdout, child.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            _fence(cancelled, deadline, clock)
            for key, _ in selector.select(min(configuration.policy.poll_ns / 1e9, .05)):
                part = os.read(key.fd, min(65536, configuration.probe_bytes + 1))
                if not part:
                    selector.unregister(key.fileobj)
                elif key.fileobj is child.stdout:
                    stdout.extend(part)
                else:
                    stderr.extend(part)
                learn._need(len(stdout) + len(stderr) <= configuration.probe_bytes, 'PRODUCER')
        learn._need(child.wait(timeout=configuration.policy.drain_ns / 1e9) == 0 and not stderr, 'PRODUCER')
        raw = bytes(stdout)
        lines = raw.split(b'\n')
        learn._need(len(lines) == 3 and lines[0] == b'srf-identity-surface capabilities v1' and lines[2] == b'', 'PRODUCER')
        tokens = lines[1].split(b' ')
        known = {b'--paste': 'paste', b'--type-stdin': 'type-stdin', b'--scroll': 'scroll', b'--drag': 'drag', b'downloads': 'downloads'}
        learn._need(tokens and len(set(tokens)) == len(tokens) and all(t in known for t in tokens), 'PRODUCER')
        learn._need(_metadata(executable) == identity, 'PRODUCER')
        observed = clock()
        answer = ProducerEvidence(executable, identity, _hash(raw), frozenset(known[t] for t in tokens), observed, deadline, True)
    finally:
        selector.close()
        for stream in (child.stdout, child.stderr):
            stream.close()
        learn._need(learn._pass(resources.drain(configuration.policy.drain_ns)), 'DRAIN')
    return answer


# @anchor:wa.s4.mw_learn_host._intake
def _intake(directory, explanation, workflow_id, profile, configuration, deadline, cancelled, clock):
    _fence(cancelled, deadline, clock)
    root = Path(directory).absolute()
    learn._private(root)
    learn._ram(root)
    root_identity = _metadata(root)
    learn._need((root / 'events.jsonl').is_file() and not (root / 'events.jsonl').is_symlink(), 'INPUT_MISSING', 2)
    raw, explanation_identity = learn._read(explanation, 16384)
    learn._need(learn._pass(gate.check_explanation(Path(explanation))), 'EXPLANATION', 2)
    recording = demos.load_demo(root, limits=configuration.policy.demo_limits, final_check='strict')
    learn._need(recording.binding[0] == profile.window, 'INPUT_BINDING', 3)
    identities = []
    for name in ['events.jsonl'] + [name for name, _ in recording.files]:
        _, identity = learn._read(root / name, max(configuration.policy.demo_limits.event_file, configuration.policy.demo_limits.png_bytes))
        identities.append(identity)
    segments = demos.segment_demo(recording)
    projection = demos.project_demo_steps(segments, demo_id=workflow_id, site_id=profile.site)
    slots = tuple((seq, sid, next(e.text_len for e in recording.events if e.seq == seq)) for seq, sid in projection.text_slots)
    learn._need(all(type(n) is int and n >= 0 for _, _, n in slots), 'SLOT_SCHEMA', 2)
    provenance = dict(source='human', tier=1, recorded=configuration.recorded_date,
                      source_ref=f'demos/{workflow_id}/EXPLANATION.md', run_id=None, quote_sha256=_hash(raw),
                      receipt_sha256=None, derived_from_sha256=None)
    manifest = dict(schema='mw.demo.v1', demo_id=workflow_id, site_id=profile.site,
        recorded_date=configuration.recorded_date,
        surface=dict(viewport_px=list(recording.binding[1:]), ax_enabled=False, auth_boundary='operator_capsule'),
        wire_normalization=dict(sparse_rows_normalized=True, filtered_noise_keys=[], frame_storage='ram_only'),
        explanation=dict(file='EXPLANATION.md', sha256=_hash(raw), quote_tier=1, redaction='reviewed_original', source_explanation_sha256=None),
        events_summary=dict(total_events=len(recording.events), segments_count=len(segments),
            duration_ms=(recording.events[-1].mono_ns - recording.events[0].mono_ns) // 1000000,
            text_slots=[dict(seq=seq, slot_id=sid, text_len=n) for seq, sid, n in slots]),
        steps_file='steps.json', steps_sha256=_hash(projection.payload), provenance=provenance)
    defs = gate.compile_bundle(gate.strict_load((gate.DOCS / 'mw.v1.schema.json').read_bytes()))
    learn._need(gate.conforms(manifest, defs['demo'], defs), 'INPUT_SCHEMA', 2)
    del segments
    _fence(cancelled, deadline, clock)
    return RecorderIntake(root, root_identity, tuple(identities), recording.files, projection.payload,
                         _hash(projection.payload), raw, explanation_identity, deadline, slots, learn._bytes(manifest))


# @anchor:wa.s4.mw_learn_host._snapshot
def _snapshot(configuration, resources):
    root = configuration.memoryweb_root
    learn._private(root)
    records, total, pending = [], 0, [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path in resources.transients:
                    continue
                relative = str(path.relative_to(root))
                # Charge metadata as well as bytes: empty-directory floods are bounded.
                total += len(relative.encode('utf-8')) + 256
                learn._need(total <= configuration.store_bytes, 'CAPACITY', 2)
                if entry.is_dir(follow_symlinks=False):
                    learn._private(path)
                    pending.append(path)
                    if path != root / 'sites':
                        records.append([relative, _metadata(path)[:4]])
                else:
                    learn._private(path, directory=False)
                    raw, identity = learn._read(path, configuration.store_bytes - total)
                    total += len(raw)
                    learn._need(total <= configuration.store_bytes, 'CAPACITY', 2)
                    records.append([relative, identity[2], identity[3]])
    records.sort(key=lambda row: row[0])
    return _hash(learn._bytes(records))


# @anchor:wa.s4.mw_learn_host.build_learn_owners
def build_learn_owners(*, profile, configuration, intake, producer, resources, cancelled, clock):
    learn._need(profile == configuration.profile, 'HOST_CONFIGURATION', 2)
    if not configuration.profile_identity:
        configuration = replace(configuration, profile_identity=learn._read(configuration.profile_path, 16384)[1])
    slots = learn.build_text_slots(symbolic_slots=intake.symbolic_slots, templates=gate.strict_load(configuration.slot_templates))
    # Native evidence can attest operations only, independently of semantic registrations.
    required_ops = {row['kind'] for row in gate.strict_load(intake.projection)['steps']} & {'paste', 'scroll', 'drag'}
    required_ops |= {'paste' if s['transport'] == 'clipboard_paste' else 'type-stdin' for s in slots}
    learn._need(required_ops <= producer.operations, 'CAPABILITY_MISSING')
    constraints = learn._bytes(dict(execution_policy=gate.strict_load(configuration.execution_policy),
        postconditions=gate.strict_load(configuration.postconditions), protected=list(configuration.protected), text_slots=slots))
    scope = _hash(learn._bytes([profile.site, profile.capsule, profile.container, profile.window, configuration.profile_identity]))
    authority = _hash(learn._bytes([scope, configuration.identity, configuration.acknowledgement]))
    revision = _hash(learn._bytes([authority, _snapshot(configuration, resources)]))
    registrations = gate.strict_load(configuration.semantic_capabilities)
    manifest = gate.strict_load(intake.manifest)
    workflow_id = manifest['demo_id']
    names = {'site.json', 'SITE.md', f'workflows/{workflow_id}.json', f'demos/{workflow_id}/manifest.json',
             f'demos/{workflow_id}/steps.json', f'demos/{workflow_id}/EXPLANATION.md'}
    destinations = {f'sites/{profile.site}/{n}' for n in names}

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.fresh
    def fresh():
        _fence(cancelled, intake.deadline, clock)
        learn._need(producer.cleaned is True and producer.observed_ns <= clock() < producer.deadline
                    and _metadata(producer.executable) == producer.executable_identity, 'PRODUCER')
        learn._need(profiles.load_profile(profile.site, profile_dir=configuration.profile_path.parent) == profile
                    and profiles._token_digest(profile) == configuration.acknowledgement, 'REVISION', 3)
        learn._need(_configuration_file(configuration.path)[1] == configuration.identity
                    and learn._read(configuration.profile_path, 16384)[1] == configuration.profile_identity, 'REVISION', 3)
        learn._need(_metadata(intake.root) == intake.root_identity, 'INPUT_CHANGED', 3)
        for identity in (*intake.identities, intake.explanation_identity):
            learn._need(learn._read(identity[0], identity[1])[1] == identity, 'INPUT_CHANGED', 3)
        learn._need(_hash(learn._bytes([authority, _snapshot(configuration, resources)])) == revision, 'REVISION', 3)

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.revision_check
    def revision_check(expected):
        try:
            learn._need(expected == revision, 'REVISION', 3)
            fresh()
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(3, 'REVISION')

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.capability_check
    def capability_check(obj, predicate):
        try:
            fresh()
            required = obj.get('required_capabilities', [obj['capability_id']] if 'capability_id' in obj else [])
            supported = producer.operations | frozenset(registrations)
            learn._need(required and all(r in supported for r in required), 'CAPABILITY_MISSING')
            if predicate is not None:
                learn._need(predicate in registrations.get(predicate['capability_id'], []), 'CAPABILITY_MISSING')
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(5, 'CAPABILITY_MISSING')

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.evidence_check
    def evidence_check(kind, artifact, related):
        try:
            fresh()
            learn._need(kind == 'provenance', 'EVIDENCE_DENIED')
            p = artifact['provenance']
            learn._need(p['source'] == 'human' and (p['source_ref'] == f'demos/{workflow_id}/EXPLANATION.md'
                        or (artifact.get('schema') == 'mw.site.v1' and p['source_ref'] == 'SITE.md'))
                        and p['quote_sha256'] == _hash(intake.explanation), 'EVIDENCE_DENIED')
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(5, 'EVIDENCE_DENIED')

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.review_check
    def review_check(content, purpose):
        return gate.GateResult(5, 'REVIEW_DENIED')

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.context_factory
    def context_factory(root):
        root = Path(root).absolute()
        parent = root.parent
        if parent.parent == configuration.memoryweb_root / 'sites' and parent.name.startswith('.mw-staging-'):
            learn._private(parent)
            resources.transients.add(parent)
        # @anchor:wa.s4.mw_learn_host.build_learn_owners.bound_evidence
        def bound_evidence(kind, artifact, related):
            if kind == 'provenance' and artifact.get('provenance', {}).get('source_ref') == 'SITE.md':
                try:
                    learn._need(learn._read(root / 'SITE.md', 8192)[0] == intake.explanation, 'EVIDENCE_DENIED')
                except Exception:
                    return gate.GateResult(5, 'EVIDENCE_DENIED')
            return evidence_check(kind, artifact, related)
        return gate.ValidationContext(root=root, capability_check=capability_check, evidence_check=bound_evidence,
                                      review_check=review_check)

    # @anchor:wa.s4.mw_learn_host.build_learn_owners.disclosure
    def disclosure(digest, given_scope, given_authority, destination, purpose, given_revision):
        try:
            fresh()
            learn._need((given_scope, given_authority, given_revision) == (scope, authority, revision), 'PRIVACY')
            learn._need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest), 'PRIVACY')
            learn._need((destination == 'muse' and purpose == 'author' and resources.reserved > 0) or
                        (destination in destinations and purpose == 'publish'), 'PRIVACY')
            return gate.GateResult(0, 'PASS')
        except Exception:
            return gate.GateResult(5, 'PRIVACY')

    return learn.LearnOwners(scope, authority, revision, constraints, context_factory, disclosure,
                             resources, revision_check, cancelled)


# @anchor:wa.s4.mw_learn_host.run_learn_host
def run_learn_host(*, profile_name, demo_directory, explanation, workflow_id, profile_dir=None,
                   configuration_path=None, backend=None, describer=None, clock=time.monotonic_ns):
    resources, handlers = None, {}
    result = learn.LearnResult(5, 'HOST_REFUSED')
    cancelled = False
    start = clock()

    # @anchor:wa.s4.mw_learn_host.run_learn_host.interrupted
    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = True
        if resources is not None:
            resources.cancelled = True

    try:
        learn._id(profile_name)
        learn._id(workflow_id)
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, interrupted)
        directory = Path.home() / '.config/memoryweb/profiles' if profile_dir is None else Path(profile_dir)
        profile = profiles.load_profile(profile_name, profile_dir=directory)
        profiles._token_digest(profile)
        path = directory / (profile_name + '.learn.json') if configuration_path is None else Path(configuration_path)
        configuration = load_host_configuration(profile_name, configuration_path=path, profile=profile)
        profile_path = directory / (profile_name + '.json')
        configuration = replace(configuration, profile_path=profile_path,
                                profile_identity=learn._read(profile_path, 16384)[1])
        deadline = start + configuration.policy.deadline_ns
        _fence(lambda: cancelled, deadline, clock)
        resources = HostResources(configuration.capacity_bytes)
        resources.overhead = configuration.store_bytes * 8 + 20 * 65536
        resources.deadline = deadline
        # Intake and producer buffers reserve before child startup or pixel decoding.
        intake_peak = (configuration.policy.demo_limits.memory_bytes + configuration.policy.demo_limits.retained_bytes
                       + configuration.probe_bytes * 4 + configuration.policy.request_bytes * 20)
        learn._need(learn._pass(resources.reserve(intake_peak)), 'CAPACITY', 2)
        producer = _probe(configuration, resources, lambda: cancelled, deadline, clock)
        intake = _intake(demo_directory, explanation, workflow_id, profile, configuration, deadline, lambda: cancelled, clock)
        learn._need(learn._pass(resources.drain(configuration.policy.drain_ns)), 'DRAIN')
        owners = build_learn_owners(profile=profile, configuration=configuration, intake=intake, producer=producer,
                                    resources=resources, cancelled=lambda: cancelled, clock=clock)
        learn._policy(configuration.policy, owners)
        resources.release()
        parent = Path('/dev/shm/vqa-ramws')
        parent.mkdir(mode=0o700, exist_ok=True)
        learn._private(parent)
        learn._ram(parent)
        _fence(lambda: cancelled, deadline, clock)
        with tempfile.TemporaryDirectory(prefix='mw-host-', dir=parent) as workspace:
            workspace = Path(workspace)
            for name, raw in (('manifest.json', intake.manifest), ('site.json', configuration.site)):
                fd = os.open(workspace / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
            inputs = learn.LearnInputs(Path(demo_directory), Path(explanation), workspace / 'manifest.json',
                workspace / 'site.json', gate.DOCS / 'AUTHORING_GUIDE.md', gate.DOCS / 'mw.v1.schema.json',
                configuration.memoryweb_root, profile.site, workflow_id, configuration.router_root,
                configuration.router_query, configuration.ve_root)
            _fence(lambda: cancelled, deadline, clock)
            policy = replace(configuration.policy, deadline_ns=deadline - clock())
            selected_backend = backend if backend is not None else learn.AuthorBackend(workspace=workspace, effort='medium', timeout=policy.deadline_ns / 1e9)
            result = learn.learn(inputs, policy=policy, owners=owners, backend=selected_backend, describer=describer, clock=clock)
    except learn._Stop as exc:
        result = learn.LearnResult(exc.rc, exc.category)
    except profiles.ProfileError:
        result = learn.LearnResult(5, 'PROFILE_ACKNOWLEDGEMENT')
    except FileNotFoundError:
        result = learn.LearnResult(2, 'HOST_CONFIGURATION' if configuration_path is None else 'INPUT_MISSING')
    except (ValueError, OSError, TypeError, KeyError):
        result = learn.LearnResult(2, 'HOST_REFUSED')
    except Exception:
        result = learn.LearnResult(5, 'HOST_REFUSED')
    finally:
        if resources is not None:
            try:
                if not learn._pass(resources.drain(configuration.policy.drain_ns)):
                    result = replace(result, rc=5, category='DRAIN')
                resources.release()
            except Exception:
                result = replace(result, rc=5, category='DRAIN')
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return result
