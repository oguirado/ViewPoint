"""C9 / WP1-06 review model and offline pins, NOT a production harvest broker.

All targets, AX trees, submissions and extraction results are in memory. The
``fixture.ax`` method and ``question_id/generation/navigation`` event envelope
are test metadata, not CDP or MemoryWeb APIs. Logical step budgets are not wall
clock deadlines. See docs/spec/jq1_a6_multiwindow_harvest.md for adoption gates.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit

from wender.perceive import ax
from wender.perceive.cdp_pipe import CdpError
from wender.perceive.cdp_port import CdpPort


def _origin(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("fixture site requires an explicit HTTPS origin")
    return (p.scheme, p.hostname.lower(), p.port or 443)


@dataclass(frozen=True)
class _Site:
    name: str
    prefix: str
    open_url: str

    @property
    def lease_key(self):
        return _origin(self.prefix)

    def matches(self, target):
        try:
            return (target.get("type") == "page"
                    and _origin(target.get("url", "")) == self.lease_key
                    and target["url"].startswith(self.prefix))
        except ValueError:
            return False


SITES = (
    _Site("perplexity", "https://www.perplexity.ai/",
          "https://www.perplexity.ai/fixture-route?q=offline"),
    _Site("chatgpt", "https://chatgpt.com/",
          "https://chatgpt.com/fixture-route?q=offline"),
    _Site("gemini", "https://gemini.google.com/",
          "https://gemini.google.com/fixture-route?q=offline"),
)
INDEPENDENT = _Site("independent", "https://www.google.com/",
                    "https://www.google.com/search?fixture=offline")
SORRY_URL = "https://www.google.com/sorry/index"


def _targets():
    return [dict(targetId="t-" + s.name, type="page", url=s.prefix)
            for s in SITES] + [dict(targetId="t-independent", type="page",
                                   url=INDEPENDENT.open_url)]


def _node(nid, role, name="", children=()):
    return {"nodeId": nid, "role": {"value": role}, "name": {"value": name},
            "ignored": False, "childIds": list(children)}


def _answer_ax(text, complete=True, user_only=False):
    """Synthetic role/name rules; no claim these are live provider selectors."""
    children = ["user", "user-copy"]
    nodes = [_node("user", "heading", "User fixture prompt"),
             _node("user-copy", "button", "Copy fixture answer")]
    if not user_only:
        children += ["assistant", "answer", "answer-copy", "footer"]
        nodes += [_node("assistant", "heading", "Assistant fixture response"),
                  _node("answer", "article", "Answer region", ["paragraph"]),
                  _node("paragraph", "paragraph", children=["text"]),
                  _node("text", "StaticText", text),
                  _node("answer-copy", "button", "Copy fixture answer"),
                  _node("footer", "StaticText", "UI trailer must not be extracted")]
    if not complete:
        children.append("generating")
        nodes.append(_node("generating", "button", "Stop generating"))
    return [_node("root", "RootWebArea", "Fixture", children)] + nodes


def _fixture_complete(raw):
    by_id = {n["nodeId"]: n for n in raw}
    order = by_id.get("root", {}).get("childIds", [])
    names = [by_id[n]["name"]["value"] for n in order]
    if "Stop generating" in names:
        return False
    try:
        start = names.index("Assistant fixture response")
        end = names.index("Copy fixture answer", start + 1)
    except ValueError:
        return False
    return any(by_id[n]["role"]["value"] == "article"
               for n in order[start + 1:end])


def _fixture_extract(raw):
    """Extract only the fixture answer region from transient RAW AX."""
    if not _fixture_complete(raw):
        raise ValueError("answer not complete")
    by_id = {n["nodeId"]: n for n in raw}
    text, stack, seen = [], ["answer"], set()
    while stack:
        nid = stack.pop()
        if nid in seen:
            raise ValueError("cyclic fixture answer")
        seen.add(nid)
        n = by_id[nid]
        if n["role"]["value"] == "StaticText":
            text.append(n["name"]["value"])
        stack.extend(reversed(n.get("childIds", [])))
    answer = "\n".join(text)
    if not answer:
        raise ValueError("empty fixture answer")
    return answer


class _FakeCDP:
    """A strict command fake: construction cannot open a browser or socket."""

    def __init__(self, targets=None):
        self.targets = deepcopy(_targets() if targets is None else targets)
        self.sessions = {}
        self.created = 0
        self.attached = 0
        self.failures = {}
        self.send = Mock(side_effect=self._send)

    def _send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        failure = self.failures.get((method, session_id),
                                    self.failures.get(method))
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            return deepcopy(failure)
        if method == "Target.getTargets":
            return {"targetInfos": deepcopy(self.targets)}
        if method == "Target.createTarget":
            self.created += 1
            tid = "created-%d" % self.created
            self.targets.append(dict(targetId=tid, type="page", url=params["url"]))
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            assert params["flatten"] is True
            assert any(t["targetId"] == params["targetId"] for t in self.targets)
            self.attached += 1
            sid = "session-%d" % self.attached
            self.sessions[sid] = params["targetId"]
            return {"sessionId": sid}
        if method == "Target.detachFromTarget":
            assert params["sessionId"] in self.sessions
            del self.sessions[params["sessionId"]]
            return {}
        if method == "Target.closeTarget":
            assert any(t["targetId"] == params["targetId"] for t in self.targets)
            self.targets = [t for t in self.targets if t["targetId"] != params["targetId"]]
            return {"success": True}
        if method in ("Page.enable", "Accessibility.enable", "Accessibility.disable"):
            assert session_id in self.sessions
            return {}
        if method == "Accessibility.getFullAXTree":
            assert session_id in self.sessions
            return {"nodes": _answer_ax("offline raw AX")}
        if method == "Page.navigate":
            tid = self.sessions[session_id]
            next(t for t in self.targets if t["targetId"] == tid)["url"] = params["url"]
            return {"frameId": "frame-" + tid}
        raise AssertionError("unexpected CDP command: " + method)

    def calls_for(self, method):
        return [c for c in self.send.call_args_list if c.args[0] == method]


@dataclass(frozen=True)
class _Lease:
    site: tuple
    question_id: str
    generation: int


class _LeaseBusy(Exception):
    pass


class _Leases:
    """One cooperative scheduler owns this table; production atomicity is a gate."""

    def __init__(self):
        self.active, self.generations = {}, {}

    def acquire(self, site, question_id):
        if site in self.active:
            raise _LeaseBusy("one active question per site")
        generation = self.generations.get(site, 0) + 1
        self.generations[site] = generation
        token = _Lease(site, question_id, generation)
        self.active[site] = token
        return token

    def release(self, token):
        if token is not None and self.active.get(token.site) is token:
            del self.active[token.site]


class _Fenced(Exception):
    pass


class _TransientRead(Exception):
    pass


class _Queues:
    def __init__(self, limit=32):
        self.by_session, self.limit = {}, limit

    def register(self, sid):
        self.by_session.setdefault(sid, deque())

    def put(self, event):
        q = self.by_session[event["sessionId"]]
        if len(q) >= self.limit:
            return False
        q.append(deepcopy(event))
        return True

    def take(self, sid, method):
        q = self.by_session[sid]
        for i, ev in enumerate(q):
            if ev["method"] == method:
                del q[i]
                return ev
        return None

    def clear_loads(self, sid):
        self.clear_method(sid, "Page.loadEventFired")

    def clear_method(self, sid, method):
        self.by_session[sid] = deque(e for e in self.by_session[sid]
                                     if e["method"] != method)


@dataclass(frozen=True)
class _CallerTab:
    target_id: str
    session_id: str


@dataclass
class _Job:
    site: _Site
    question_id: str
    question: str
    token: object = None
    target_id: object = None
    session_id: object = None
    target_owned: bool = False
    session_owned: bool = False
    create_unknown: bool = False
    attach_unknown: bool = False
    fenced: bool = False
    phase: str = "ACQUIRING"
    completion: str = "not_started"
    extraction: str = "not_attempted"
    navigation: int = 0
    waits_left: int = 8
    read_attempts: int = 0
    read_errors: int = 0
    submit_attempts: int = 0
    snapshot: object = None
    receipt: object = None


class _ReferenceBroker:
    """Executable proposed policy confined to this test module.

    submit/read/extract are local Mock callbacks, not external tool contracts.
    STOP, timeout and cancellation release the local lease and defer *all*
    remote cleanup. A tombstone prevents automatic re-entry after those states.
    """
    MAX_READ_ERRORS = 3

    def __init__(self, cdp, caller_bindings=None):
        self.cdp = cdp
        # Trusted fixture/caller registry, NOT an invented CDP introspection API.
        self.caller_bindings = dict(caller_bindings or {})
        self.leases = _Leases()
        self.queues = _Queues()
        self.jobs, self.by_session, self.receipts = [], {}, []
        self.tombstones = set()
        self.cancelled = False
        self.ignored_events = 0
        self.pending_attach = None
        self.early_events = []
        self.submit = Mock(return_value=None)
        self.read = Mock(side_effect=self._read_fixture)
        self.extract = Mock(side_effect=_fixture_extract)

    def _gate(self, job):
        if (job.receipt is not None or job.fenced or self.cancelled
                or self.leases.active.get(job.site.lease_key) is not job.token):
            raise _Fenced("job is no longer allowed to dispatch")

    def command(self, job, method, params=None, session_id=None):
        self._gate(job)
        if session_id is not None and session_id != job.session_id:
            raise ValueError("command session does not belong to this job")
        reply = self.cdp.send(method, params, session_id=session_id)
        # Effects/ownership acknowledged by the reply are recorded by the caller;
        # the next command is gated again even if STOP arrived during this call.
        return reply

    def _bind(self, job):
        prior = self.by_session.get(job.session_id)
        if prior is not None and (prior.receipt is None or prior.fenced):
            raise ValueError("session already belongs to an active or fenced job")
        self.by_session[job.session_id] = job
        self.queues.register(job.session_id)
        if prior is not None:
            self.queues.by_session[job.session_id].clear()

    @staticmethod
    def _refresh_receipt_identity(job):
        # An already-sent command can acknowledge ownership after cancellation.
        # Record that fact locally for deferred cleanup; never send another call.
        if job.receipt is not None:
            job.receipt.update(
                target_id=job.target_id, session_id=job.session_id,
                target_ownership="broker" if job.target_owned else "caller_or_unknown",
                session_ownership="broker" if job.session_owned else "caller_or_unknown")

    def ensure_tab(self, job, selected_target=None, caller_tab=None):
        """PROPOSED open-or-reuse; production CdpPort has no such method yet."""
        site = job.site
        if _origin(site.open_url) != site.lease_key:
            raise ValueError("requested route is outside the configured site")
        if (caller_tab is not None
                and self.caller_bindings.get(caller_tab.session_id) != caller_tab.target_id):
            raise ValueError("caller session/target binding is not registered")
        targets = self.command(job, "Target.getTargets").get("targetInfos", [])
        matches = [t for t in targets if site.matches(t)]
        if caller_tab is not None:
            selected_target = caller_tab.target_id
        if selected_target is not None:
            matches = [t for t in matches if t["targetId"] == selected_target]
            if len(matches) != 1:
                raise ValueError("explicit target is not a matching page")
        if len(matches) > 1:
            raise ValueError("AMBIGUOUS_TARGET: explicit selection required")
        if matches:
            job.target_id = matches[0]["targetId"]
        else:
            job.create_unknown = True
            result = self.command(job, "Target.createTarget", {"url": site.open_url})
            tid = result.get("targetId")
            if (not isinstance(tid, str) or not tid
                    or any(t["targetId"] == tid for t in targets)):
                raise ValueError("creation outcome has no fresh target identity")
            job.target_id, job.target_owned, job.create_unknown = tid, True, False
        self._refresh_receipt_identity(job)
        if caller_tab is None:
            job.attach_unknown = True
            self.pending_attach, self.early_events = job, []
            try:
                result = self.command(job, "Target.attachToTarget",
                                      {"targetId": job.target_id, "flatten": True})
            except Exception:
                # An unbound STOP plus an unknown attach outcome cannot justify
                # further cleanup dispatch while its session identity is unknown.
                if any(self._stop_evidence(e) for e in self.early_events):
                    job.fenced = True
                raise
            finally:
                early = self.early_events
                self.pending_attach, self.early_events = None, []
            sid = result.get("sessionId")
            if not isinstance(sid, str) or not sid:
                raise ValueError("attach outcome has no session identity")
            # Validate ownership before claiming the attachment for cleanup.
            prior = self.by_session.get(sid)
            if prior is not None or sid in self.caller_bindings:
                raise ValueError("attach returned an already registered or caller-owned session")
            job.session_id, job.session_owned, job.attach_unknown = sid, True, False
        else:
            job.session_id = caller_tab.session_id
            early = []
        self._refresh_receipt_identity(job)
        self._bind(job)
        for event in early:
            if event.get("sessionId") == job.session_id:
                self.feed(event)
            else:
                self.ignored_events += 1
        self.command(job, "Page.enable", session_id=job.session_id)
        self.navigate(job)

    def navigate(self, job):
        self._gate(job)
        if job.submit_attempts:
            raise ValueError("cannot navigate an already submitted question")
        self.queues.clear_loads(job.session_id)  # only this session, unlike S1-F4
        job.navigation += 1
        result = self.command(job, "Page.navigate", {"url": job.site.open_url},
                              session_id=job.session_id)
        if result.get("errorText"):
            raise CdpError(result["errorText"])
        self._gate(job)
        job.phase = "NAVIGATING"

    def start(self, site, question_id="question-1", question="One shared question",
              selected_target=None, caller_tab=None):
        job = _Job(site, question_id, question)
        self.jobs.append(job)
        try:
            job.token = self.leases.acquire(site.lease_key, question_id)
            if self.cancelled or site.lease_key in self.tombstones:
                job.fenced = True
                return self._finish(job, "REVIEW_REQUIRED", error="terminal fence persists")
            self.ensure_tab(job, selected_target, caller_tab)
        except _LeaseBusy as exc:
            self._finish(job, "LEASE_BUSY", error=str(exc))
        except _Fenced:
            if job.receipt is None:
                self._finish(job, "CANCELLED", error="dispatch fenced")
        except Exception as exc:
            self._finish(job, "SETUP_ERROR", completion="error", error=str(exc))
        return job

    def _cleanup(self, job):
        errors = []
        if job.fenced or self.cancelled:
            return "deferred", errors
        commands = []
        if job.session_owned:
            commands.append(("Target.detachFromTarget", {"sessionId": job.session_id}))
        if job.target_owned:
            commands.append(("Target.closeTarget", {"targetId": job.target_id}))
        for method, params in commands:
            if job.fenced or self.cancelled:
                return "deferred", errors
            try:
                result = self.cdp.send(method, params)
                if method == "Target.closeTarget" and result.get("success") is not True:
                    raise CdpError("closeTarget not acknowledged")
            except Exception as exc:
                errors.append(str(exc))
        if job.create_unknown or job.attach_unknown:
            return "reconcile_unknown_outcome", errors
        return ("failed" if errors else "complete"), errors

    def _finish(self, job, status, completion=None, extraction=None, answer=None, error=None):
        if job.receipt is not None:
            return job
        if completion is not None:
            job.completion = completion
        if extraction is not None:
            job.extraction = extraction
        job.phase = "TERMINAL"
        job.receipt = {
            "site": job.site.name, "target_id": job.target_id,
            "session_id": job.session_id, "question_id": job.question_id,
            "generation": job.token.generation if job.token else 0,
            "requested_route": job.site.open_url,
            "status": status, "completion_status": job.completion,
            "extraction_status": job.extraction,
            "answer": answer if status == "SUCCESS" else None,
            "error": error, "submit_attempts": job.submit_attempts,
            "read_attempts": job.read_attempts,
            "target_ownership": "broker" if job.target_owned else "caller_or_unknown",
            "session_ownership": "broker" if job.session_owned else "caller_or_unknown",
        }
        cleanup, errors = "failed", []
        try:
            cleanup, errors = self._cleanup(job)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            job.receipt.update(cleanup_status=cleanup, cleanup_errors=errors)
            if cleanup != "complete":
                self.tombstones.add(job.site.lease_key)
            self.leases.release(job.token)
            self.receipts.append(job.receipt)
        return job

    def _halt(self, job, status, reason):
        job.fenced = True
        self.tombstones.add(job.site.lease_key)
        completion = job.completion if job.completion == "complete" else status.lower()
        extraction = job.extraction if job.extraction == "ok" else "blocked"
        self._finish(job, status, completion=completion, extraction=extraction, error=reason)

    def cancel(self, job=None):
        if job is None:
            self.cancelled = True
            for active in self.jobs:
                if active.receipt is None:
                    self._halt(active, "CANCELLED", "caller cancelled the harvest")
        elif job.receipt is None:
            self._halt(job, "CANCELLED", "caller cancelled this job")

    @staticmethod
    def _stop_evidence(event):
        params = event.get("params", {})
        url = params.get("url", params.get("frame", {}).get("url", ""))
        parsed = urlsplit(url)
        if (parsed.hostname in ("www.google.com", "google.com")
                and (parsed.path == "/sorry" or parsed.path.startswith("/sorry/"))):
            return "observed /sorry/ redirect"
        for n in params.get("nodes", []):
            if (n.get("role", {}).get("value") == "alert"
                    and "unusual traffic" in n.get("name", {}).get("value", "").lower()):
                return "observed fixture unusual-traffic alert"
        return None

    def feed(self, event):
        job = self.by_session.get(event.get("sessionId"))
        if job is None:
            if self.pending_attach is not None:
                if len(self.early_events) >= self.queues.limit:
                    self._halt(self.pending_attach, "EVENT_OVERFLOW", "early attachment queue is full")
                else:
                    self.early_events.append(deepcopy(event))
                return
            self.ignored_events += 1
            return
        # Trusted model envelopes reject old questions/generations. Real CDP
        # loader/frame correlation is an unresolved production review gate.
        needs_envelope = event.get("method") in ("fixture.ax", "Page.loadEventFired")
        if (needs_envelope or "question_id" in event) and not self._correlated(job, event):
            self.ignored_events += 1
            return
        stop = self._stop_evidence(event)
        if stop:
            self._halt(job, "STOP", stop)  # control observation precedes queued work
            return
        if job.receipt is not None:
            self.ignored_events += 1
            return
        if not self.queues.put(event):
            self._halt(job, "EVENT_OVERFLOW", "bounded session queue is full")

    @staticmethod
    def _correlated(job, event):
        return (event.get("question_id") == job.question_id
                and event.get("generation") == job.token.generation
                and event.get("navigation") == job.navigation)

    def _take_work(self, job, method):
        # Recheck at consumption too: evidence may have entered before navigation
        # or rebinding changed the current generation.
        while True:
            event = self.queues.take(job.session_id, method)
            if event is None or self._correlated(job, event):
                return event
            self.ignored_events += 1

    def _read_fixture(self, job):
        event = self._take_work(job, "fixture.ax")
        return event["params"]["nodes"] if event else None

    def _wait_step(self, job):
        job.waits_left -= 1
        if job.waits_left <= 0:
            self._halt(job, "TIMEOUT", "logical fixture wait budget exhausted")

    def step(self, job):
        if job.receipt is not None:
            return
        try:
            self._gate(job)
            if job.phase == "NAVIGATING":
                event = self._take_work(job, "Page.loadEventFired")
                if event is None:
                    self._wait_step(job)
                else:
                    job.phase = "READY"
            elif job.phase == "READY":
                self.queues.clear_method(job.session_id, "fixture.ax")
                job.submit_attempts += 1
                job.completion, job.phase = "pending", "WAITING"
                try:
                    self.submit(job)  # fixture callback, exactly one attempt
                    self._gate(job)
                except _Fenced:
                    raise
                except Exception as exc:
                    job.fenced = True
                    self.tombstones.add(job.site.lease_key)
                    self._finish(job, "SUBMISSION_UNKNOWN", completion="unknown", error=str(exc))
            elif job.phase == "WAITING":
                job.read_attempts += 1
                try:
                    raw = self.read(job)
                    self._gate(job)
                except _TransientRead as exc:
                    self._gate(job)  # STOP observed during a failed read wins
                    job.read_errors += 1
                    if job.read_errors >= self.MAX_READ_ERRORS:
                        self._finish(job, "READ_ERROR", completion="error", error=str(exc))
                    else:
                        self._wait_step(job)
                    return
                if raw is not None and _fixture_complete(raw):
                    job.snapshot = deepcopy(raw)
                    job.completion, job.phase = "complete", "EXTRACT"
                else:
                    self._wait_step(job)
            elif job.phase == "EXTRACT":
                job.extraction = "pending"
                try:
                    answer = self.extract(job.snapshot)
                    self._gate(job)
                    if not isinstance(answer, str) or not answer:
                        raise ValueError("extractor returned no answer")
                    self._finish(job, "SUCCESS", extraction="ok", answer=answer)
                except _Fenced:
                    raise
                except Exception as exc:
                    self._finish(job, "EXTRACTION_ERROR", extraction="error", error=str(exc))
        except _Fenced:
            if job.receipt is None:
                self._halt(job, "CANCELLED", "dispatch fenced")
        except Exception as exc:
            self._finish(job, "ERROR", completion="error", error=str(exc))


def _event(job, method, **params):
    return {"method": method, "sessionId": job.session_id,
            "question_id": job.question_id, "generation": job.token.generation,
            "navigation": job.navigation, "params": params}


class ProductionPins(unittest.TestCase):
    def test_attach_url_reuses_first_matching_page_and_enables_its_session(self):
        fake = _FakeCDP([dict(targetId="worker", type="worker", url=SITES[0].prefix)]
                        + _targets() + [dict(targetId="second", type="page", url=SITES[0].prefix)])
        port = CdpPort()
        port.send = fake.send
        sid = port.attach_url(SITES[0].prefix)
        self.assertEqual(port._attached, [sid])
        self.assertEqual(fake.sessions[sid], "t-perplexity")
        self.assertEqual(fake.calls_for("Target.createTarget"), [])
        self.assertEqual(fake.calls_for("Page.navigate"), [])
        self.assertEqual(fake.calls_for("Page.enable"), [call("Page.enable", session_id=sid)])

    def test_attach_url_missing_page_raises_without_creating(self):
        port, fake = CdpPort(), _FakeCDP([])
        port.send = fake.send
        with self.assertRaisesRegex(CdpError, "no open page"):
            port.attach_url(SITES[0].prefix)
        self.assertEqual(fake.send.call_args_list, [call("Target.getTargets")])

    def test_wait_event_consumes_only_the_requested_session(self):
        port = CdpPort()
        a = {"method": "Page.loadEventFired", "sessionId": "A"}
        b = {"method": "Page.loadEventFired", "sessionId": "B"}
        port.events.extend([b, a])
        self.assertEqual(port.wait_event("Page.loadEventFired", session_id="A"), a)
        self.assertEqual(list(port.events), [b])

    def test_close_only_detaches_owned_sessions_never_browser_close(self):
        port = CdpPort()
        port.send, port._ws = Mock(return_value={}), Mock()
        socket = port._ws
        port._attached = ["owned-A", "owned-B"]
        port.close()
        self.assertEqual(port.send.call_args_list,
                         [call("Target.detachFromTarget", {"sessionId": "owned-A"}, timeout=5),
                          call("Target.detachFromTarget", {"sessionId": "owned-B"}, timeout=5)])
        socket.close.assert_called_once_with()
        self.assertEqual(port._attached, [])
        self.assertIsNone(port._ws)
        port.close()
        self.assertEqual(port.send.call_count, 2)

    def test_raw_ax_uses_explicit_session_with_immediate_fixture_success(self):
        fake = _FakeCDP()
        fake.sessions["fixture-session"] = "t-chatgpt"
        with patch.object(ax.time, "sleep", side_effect=AssertionError("no sleeps")):
            raw = ax.raw_tree(fake, "fixture-session")
        self.assertEqual(raw, _answer_ax("offline raw AX"))
        self.assertEqual(fake.send.call_args_list,
                         [call("Accessibility.enable", session_id="fixture-session"),
                          call("Accessibility.getFullAXTree", session_id="fixture-session")])

    def test_pruned_ax_cannot_be_used_as_full_answer_text(self):
        raw = _answer_ax("complete answer beyond the chassis")
        pruned = ax.prune(raw)
        self.assertIn("article", [n["role"] for n in pruned])
        self.assertNotIn("StaticText", [n["role"] for n in pruned])
        self.assertNotIn("complete answer beyond the chassis", [n["name"] for n in pruned])
        self.assertEqual(_fixture_extract(raw), "complete answer beyond the chassis")

    def test_wp1_06_production_ensure_tab_open_or_reuse_is_absent(self):
        """WP1-06: CdpPort.ensure_tab opens or reuses a site tab with immediate load evidence."""
        port, fake = CdpPort(), _FakeCDP([])
        orig_send = fake.send.side_effect

        def send_with_load(method, params=None, session_id=None, timeout=None):
            res = orig_send(method, params, session_id, timeout)
            if method == "Page.navigate":
                port.events.append({"method": "Page.loadEventFired", "sessionId": session_id})
            return res

        fake.send.side_effect = send_with_load
        port.send = fake.send
        ensure_tab = port.ensure_tab
        handle = ensure_tab(SITES[0].prefix, SITES[0].open_url)
        self.assertEqual(len(fake.calls_for("Target.createTarget")), 1)
        self.assertTrue(handle.target_owned)
        self.assertTrue(handle.session_owned)

    def test_s1_f4_production_navigate_must_preserve_other_session_load(self):
        """S1-F4 only: real navigate currently purges B's pending load."""
        port = CdpPort()
        old_a = {"method": "Page.loadEventFired", "sessionId": "A", "params": {"old": True}}
        pending_b = {"method": "Page.loadEventFired", "sessionId": "B"}
        port.events.extend([old_a, pending_b])

        def immediate_load(method, params=None, session_id=None, timeout=None):
            self.assertEqual(method, "Page.navigate")
            port.events.append({"method": "Page.loadEventFired", "sessionId": session_id})
            return {"frameId": "A-frame"}

        port.send = Mock(side_effect=immediate_load)
        port.navigate("A", SITES[0].open_url)
        self.assertIn(pending_b, port.events)


class ReferenceModelTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeCDP()
        self.broker = _ReferenceBroker(self.fake)

    def submitted(self, site=SITES[0], **kwargs):
        job = self.broker.start(site, **kwargs)
        self.assertIsNone(job.receipt)
        self.broker.feed(_event(job, "Page.loadEventFired"))
        self.broker.step(job)
        self.broker.step(job)
        self.assertEqual(job.phase, "WAITING")
        return job

    def finish_answer(self, job, text=None):
        text = text if text is not None else "answer from " + job.site.name
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax(text)))
        self.broker.step(job)
        self.assertEqual(job.completion, "complete")
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "SUCCESS")
        return job.receipt

    def assertReleased(self, job):
        self.assertIsNot(self.broker.leases.active.get(job.site.lease_key), job.token)

    def assertNoFurtherCalls(self, job):
        before = (list(self.fake.send.call_args_list), self.broker.submit.call_count,
                  self.broker.read.call_count, self.broker.extract.call_count)
        for _ in range(4):
            self.broker.step(job)
        self.assertEqual(before, (list(self.fake.send.call_args_list), self.broker.submit.call_count,
                                  self.broker.read.call_count, self.broker.extract.call_count))

    def test_reuse_enables_correct_session_and_navigates_direct_route(self):
        job = self.broker.start(SITES[1])
        self.assertIsNone(job.receipt)
        self.assertEqual(job.target_id, "t-chatgpt")
        self.assertFalse(job.target_owned)
        self.assertTrue(job.session_owned)
        self.assertEqual(self.fake.calls_for("Target.createTarget"), [])
        self.assertEqual(self.fake.calls_for("Target.attachToTarget"),
                         [call("Target.attachToTarget", {"targetId": "t-chatgpt", "flatten": True}, session_id=None)])
        self.assertEqual(self.fake.calls_for("Page.enable"),
                         [call("Page.enable", None, session_id=job.session_id)])
        self.assertEqual(self.fake.calls_for("Page.navigate"),
                         [call("Page.navigate", {"url": SITES[1].open_url}, session_id=job.session_id)])

    def test_no_match_creates_once_at_open_url_then_attaches_enables_and_routes(self):
        self.fake.targets = [t for t in self.fake.targets if t["targetId"] != "t-gemini"]
        job = self.broker.start(SITES[2])
        self.assertTrue(job.target_owned)
        self.assertEqual([c.args[0] for c in self.fake.send.call_args_list],
                         ["Target.getTargets", "Target.createTarget", "Target.attachToTarget",
                          "Page.enable", "Page.navigate"])
        self.assertEqual(self.fake.calls_for("Target.createTarget"),
                         [call("Target.createTarget", {"url": SITES[2].open_url}, session_id=None)])
        self.assertEqual(self.fake.sessions[job.session_id], job.target_id)
        self.assertEqual(self.fake.calls_for("Page.enable")[0].kwargs["session_id"], job.session_id)
        self.assertEqual(self.fake.calls_for("Page.navigate")[0].args[1]["url"], SITES[2].open_url)

    def test_non_page_and_other_origin_targets_are_not_reused(self):
        self.fake.targets = [dict(targetId="worker", type="worker", url=SITES[0].prefix),
                             dict(targetId="other", type="page", url="https://www.perplexity.ai.evil.invalid/")]
        job = self.broker.start(SITES[0])
        self.assertTrue(job.target_owned)
        self.assertEqual(len(self.fake.calls_for("Target.createTarget")), 1)

    def test_multiple_matches_reject_without_creating_or_attaching(self):
        self.fake.targets.append(dict(targetId="duplicate", type="page", url=SITES[0].prefix))
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertIn("AMBIGUOUS_TARGET", job.receipt["error"])
        self.assertEqual([c.args[0] for c in self.fake.send.call_args_list], ["Target.getTargets"])
        self.assertReleased(job)

    def test_explicit_selection_resolves_ambiguity_but_invalid_hint_never_creates(self):
        self.fake.targets.append(dict(targetId="duplicate", type="page", url=SITES[0].prefix))
        job = self.broker.start(SITES[0], selected_target="duplicate")
        self.assertEqual(job.target_id, "duplicate")
        missing = self.broker.start(SITES[1], selected_target="not-a-target")
        self.assertEqual(missing.receipt["status"], "SETUP_ERROR")
        self.assertEqual(self.fake.calls_for("Target.createTarget"), [])

    def test_route_outside_configured_site_rejected_before_any_send(self):
        site = _Site("chatgpt", SITES[1].prefix, "https://other.invalid/route")
        job = self.broker.start(site)
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.fake.send.assert_not_called()
        self.assertReleased(job)

    def test_contention_is_per_site_origin_and_does_not_release_incumbent(self):
        incumbent = self.broker.start(SITES[1])
        before = list(self.fake.send.call_args_list)
        alias = _Site("chatgpt-alias", SITES[1].prefix, SITES[1].open_url)
        rejected = self.broker.start(alias, question_id="question-2")
        self.assertEqual(rejected.receipt["status"], "LEASE_BUSY")
        self.assertEqual(rejected.receipt["question_id"], "question-2")
        self.assertEqual(before, self.fake.send.call_args_list)
        self.assertIs(self.broker.leases.active[alias.lease_key], incumbent.token)

    def test_stale_release_cannot_unlock_a_new_question(self):
        first = self.submitted()
        self.finish_answer(first)
        second = self.broker.start(SITES[0], question_id="question-2")
        self.broker.leases.release(first.token)
        self.assertIs(self.broker.leases.active[SITES[0].lease_key], second.token)
        self.assertGreater(second.token.generation, first.token.generation)

    def test_interleaved_navigation_preserves_sibling_pending_load_and_purges_own_old_load(self):
        a, b = [self.broker.start(s) for s in SITES[:2]]
        old_a = _event(a, "Page.loadEventFired")
        pending_b = _event(b, "Page.loadEventFired")
        self.broker.feed(old_a)
        self.broker.feed(pending_b)
        self.broker.navigate(a)
        self.assertEqual(list(self.broker.queues.by_session[b.session_id]), [pending_b])
        self.broker.step(a)
        self.assertEqual(a.phase, "NAVIGATING")
        self.broker.feed(old_a)  # a delayed old model navigation envelope also loses
        self.broker.step(a)
        self.assertEqual(a.phase, "NAVIGATING")
        self.broker.feed(_event(a, "Page.loadEventFired"))
        self.broker.step(a)
        self.broker.step(b)
        self.assertEqual((a.phase, b.phase), ("READY", "READY"))

    def test_three_healthy_sites_finish_with_provenance_while_independent_tab_stops(self):
        jobs = [self.broker.start(s) for s in SITES]
        stopped = self.broker.start(INDEPENDENT)
        self.broker.feed({"method": "Page.frameNavigated", "sessionId": stopped.session_id,
                          "params": {"frame": {"url": SORRY_URL}}})
        stop_trace = list(self.fake.send.call_args_list)
        self.assertEqual(stopped.receipt["status"], "STOP")
        self.assertEqual(stopped.receipt["cleanup_status"], "deferred")
        for job in reversed(jobs):
            self.broker.feed(_event(job, "Page.loadEventFired"))
        for job in jobs:
            self.broker.step(job)
            self.broker.step(job)
        self.assertEqual(self.broker.submit.call_count, 3)
        self.assertEqual([c.args[0].question for c in self.broker.submit.call_args_list],
                         ["One shared question"] * 3)
        self.assertTrue(all(j.receipt is None and j.phase == "WAITING" for j in jobs))
        for job in (jobs[2], jobs[0], jobs[1]):
            self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("answer " + job.site.name)))
        for job in (jobs[1], jobs[2], jobs[0]):
            self.broker.step(job)
        self.assertTrue(all(j.completion == "complete" and j.receipt is None for j in jobs))
        for job in (jobs[2], jobs[1], jobs[0]):
            self.broker.step(job)
            receipt = job.receipt
            self.assertEqual((receipt["site"], receipt["target_id"], receipt["session_id"],
                              receipt["question_id"], receipt["answer"]),
                             (job.site.name, job.target_id, job.session_id,
                              "question-1", "answer " + job.site.name))
        self.assertEqual(len(self.broker.receipts), 4)
        self.assertEqual(self.broker.leases.active, {})
        self.assertEqual(stopped.receipt["target_id"], "t-independent")
        self.assertEqual(stopped.receipt["session_id"], stopped.session_id)
        self.assertEqual(stopped.receipt["question_id"], "question-1")
        for c in self.fake.send.call_args_list[len(stop_trace):]:
            self.assertNotEqual(c.kwargs.get("session_id"), stopped.session_id)
            self.assertNotEqual((c.args[1] or {}).get("sessionId"), stopped.session_id)
        self.assertNoFurtherCalls(stopped)

    def test_timeout_releases_only_its_lease_and_sibling_finishes(self):
        timed_out, healthy = [self.submitted(s) for s in SITES[:2]]
        for _ in range(timed_out.waits_left):
            self.broker.step(timed_out)
        self.assertEqual(timed_out.receipt["status"], "TIMEOUT")
        self.assertEqual(timed_out.receipt["cleanup_status"], "deferred")
        self.assertIsNone(timed_out.receipt["answer"])
        self.assertReleased(timed_out)
        self.assertIs(self.broker.leases.active[healthy.site.lease_key], healthy.token)
        self.assertNoFurtherCalls(timed_out)
        self.finish_answer(healthy)

    def test_navigation_timeout_never_submits_question(self):
        job = self.broker.start(SITES[0])
        for _ in range(job.waits_left):
            self.broker.step(job)
        self.assertEqual(job.receipt["status"], "TIMEOUT")
        self.broker.submit.assert_not_called()
        self.assertNoFurtherCalls(job)

    def test_stop_after_completion_preserves_completion_and_blocks_extraction(self):
        job = self.submitted()
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("not extracted")))
        self.broker.step(job)
        self.broker.feed(_event(job, "Page.frameNavigated", frame={"url": SORRY_URL}))
        self.assertEqual(job.receipt["completion_status"], "complete")
        self.assertEqual(job.receipt["extraction_status"], "blocked")
        self.assertIsNone(job.receipt["answer"])
        self.broker.extract.assert_not_called()
        self.assertNoFurtherCalls(job)

    def test_stop_observed_during_enable_prevents_navigation(self):
        original = self.fake.send.side_effect

        def stop_on_enable(method, params=None, session_id=None, timeout=None):
            result = original(method, params, session_id, timeout)
            if method == "Page.enable":
                self.broker.feed({"method": "Page.frameNavigated", "sessionId": session_id,
                                  "params": {"frame": {"url": SORRY_URL}}})
            return result

        self.fake.send.side_effect = stop_on_enable
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertEqual(self.fake.calls_for("Page.navigate"), [])
        self.assertReleased(job)
        self.assertNoFurtherCalls(job)

    def test_stop_between_compound_read_commands_fences_next_command(self):
        job = self.submitted()

        def guarded_read(active):
            self.broker.command(active, "Accessibility.enable", session_id=active.session_id)
            self.broker.feed(_event(active, "Page.frameNavigated", frame={"url": SORRY_URL}))
            self.broker.command(active, "Accessibility.getFullAXTree", session_id=active.session_id)

        self.broker.read.side_effect = guarded_read
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertEqual(self.fake.calls_for("Accessibility.getFullAXTree"), [])
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertNoFurtherCalls(job)

    def test_stop_during_transient_read_error_prevents_retry(self):
        job = self.submitted()

        def stop_then_error(active):
            self.broker.feed(_event(active, "Page.frameNavigated", frame={"url": SORRY_URL}))
            raise _TransientRead("temporary read failure")

        self.broker.read.side_effect = stop_then_error
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertEqual(self.broker.read.call_count, 1)
        self.assertNoFurtherCalls(job)

    def test_stop_before_scheduled_read_retry_prevents_retry(self):
        job = self.submitted()
        self.broker.read.side_effect = _TransientRead("temporary")
        self.broker.step(job)
        self.assertIsNone(job.receipt)
        self.broker.feed(_event(job, "fixture.ax", nodes=[_node("wall", "alert", "Unusual traffic")]))
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertNoFurtherCalls(job)

    def test_idempotent_read_retries_are_bounded_without_question_resubmission(self):
        job = self.submitted()
        self.broker.read.side_effect = _TransientRead("temporary")
        for _ in range(self.broker.MAX_READ_ERRORS + 2):
            self.broker.step(job)
        self.assertEqual(self.broker.read.call_count, 3)
        self.assertEqual(self.broker.submit.call_count, 1)
        self.assertEqual(job.receipt["status"], "READ_ERROR")
        self.assertReleased(job)

    def test_transient_read_can_recover_within_budget(self):
        job = self.submitted()
        self.broker.read.side_effect = [_TransientRead("temporary"), _answer_ax("recovered")]
        self.broker.step(job)
        self.broker.step(job)
        self.broker.step(job)
        self.assertEqual(job.receipt["answer"], "recovered")
        self.assertEqual(job.receipt["read_attempts"], 2)
        self.assertEqual(job.receipt["submit_attempts"], 1)

    def test_ambiguous_submission_is_never_replayed(self):
        self.broker.submit.side_effect = CdpError("delivery outcome unknown")
        job = self.broker.start(SITES[0])
        self.broker.feed(_event(job, "Page.loadEventFired"))
        self.broker.step(job)
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(job.receipt["completion_status"], "unknown")
        self.assertReleased(job)
        self.assertNoFurtherCalls(job)

    def test_user_copy_button_and_generation_stop_button_are_not_completion(self):
        job = self.submitted()
        for raw in (_answer_ax("user text", user_only=True), _answer_ax("partial", complete=False)):
            self.broker.feed(_event(job, "fixture.ax", nodes=raw))
            self.broker.step(job)
            self.assertEqual(job.completion, "pending")
            self.assertFalse(job.fenced)
            self.assertIsNone(job.receipt)
        self.broker.extract.assert_not_called()
        self.finish_answer(job, "final answer without UI trailer")
        self.assertEqual(job.receipt["answer"], "final answer without UI trailer")

    def test_extraction_error_keeps_successful_completion_separate(self):
        job = self.submitted()
        self.broker.extract.side_effect = ValueError("fixture extraction failed")
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("answer")))
        self.broker.step(job)
        self.assertEqual(job.completion, "complete")
        self.assertEqual(job.extraction, "not_attempted")
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "EXTRACTION_ERROR")
        self.assertEqual(job.receipt["completion_status"], "complete")
        self.assertEqual(job.receipt["extraction_status"], "error")
        self.assertIsNone(job.receipt["answer"])
        self.assertReleased(job)

    def test_empty_extraction_is_failure_not_empty_success(self):
        job = self.submitted()
        self.broker.extract.side_effect = None
        self.broker.extract.return_value = ""
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("answer")))
        self.broker.step(job)
        self.broker.step(job)
        self.assertEqual(job.receipt["status"], "EXTRACTION_ERROR")
        self.assertEqual(job.receipt["completion_status"], "complete")

    def test_unknown_session_and_old_question_envelopes_cannot_supply_answer(self):
        job = self.submitted()
        stale = _event(job, "fixture.ax", nodes=_answer_ax("wrong answer"))
        stale["question_id"] = "previous-question"
        unknown = _event(job, "fixture.ax", nodes=_answer_ax("foreign answer"))
        unknown["sessionId"] = "unknown-session"
        self.broker.feed(stale)
        self.broker.feed(unknown)
        self.broker.step(job)
        self.assertEqual(job.completion, "pending")
        self.assertEqual(self.broker.ignored_events, 2)
        self.finish_answer(job, "right answer")

    def test_old_generation_on_reused_caller_session_cannot_finish_new_question(self):
        handle = _CallerTab("t-chatgpt", "caller-session")
        self.fake.sessions[handle.session_id] = handle.target_id
        self.broker.caller_bindings[handle.session_id] = handle.target_id
        first = self.submitted(SITES[1], caller_tab=handle)
        stale = _event(first, "fixture.ax", nodes=_answer_ax("previous answer"))
        self.finish_answer(first)
        second = self.submitted(SITES[1], caller_tab=handle)
        self.broker.feed(stale)  # same question string/id; generation still differs
        self.broker.step(second)
        self.assertEqual(second.completion, "pending")
        self.finish_answer(second, "new answer")

    def test_stop_tombstone_survives_lease_release_and_late_events(self):
        job = self.submitted()
        self.broker.feed(_event(job, "Page.frameNavigated", frame={"url": SORRY_URL}))
        before = list(self.fake.send.call_args_list)
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("late answer")))
        again = self.broker.start(SITES[0], question_id="question-2")
        self.assertEqual(again.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(before, self.fake.send.call_args_list)
        self.assertIsNone(job.receipt["answer"])
        self.assertEqual(self.broker.leases.active, {})

    def test_success_releases_and_detaches_only_own_session_on_caller_tab(self):
        sibling = self.broker.start(SITES[1])
        job = self.submitted()
        self.finish_answer(job)
        self.assertReleased(job)
        self.assertIn(sibling.session_id, self.fake.sessions)
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"),
                         [call("Target.detachFromTarget", {"sessionId": job.session_id})])
        self.assertEqual(self.fake.calls_for("Target.closeTarget"), [])
        self.assertEqual(self.fake.calls_for("Browser.close"), [])

    def test_broker_owned_tab_is_closed_on_ordinary_success(self):
        self.fake.targets = []
        job = self.submitted()
        self.finish_answer(job)
        self.assertEqual(self.fake.calls_for("Target.closeTarget"),
                         [call("Target.closeTarget", {"targetId": job.target_id})])
        self.assertEqual(self.fake.targets, [])
        self.assertEqual(self.fake.calls_for("Browser.close"), [])

    def test_caller_owned_session_is_never_detached_or_closed(self):
        self.fake.sessions["caller-session"] = "t-gemini"
        self.broker.caller_bindings["caller-session"] = "t-gemini"
        job = self.submitted(SITES[2], caller_tab=_CallerTab("t-gemini", "caller-session"))
        self.finish_answer(job)
        self.assertFalse(job.target_owned)
        self.assertFalse(job.session_owned)
        self.assertEqual(self.fake.calls_for("Target.attachToTarget"), [])
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertEqual(self.fake.calls_for("Target.closeTarget"), [])
        self.assertIn("caller-session", self.fake.sessions)

    def test_creation_exception_releases_lease_without_retry_or_sibling_detach(self):
        sibling = self.broker.start(SITES[1])
        self.fake.targets = [t for t in self.fake.targets if t["targetId"] != "t-perplexity"]
        self.fake.failures["Target.createTarget"] = CdpError("create outcome unknown")
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertEqual(job.receipt["cleanup_status"], "reconcile_unknown_outcome")
        self.assertEqual(len(self.fake.calls_for("Target.createTarget")), 1)
        self.assertIsNone(job.target_id)
        self.assertReleased(job)
        self.assertIn(sibling.session_id, self.fake.sessions)
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertEqual(self.fake.calls_for("Target.closeTarget"), [])

    def test_malformed_creation_reply_never_guesses_target_or_retries(self):
        self.fake.targets = []
        self.fake.failures["Target.createTarget"] = {}
        job = self.broker.start(SITES[0])
        self.assertIsNone(job.target_id)
        self.assertEqual(len(self.fake.calls_for("Target.createTarget")), 1)
        self.assertEqual(self.fake.calls_for("Target.attachToTarget"), [])
        self.assertEqual(job.receipt["cleanup_status"], "reconcile_unknown_outcome")
        self.assertReleased(job)

    def test_attach_exception_on_reused_tab_never_detaches_sibling(self):
        sibling = self.broker.start(SITES[1])
        self.fake.failures["Target.attachToTarget"] = CdpError("attach outcome unknown")
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertReleased(job)
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertEqual(self.fake.calls_for("Target.closeTarget"), [])
        self.assertIn(sibling.session_id, self.fake.sessions)

    def test_attach_exception_after_creation_closes_only_acknowledged_owned_target(self):
        sibling = self.broker.start(SITES[1])
        self.fake.targets = [t for t in self.fake.targets if t["targetId"] != "t-perplexity"]
        self.fake.failures["Target.attachToTarget"] = CdpError("attach failed")
        job = self.broker.start(SITES[0])
        self.assertTrue(job.target_owned)
        self.assertEqual(self.fake.calls_for("Target.closeTarget"),
                         [call("Target.closeTarget", {"targetId": job.target_id})])
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertIn(sibling.session_id, self.fake.sessions)
        self.assertReleased(job)

    def test_enable_exception_cleans_its_attachment_not_sibling_or_caller_target(self):
        sibling = self.broker.start(SITES[1])
        self.fake.failures["Page.enable"] = CdpError("enable failed")
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"),
                         [call("Target.detachFromTarget", {"sessionId": job.session_id})])
        self.assertEqual(self.fake.calls_for("Target.closeTarget"), [])
        self.assertIn(sibling.session_id, self.fake.sessions)
        self.assertReleased(job)

    def test_navigation_error_cleans_only_acknowledged_owned_resources(self):
        self.fake.targets = []
        self.fake.failures["Page.navigate"] = {"errorText": "fixture route rejected"}
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertIn("fixture route rejected", job.receipt["error"])
        self.assertEqual(len(self.fake.calls_for("Target.detachFromTarget")), 1)
        self.assertEqual(len(self.fake.calls_for("Target.closeTarget")), 1)
        self.broker.submit.assert_not_called()
        self.assertReleased(job)

    def test_cleanup_error_keeps_primary_failure_and_releases_lease(self):
        self.fake.failures["Page.enable"] = CdpError("primary enable failure")
        self.fake.failures["Target.detachFromTarget"] = CdpError("cleanup detach failure")
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertEqual(job.receipt["error"], "primary enable failure")
        self.assertEqual(job.receipt["cleanup_errors"], ["cleanup detach failure"])
        self.assertEqual(job.receipt["cleanup_status"], "failed")
        self.assertReleased(job)

    def test_local_cancellation_releases_one_lease_and_sibling_finishes(self):
        cancelled, healthy = [self.submitted(s) for s in SITES[:2]]
        self.broker.cancel(cancelled)
        self.assertEqual(cancelled.receipt["status"], "CANCELLED")
        self.assertReleased(cancelled)
        self.assertNoFurtherCalls(cancelled)
        self.finish_answer(healthy)

    def test_global_cancellation_fences_every_job_and_future_start(self):
        jobs = [self.submitted(s) for s in SITES]
        self.broker.cancel()
        self.assertEqual(self.broker.leases.active, {})
        for job in jobs:
            self.assertEqual(job.receipt["status"], "CANCELLED")
            self.assertEqual(job.receipt["cleanup_status"], "deferred")
            self.assertNoFurtherCalls(job)
        before = list(self.fake.send.call_args_list)
        new = self.broker.start(INDEPENDENT)
        self.assertEqual(new.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(before, self.fake.send.call_args_list)

    def test_session_queue_overflow_is_explicit_and_does_not_drop_sibling_event(self):
        a, b = [self.broker.start(s) for s in SITES[:2]]
        self.broker.queues.limit = 2
        pending_b = _event(b, "Page.loadEventFired")
        self.broker.feed(pending_b)
        for _ in range(3):
            self.broker.feed(_event(a, "fixture.other"))
        self.assertEqual(a.receipt["status"], "EVENT_OVERFLOW")
        self.assertEqual(list(self.broker.queues.by_session[b.session_id]), [pending_b])
        self.assertReleased(a)
        self.assertNoFurtherCalls(a)

    def test_untagged_completion_cannot_bypass_question_correlation(self):
        job = self.submitted()
        self.broker.feed({"method": "fixture.ax", "sessionId": job.session_id,
                          "params": {"nodes": _answer_ax("uncorrelated answer")}})
        self.broker.step(job)
        self.assertEqual(job.completion, "pending")
        self.finish_answer(job, "correlated answer")

    def test_already_buffered_old_navigation_answer_is_rejected_on_consumption(self):
        job = self.broker.start(SITES[0])
        self.broker.feed(_event(job, "fixture.ax", nodes=_answer_ax("old route answer")))
        self.broker.navigate(job)
        self.broker.feed(_event(job, "Page.loadEventFired"))
        self.broker.step(job)
        self.broker.step(job)
        self.broker.step(job)
        self.assertEqual(job.completion, "pending")
        self.finish_answer(job, "new route answer")

    def test_already_buffered_old_question_answer_is_rejected_after_caller_rebind(self):
        handle = _CallerTab("t-chatgpt", "caller-session")
        self.fake.sessions[handle.session_id] = handle.target_id
        self.broker.caller_bindings = {handle.session_id: handle.target_id}
        first = self.submitted(SITES[1], caller_tab=handle)
        self.broker.feed(_event(first, "fixture.ax", nodes=_answer_ax("first answer")))
        self.broker.feed(_event(first, "fixture.ax", nodes=_answer_ax("buffered old answer")))
        self.broker.step(first)
        self.broker.step(first)
        second = self.submitted(SITES[1], caller_tab=handle)
        self.broker.step(second)
        self.assertEqual(second.completion, "pending")
        self.finish_answer(second, "second answer")

    def test_caller_session_target_mismatch_is_rejected_before_any_command(self):
        self.fake.sessions["caller-session"] = "t-gemini"
        self.broker.caller_bindings = {"caller-session": "t-gemini"}
        job = self.broker.start(SITES[1], caller_tab=_CallerTab("t-chatgpt", "caller-session"))
        self.assertIsNotNone(job.receipt)
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.fake.send.assert_not_called()
        self.assertReleased(job)

    def test_attach_reply_cannot_claim_or_detach_a_known_caller_session(self):
        self.fake.sessions["caller-session"] = "t-gemini"
        self.broker.caller_bindings["caller-session"] = "t-gemini"
        self.fake.failures["Target.attachToTarget"] = {"sessionId": "caller-session"}
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertIsNone(job.session_id)
        self.assertFalse(job.session_owned)
        self.assertEqual(self.fake.calls_for("Page.enable"), [])
        self.assertEqual(self.fake.calls_for("Target.detachFromTarget"), [])
        self.assertEqual(self.fake.sessions["caller-session"], "t-gemini")
        self.assertReleased(job)

    def test_unknown_creation_outcome_blocks_readmission_until_review(self):
        self.fake.targets = []
        self.fake.failures["Target.createTarget"] = CdpError("unknown create outcome")
        first = self.broker.start(SITES[0])
        before = list(self.fake.send.call_args_list)
        second = self.broker.start(SITES[0], question_id="question-2")
        self.assertEqual(second.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(before, self.fake.send.call_args_list)
        self.assertReleased(first)
        self.assertReleased(second)

    def test_failed_cleanup_blocks_readmission_without_holding_a_lease(self):
        self.fake.failures["Page.enable"] = CdpError("enable failed")
        self.fake.failures["Target.detachFromTarget"] = CdpError("detach failed")
        first = self.broker.start(SITES[0])
        before = list(self.fake.send.call_args_list)
        second = self.broker.start(SITES[0], question_id="question-2")
        self.assertEqual(second.receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(before, self.fake.send.call_args_list)
        self.assertReleased(first)
        self.assertReleased(second)

    def test_stop_event_before_attach_reply_binding_prevents_enable_and_navigation(self):
        original = self.fake.send.side_effect

        def early_stop(method, params=None, session_id=None, timeout=None):
            result = original(method, params, session_id, timeout)
            if method == "Target.attachToTarget":
                self.broker.feed({"method": "Page.frameNavigated", "sessionId": result["sessionId"],
                                  "params": {"frame": {"url": SORRY_URL}}})
            return result

        self.fake.send.side_effect = early_stop
        job = self.broker.start(SITES[0])
        self.assertIsNotNone(job.receipt)
        self.assertEqual(job.receipt["status"], "STOP")
        self.assertEqual(self.fake.calls_for("Page.enable"), [])
        self.assertEqual(self.fake.calls_for("Page.navigate"), [])
        self.assertReleased(job)
        self.assertNoFurtherCalls(job)

    def test_cancellation_during_create_retains_late_acknowledged_resource_identity(self):
        self.fake.targets = []
        original = self.fake.send.side_effect

        def cancel_during_create(method, params=None, session_id=None, timeout=None):
            result = original(method, params, session_id, timeout)
            if method == "Target.createTarget":
                self.broker.cancel()
            return result

        self.fake.send.side_effect = cancel_during_create
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "CANCELLED")
        self.assertEqual(job.receipt["target_id"], job.target_id)
        self.assertEqual(job.receipt["target_ownership"], "broker")
        self.assertEqual(job.receipt["cleanup_status"], "deferred")
        self.assertEqual(self.fake.calls_for("Target.attachToTarget"), [])
        self.assertReleased(job)
        self.assertNoFurtherCalls(job)

    def test_unexpected_cleanup_exception_still_collects_primary_receipt(self):
        self.fake.failures["Page.enable"] = CdpError("primary error")
        self.broker._cleanup = Mock(side_effect=RuntimeError("unexpected cleanup error"))
        job = self.broker.start(SITES[0])
        self.assertEqual(job.receipt["status"], "SETUP_ERROR")
        self.assertEqual(job.receipt["error"], "primary error")
        self.assertEqual(job.receipt["cleanup_errors"], ["unexpected cleanup error"])
        self.assertIn(job.receipt, self.broker.receipts)
        self.assertReleased(job)


if __name__ == "__main__":
    unittest.main()
