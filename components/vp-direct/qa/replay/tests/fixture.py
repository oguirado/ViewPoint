"""Offline fixture ports and crash peers for the committed replay subjects."""

from hashlib import sha256
import json
import os
from pathlib import Path
from random import Random
import subprocess
import sys
from time import monotonic_ns
from types import SimpleNamespace

SOURCE = Path(os.environ["REP_SOURCE"]).resolve(strict=True)
DIRECT = SOURCE / "components/vp-direct"
sys.path[:0] = [str(DIRECT), str(SOURCE / "agents"), str(SOURCE / "components/wender-agent"),
                str(SOURCE / "components/blender-visual-qa"), str(DIRECT / "vendor/broker")]

from bus.broker import Broker
from bus.journal import Journal
from bus.journal.journal import TRANSITION
from perception.lifecycle import PerceptionLifecycle, RecoverableDemandCoordinator, RuntimeEvidence
from perception.receipts import append_transition
from perception.stream import PerceptionStream
from perception.stream.tests.test_stream import put_screen
from rewind.machine import GoalMachine
from schemas.validation import validate_contract
from vadapt import x11_ramcap

ALIASES = ("cdp:tab", "x11:window", "atspi:object")
PIXELS = b"red" * 32


def canonical_line(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8") + b"\n"


def digest(data):
    return sha256(data).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def append_effect(path, operation):
    with path.open("ab") as ledger:
        ledger.write(canonical_line(operation))
        ledger.flush()
        os.fsync(ledger.fileno())


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"{name}_v1.valid.json").read_bytes())


def start_goal(cls, path, binding, *, steps, observation):
    checkpoint = example("vp.rewind_receipt")
    checkpoint.update(goal_id="goal", capsule_id=binding["surface_id"], op="checkpoint", confirmed_tick=0)
    checkpoint["budget"].update(attempts_used=0, attempts_limit=10, rewinds_used=0, rewinds_limit=3)
    return cls.create(path, goal_id="goal", session_id="session", binding=binding, checkpoint=checkpoint,
                      score=100, steps=steps, duplicates=3, epsilon=1, action_quantum="0.01",
                      observation_id=observation, generation=1)


class LifecycleFixture:
    def __init__(self, root, cleanup):
        self.root = root
        self.cleanup = cleanup
        self.binding = example("vp.surface_binding")
        self.agent = dict(example("vp.demand"), demand_id="agent", consumer=self.binding["owner"],
                          purpose="agent-perception")
        self.recorder = dict(self.agent, demand_id="recorder", consumer="recorder", purpose="recording")
        (root / "publisher.json").write_bytes(canonical_line([self.binding, self.recorder]))
        self.broker = cleanup.enter_context(Broker(root / "broker"))
        self.broker.bind("session", self.binding, ALIASES, lease_state="AGENT")
        self.open_demand()
        put_screen(root / "screen", PIXELS)
        config = x11_ramcap.SourceConfig(str(root), "screen", 32, 1, "synthetic fixture")
        self.stream = PerceptionStream(
            self.binding, config, ring_slots=2, interval_ns=1000, ttl_ns=10**11,
            max_frame_bytes=4096, max_ring_bytes=8192, workspace_root=root / "rings")
        cleanup.callback(self.stream.close)
        self.journal = Journal(root)
        self.publications = []
        self.observe = self.publish
        self.drain = lambda: True
        self.provision()
        self.initial = self.call("wake")
        self.goal = start_goal(GoalMachine, root / "goal", self.binding, steps=2,
                               observation=self.initial["fresh_observation_id"])
        self.lifecycle.bind_goal(self.goal)

    def open_demand(self):
        self.demand = RecoverableDemandCoordinator(
            self.root / "demand.sqlite3", self.binding, interests=["VERIFIED"],
            authorize=lambda row: row in (self.agent, self.recorder), drain=lambda: True)
        self.cleanup.callback(self.demand.retire_owner)

    def reopen(self, **options):
        self.demand.retire_owner()
        self.broker.close()
        self.broker = self.cleanup.enter_context(Broker(self.root / "broker"))
        self.open_demand()
        self.goal = GoalMachine(self.root / "goal", "goal")
        self.journal = Journal(self.root)
        self.provision(recover_goal=self.goal, **options)

    def provision(self, **options):
        self.lifecycle = PerceptionLifecycle(
            self.broker, "session", self.binding, self.demand, self.agent, self.stream,
            qualified=lambda row: row == self.binding,
            runtime=lambda: RuntimeEvidence(self.binding["incarnation"], (), 40),
            drain_agent=lambda _: self.drain(),
            on_transition=lambda receipt, proof: self.observe(receipt, proof), **options)

    def publish(self, receipt, proof):
        ref = append_transition(self.journal, receipt, proof, agent="broker")
        self.publications.append((receipt, proof, ref))

    def call(self, op):
        result = self.lifecycle.vp_perception_call(dict(op=op, reason="rep fixture", session_id="session"))
        validate_contract(f"vp_perception_call.{op}.result/v1", result)
        return result

    def broker_states(self):
        return [row["perception"]["state"] for row in rows(self.root / "broker/events.jsonl")
                if "perception" in row]

    def ledger(self):
        return self.goal.replay()


def journal_request(sequence):
    payload = dict(example("vp.transition_receipt"), acks=["identity", "capability", "stream"])
    return dict(agent="broker", op_id=f"rep-seed-{sequence}", kind=TRANSITION, payload=payload)


def crash_journal(root, seed):
    class InterruptedJournal(Journal):
        @staticmethod
        def _write(*args):
            Journal._write(*args)
            os._exit(79)

    InterruptedJournal(root).append(**journal_request(seed))


def crash_broker(root, seed):
    alias = Random(seed).choice(ALIASES)
    broker = Broker(root / "broker")
    broker.enqueue("owner", "effect", alias, "inc", 1)
    broker.dispatch("owner", broker.try_acquire("owner", alias, "inc", 1, "effect"))
    append_effect(root / "external.jsonl", {"request": "effect", "alias": alias})
    os._exit(77)


def crash_publisher(root, seed):
    binding, recorder = json.loads((root / "publisher.json").read_bytes())
    source = RecoverableDemandCoordinator(root / "demand.sqlite3", binding, interests=["VERIFIED"],
                                          authorize=lambda row: row == recorder, drain=lambda: True)
    with source.resume_subscription(recorder) as handle:
        source.publish(handle, 1, f"recorded-{seed}")
        os._exit(78)


def crash_delivery(root, seed):
    append_effect(root / "deliveries.jsonl", {"reference": f"recorded-{seed}", "flight": 1})
    os._exit(75)


def loop_fixture(root, *, create, crash=False, witness=False):
    # Reuse the repository's MemoryWeb corpus and synthetic transport builder.
    from loop import LoopGoal, ProjectedOwners, ReplayLoop
    from loop.tests.test_loop import API, fixture as mw_fixture
    from metaviewer_agents.mw_owner import Corpus, ReplayOwner
    from recorder.store.memoryweb_store import MemoryWebStore

    class Endpoint:
        def __init__(self):
            self.calls = []
            self.owner = SimpleNamespace(backend=SimpleNamespace())

        @staticmethod
        def fail(stage):
            raise AssertionError(f"unexpected fixture transport call {stage}")

    endpoint = Endpoint()
    mw_fixture.OwnerTests.install_backend(endpoint)
    backend = endpoint.owner.backend
    original_call = backend.call
    external = root / "external.jsonl"
    if external.exists():
        calls = rows(external)
        endpoint.focused = ("Operator Name", "Submit Greeting")[int(calls[-1]["x"] > 100)]
        endpoint.generation += len(calls)

    def record(stage, **args):
        reply = original_call(stage, **args)
        if stage == "propose":
            action = args["plan"]["action"]
            append_effect(external, {"seq": len(rows(external)) + 1 if external.exists() else 1,
                                     "x": action["x"]})
            if crash and len(rows(external)) == 2:
                os._exit(76)
        return reply

    backend.call = record
    base = root / "base"
    if create:
        mw_fixture.make_checkout(base, API)
        workflow_path = base / "sites/fixture/workflows/form.json"
        workflow = Corpus(base, "fixture/form", API).workflow
        workflow["steps"] = workflow["steps"][:2]
        workflow_path.write_bytes(canonical_line(workflow))
    corpus = Corpus(base, "fixture/form", API)
    parent = ReplayOwner(backend, corpus, root / "run.json",
                         notice=dict(mode="replay", network="offline fixtures",
                                     acknowledged=True, mono_ns=monotonic_ns()))
    owners = ProjectedOwners(parent, root,
                             {"EXPLANATION.md": (base / "sites/fixture/EXPLANATION.md").read_bytes()})
    binding = dict(example("vp.surface_binding"), surface_id="owned")
    session = dict(example("vp.session"), session_id="session", capsule_id="owned", display="xpra:7",
                   incarnation=binding["incarnation"],
                   lease=dict(holder=binding["owner"], authority_epoch=1))
    store = MemoryWebStore(root)
    if create:
        goal = start_goal(LoopGoal, root / "goal", binding, steps=2, observation="active")
        revision = store.save_workflow(canonical_line(corpus.workflow))["workflow_sha256"]
    else:
        goal = LoopGoal(root / "goal", "goal")
    def human_needed(_owner, session_id, goal_id, _reason):
        return dict(by="broker", rec="HUMAN_NEEDED", seq=1, session_id=session_id, goal_id=goal_id)

    lifecycle = SimpleNamespace(session_id="session", binding=binding,
                                broker=SimpleNamespace(human_needed=human_needed),
                                vp_perception_call=lambda _: dict(complete=True, to="ACTIVE",
                                                                  fresh_observation_id="active"),
                                run_agent=lambda kind, obs, perform: perform())
    loop = ReplayLoop(goal, store, lifecycle, open_owner=owners, authorize=lambda *_: "preparar",
                      vision=None, assess=None, checkpoint=None, restore=None, session_for=lambda _: session,
                      effect_witness=(lambda _, step: step["verify"]["predicate"]) if witness else None)
    if create:
        loop.bind("fixture", "form", revision)
    return loop


def crash_loop(root, _seed):
    loop = loop_fixture(root, create=True, crash=True)
    if loop.advance() != "VERIFIED":
        raise AssertionError("prefix did not verify before the crash")
    (root / "prefix.json").write_bytes(canonical_line(loop.goal.replay()))
    loop.advance()


def peer(name, root, seed, exitcode):
    proc = subprocess.run([sys.executable, "-I", "-B", __file__, name, str(root), str(seed)],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != exitcode:
        raise AssertionError(f"{name}: child rc={proc.returncode}, wanted {exitcode}\n{proc.stdout}{proc.stderr}")


if __name__ == "__main__":
    scenario, path, raw_seed = sys.argv[1:]
    {
        "journal": crash_journal,
        "broker": crash_broker,
        "publisher": crash_publisher,
        "delivery": crash_delivery,
        "loop": crash_loop,
    }[scenario](Path(path), int(raw_seed))
