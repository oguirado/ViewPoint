# @module {"id":"wa.perceive.harvest","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe","wa.perceive.cdp_port"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.harvest - offline multi-window harvest broker and W8-style shared lease client.

Provides a deterministic, cooperative multi-window harvest broker (`HarvestBroker`) and an
injectable shared per-origin lease authority (`SharedLeaseAuthority`) for offline verification.
No live network, browser process, W8 wire protocol, distributed crash recovery, or MemoryWeb
service is used or claimed.

Public Broker API (`HarvestBroker`):
- `start(site, question_id="question-1", question="One shared question", selected_target=None, caller_tab=None, *, credential=None, holder=None, epoch=None, token=None, deadline=None, timeout=None)`:
  Acquires a normalized per-origin `(https, lowercase_host, effective_port)` lease from the
  shared authority before any discovery or side effect, validates site/route and optional
  caller-tab bindings, discovers matching `page` targets via `Target.getTargets`, reuses a
  unique match or creates once at `open_url` on a true miss, attaches flattened (`flatten=True`),
  records acknowledged resource ownership immediately, replays bounded early attachment events
  for the acknowledged session prior to `Page.enable`, enables Page, and dispatches session-scoped
  `Page.navigate`. Returns the `HarvestJob` instance.
- `feed(event)`:
  Routes a CDP/fixture event envelope once into the bounded per-session queue (or bounded early
  attachment buffer while `Target.attachToTarget` is in flight). Validates trusted provenance
  (`site`, `target_id`, `sessionId`, `question_id`, `generation`, `navigation`) at ingress for
  work events (`fixture.ax`, `Page.loadEventFired`) and any tagged envelope, rejecting stale,
  untagged, or cross-site envelopes without relabeling them to the newest generation. Evaluates
  STOP evidence (`/sorry/` redirect or unusual-traffic alert) immediately before queueing work,
  latching the local STOP fence and deferring remote cleanup. Fails only the affected session's
  job with `EVENT_OVERFLOW` when its bounded queue overflows.
- `step(job=None)`:
  Advances `job` (or all non-terminal jobs in admission order when `job is None`) by one
  cooperative transition (`NAVIGATING` -> `READY` -> `WAITING` -> `EXTRACT` -> `TERMINAL`).
  Fences local STOP, whole-job deadline expiry, local/global cancellation, and atomic lease
  credential validity before every command or callback (`submit`, `read`, `extract`) and after
  every result, prioritizing STOP over completion. Enforces shared logical wait (`waits_left`)
  and transient read-error (`MAX_READ_ERRORS`) budgets without replaying creation, attachment,
  navigation, submission, or extraction.
- `cancel(job=None)`:
  When `job` is provided, locally cancels and fences only that job, releasing its lease and
  recording a site tombstone while sibling jobs continue. When `job is None`, globally cancels
  and fences all active jobs and blocks future admissions with `REVIEW_REQUIRED`.
- `collected_receipts()`:
  Returns the list of terminal receipts (`broker.receipts`). Each job emits at most one
  terminal receipt dictionary containing:
  - `site`: configured site name (`str`).
  - `target_id`: acknowledged target ID (`str` or `None` if unknown/unestablished).
  - `session_id`: acknowledged session ID (`str` or `None` if unknown/unestablished).
  - `question_id`: caller-assigned question identifier (`str`).
  - `generation`: monotonic per-origin lease generation (`int`, `0` if lease not acquired).
  - `requested_route`: exact requested `open_url` (`str`).
  - `status`: terminal job status (`"SUCCESS"`, `"STOP"`, `"TIMEOUT"`, `"CANCELLED"`,
    `"LEASE_BUSY"`, `"REVIEW_REQUIRED"`, `"SETUP_ERROR"`, `"SUBMISSION_UNKNOWN"`,
    `"READ_ERROR"`, `"EXTRACTION_ERROR"`, `"EVENT_OVERFLOW"`, or `"ERROR"`).
  - `completion_status`: independent completion status (`"not_started"`, `"pending"`,
    `"complete"`, `"stop"`, `"timeout"`, `"cancelled"`, `"event_overflow"`, `"unknown"`,
    or `"error"`).
  - `extraction_status`: independent extraction status (`"not_attempted"`, `"pending"`,
    `"ok"`, `"blocked"`, or `"error"`).
  - `answer`: extracted answer text (`str` when `status == "SUCCESS"`, else `None`).
  - `error`: failure or fence diagnostic message (`str` or `None`).
  - `submit_attempts`: number of submission callback attempts (`int`, at most 1).
  - `read_attempts`: number of observation/read callback attempts (`int`).
  - `target_ownership`: `"broker"` if created and acknowledged by this job, else `"caller_or_unknown"`.
  - `session_ownership`: `"broker"` if attached and acknowledged by this job, else `"caller_or_unknown"`.
  - `cleanup_status`: cleanup disposition (`"complete"`, `"deferred"`, `"failed"`, or
    `"reconcile_unknown_outcome"`).
  - `cleanup_errors`: list of cleanup error strings (`list[str]`).
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import threading
import time
from unittest.mock import Mock
from urllib.parse import urlsplit

from .cdp_pipe import CdpError
from .cdp_port import _canonical_https_url, _matches_site_prefix, normalize_https_origin


# @anchor:wa.perceive.harvest.normalize_origin
def normalize_origin(url):
    """Validate an explicit HTTPS URL without credentials and return ('https', host_lower, port)."""
    try:
        return normalize_https_origin(url)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


# @anchor:wa.perceive.harvest.HarvestSite
@dataclass(frozen=True)
class HarvestSite:
    """Site configuration with normalized HTTPS origin lease key and prefix matcher."""

    name: str
    prefix: str
    open_url: str
    profile: object = None

    @property
    def lease_key(self):
        return normalize_origin(self.prefix)

    def matches(self, target):
        if not isinstance(target, dict) or target.get("type") != "page":
            return False
        tid = target.get("targetId")
        if not isinstance(tid, str) or not tid:
            return False
        try:
            prefix_origin, canon_prefix = _canonical_https_url(self.prefix)
            return _matches_site_prefix(target.get("url", ""), self.prefix, prefix_origin, canon_prefix)
        except Exception:
            return False


Site = HarvestSite
SiteConfig = HarvestSite


# @anchor:wa.perceive.harvest.CallerTab
@dataclass(frozen=True)
class CallerTab:
    """Explicit caller-owned target/session binding."""

    target_id: str
    session_id: str


ExplicitSelection = CallerTab


# @anchor:wa.perceive.harvest.LeaseToken
@dataclass(frozen=True)
class LeaseToken:
    """W8-style lease credential binding normalized origin, question, generation, holder, epoch, and token."""

    site: tuple
    question_id: str
    generation: int
    holder: str = "harvest-broker"
    epoch: int = 1
    token: str = ""


LeaseCredential = LeaseToken


# @anchor:wa.perceive.harvest.LeaseError
class LeaseError(Exception):
    """Invalid, revoked, or stale lease credential."""


# @anchor:wa.perceive.harvest.LeaseBusy
class LeaseBusy(LeaseError):
    """Normalized site origin already has an active leased question."""


# @anchor:wa.perceive.harvest.FencedError
class FencedError(Exception):
    """Job dispatch is fenced by STOP, timeout, cancellation, or credential invalidation."""


# @anchor:wa.perceive.harvest.TransientReadError
class TransientReadError(Exception):
    """Classified transient read-only observation error eligible for bounded retry."""


LeaseCredentialError = LeaseError
LeaseContentionError = LeaseBusy
FencedDispatchError = FencedError
TransientObservationError = TransientReadError


def _coerce_origin_key(site_or_key):
    if isinstance(site_or_key, tuple) and len(site_or_key) == 3:
        scheme, host, port = site_or_key
        if (
            scheme != "https"
            or not isinstance(host, str)
            or not host
            or host != host.lower()
            or not isinstance(port, int)
            or isinstance(port, bool)
            or port <= 0
            or port > 65535
        ):
            raise ValueError("invalid normalized origin tuple: %r" % (site_or_key,))
        return site_or_key
    if hasattr(site_or_key, "lease_key"):
        return _coerce_origin_key(site_or_key.lease_key)
    if isinstance(site_or_key, str):
        return normalize_origin(site_or_key)
    raise ValueError("unable to derive normalized origin from %r" % (site_or_key,))


# @anchor:wa.perceive.harvest.SharedLeaseAuthority
class SharedLeaseAuthority:
    """Injectable in-memory shared lease authority enforcing per-origin exclusion and atomic credential checks."""

    def __init__(self, initial_epoch=1):
        if not isinstance(initial_epoch, int) or isinstance(initial_epoch, bool) or initial_epoch < 1:
            raise ValueError("initial_epoch must be a positive integer")
        self._lock = threading.RLock()
        self.active = {}
        self.generations = {}
        self.epoch = initial_epoch
        self.site_epochs = {}
        self.revoked_tokens = set()
        self.revoked_holders = set()
        self.tombstones = set()
        self._token_seq = 0

    def min_epoch(self, site):
        origin = _coerce_origin_key(site)
        with self._lock:
            return max(self.epoch, self.site_epochs.get(origin, self.epoch))

    def advance_epoch(self, site=None, new_epoch=None):
        """Advance the authority or per-site epoch, invalidating older credentials."""
        with self._lock:
            if site is None:
                target = (self.epoch + 1) if new_epoch is None else int(new_epoch)
                if target <= self.epoch:
                    raise ValueError("new_epoch must exceed current epoch")
                self.epoch = target
                return self.epoch
            origin = _coerce_origin_key(site)
            cur = self.min_epoch(origin)
            target = (cur + 1) if new_epoch is None else int(new_epoch)
            if target <= cur:
                raise ValueError("new_epoch must exceed current site epoch")
            self.site_epochs[origin] = target
            return target

    def revoke(self, *, token=None, holder=None, site=None, advance_epoch=False):
        """Revoke a token string/LeaseToken, holder identity, or advance epoch."""
        with self._lock:
            if isinstance(token, LeaseToken):
                self.revoked_tokens.add(token.token)
                self.revoked_tokens.add(token)
            elif token is not None:
                self.revoked_tokens.add(token)
            if holder is not None:
                self.revoked_holders.add(holder)
            if advance_epoch:
                self.advance_epoch(site=site)

    def acquire(self, site, question_id, *, holder="harvest-broker", epoch=None, token=None, credential=None):
        """Atomically acquire a per-origin lease with monotonic generation and credential validation."""
        origin = _coerce_origin_key(site)
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("question_id must be a non-empty string")
        with self._lock:
            if credential is not None:
                if isinstance(credential, LeaseToken):
                    holder = credential.holder
                    epoch = credential.epoch
                    token = credential.token
                elif isinstance(credential, dict):
                    holder = credential.get("holder", holder)
                    epoch = credential.get("epoch", epoch)
                    token = credential.get("token", token)
                else:
                    raise LeaseError("unsupported credential object")

            if not isinstance(holder, str) or not holder:
                raise LeaseError("lease holder must be a non-empty string")
            if holder in self.revoked_holders:
                raise LeaseError("lease holder %r is revoked" % (holder,))

            required_epoch = self.min_epoch(origin)
            if epoch is None:
                eff_epoch = required_epoch
            else:
                if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < required_epoch:
                    raise LeaseError("stale or invalid credential epoch %r (min %d)" % (epoch, required_epoch))
                eff_epoch = epoch

            if token is not None and (not isinstance(token, str) or not token):
                raise LeaseError("explicit credential token must be a non-empty string")
            if token is not None and token in self.revoked_tokens:
                raise LeaseError("credential token is revoked")

            if origin in self.active:
                raise LeaseBusy("one active question per site")

            generation = self.generations.get(origin, 0) + 1
            self.generations[origin] = generation
            self._token_seq += 1
            tok_str = token if token is not None else ("lease-%s-%d-%d" % (holder, generation, self._token_seq))
            if tok_str in self.revoked_tokens:
                raise LeaseError("credential token is revoked")

            lease = LeaseToken(
                site=origin,
                question_id=question_id,
                generation=generation,
                holder=holder,
                epoch=eff_epoch,
                token=tok_str,
            )
            self.active[origin] = lease
            return lease

    def validate(
        self,
        lease_token,
        *,
        expected_site=None,
        expected_question_id=None,
        expected_holder=None,
        expected_epoch=None,
        expected_token=None,
    ):
        """Atomically verify that lease_token is the exact active, unrevoked credential."""
        if not isinstance(lease_token, LeaseToken):
            return False
        with self._lock:
            current = self.active.get(lease_token.site)
            if current is not lease_token or current != lease_token:
                return False
            if lease_token.holder in self.revoked_holders:
                return False
            if lease_token.token in self.revoked_tokens or lease_token in self.revoked_tokens:
                return False
            if lease_token.epoch < self.min_epoch(lease_token.site):
                return False
            if expected_site is not None and lease_token.site != _coerce_origin_key(expected_site):
                return False
            if expected_question_id is not None and lease_token.question_id != expected_question_id:
                return False
            if expected_holder is not None and lease_token.holder != expected_holder:
                return False
            if expected_epoch is not None and lease_token.epoch != expected_epoch:
                return False
            if expected_token is not None and lease_token.token != expected_token:
                return False
            return True

    def release(self, lease_token, *, holder=None, epoch=None, release_token=None):
        """Atomically release the active lease only if complete credential and exact token match."""
        if not isinstance(lease_token, LeaseToken):
            return False
        with self._lock:
            current = self.active.get(lease_token.site)
            if current is not lease_token or current != lease_token:
                return False
            if holder is not None and current.holder != holder:
                return False
            if epoch is not None and current.epoch != epoch:
                return False
            if release_token is not None:
                if isinstance(release_token, LeaseToken):
                    if release_token is not current or release_token != current:
                        return False
                elif isinstance(release_token, str):
                    if release_token != current.token:
                        return False
                else:
                    return False
            del self.active[lease_token.site]
            return True

    def reconcile(self, site):
        """Explicitly clear a review-required site tombstone when no active lease is held."""
        origin = _coerce_origin_key(site)
        with self._lock:
            if origin in self.active:
                raise LeaseBusy("cannot reconcile site while lease is active")
            self.tombstones.discard(origin)


# @anchor:wa.perceive.harvest.SessionQueues
class SessionQueues:
    """Bounded per-session event deques with session-isolated purge and consumption."""

    def __init__(self, limit=32):
        self.by_session = {}
        self.limit = limit

    def register(self, sid):
        self.by_session.setdefault(sid, deque())

    def put(self, event):
        sid = event.get("sessionId")
        if sid not in self.by_session:
            return False
        q = self.by_session[sid]
        if len(q) >= self.limit:
            return False
        q.append(deepcopy(event))
        return True

    def take(self, sid, method):
        q = self.by_session.get(sid)
        if q is None:
            return None
        for i, ev in enumerate(q):
            if ev.get("method") == method:
                del q[i]
                return ev
        return None

    def clear_loads(self, sid):
        self.clear_method(sid, "Page.loadEventFired")

    def clear_method(self, sid, method):
        if sid in self.by_session:
            self.by_session[sid] = deque(
                e for e in self.by_session[sid] if e.get("method") != method
            )


def _default_complete(raw):
    if not isinstance(raw, (list, tuple)):
        return False
    by_id = {n["nodeId"]: n for n in raw if isinstance(n, dict) and "nodeId" in n}
    order = by_id.get("root", {}).get("childIds", [])
    names = [by_id[n].get("name", {}).get("value", "") for n in order if n in by_id]
    if "Stop generating" in names:
        return False
    try:
        start = names.index("Assistant fixture response")
        end = names.index("Copy fixture answer", start + 1)
    except ValueError:
        return False
    return any(
        by_id[n].get("role", {}).get("value") == "article"
        for n in order[start + 1:end]
        if n in by_id
    )


def _default_extract(raw):
    if not _default_complete(raw):
        raise ValueError("answer not complete")
    by_id = {n["nodeId"]: n for n in raw if isinstance(n, dict) and "nodeId" in n}
    if "answer" not in by_id:
        raise ValueError("missing answer region")
    text, stack, seen = [], ["answer"], set()
    while stack:
        nid = stack.pop()
        if nid in seen:
            raise ValueError("cyclic answer region")
        seen.add(nid)
        n = by_id[nid]
        if n.get("role", {}).get("value") == "StaticText":
            val = n.get("name", {}).get("value", "")
            if val:
                text.append(val)
        stack.extend(reversed(n.get("childIds", [])))
    answer = "\n".join(text)
    if not answer:
        raise ValueError("empty answer")
    return answer


# @anchor:wa.perceive.harvest.HarvestJob
@dataclass
class HarvestJob:
    """Per-site harvest job state, provenance, budgets, ownership, and terminal receipt."""

    site: object
    question_id: str
    question: str
    holder: str = "harvest-broker"
    epoch: object = None
    requested_token: object = None
    deadline: object = None
    token: object = None
    target_id: object = None
    session_id: object = None
    target_owned: bool = False
    session_owned: bool = False
    create_unknown: bool = False
    attach_unknown: bool = False
    fenced: bool = False
    cancelled: bool = False
    stop_reason: object = None
    phase: str = "ACQUIRING"
    completion: str = "not_started"
    extraction: str = "not_attempted"
    navigation: int = 0
    waits_left: int = 8
    read_attempts: int = 0
    read_errors: int = 0
    submit_attempts: int = 0
    snapshot: object = None
    snapshot_provenance: object = None
    receipt: object = None
    cleanup_obligation: dict = field(default_factory=dict)


# @anchor:wa.perceive.harvest.make_event
def make_event(job, method, **params):
    """Build a provenance-tagged event envelope for a HarvestJob."""
    return {
        "method": method,
        "sessionId": job.session_id,
        "site": job.site.name,
        "target_id": job.target_id,
        "question_id": job.question_id,
        "generation": job.token.generation if job.token else 0,
        "navigation": job.navigation,
        "params": params,
    }


def _coerce_site(site):
    if isinstance(site, dict):
        return HarvestSite(
            name=site["name"],
            prefix=site["prefix"],
            open_url=site["open_url"],
            profile=site.get("profile"),
        )
    if hasattr(site, "name") and hasattr(site, "prefix") and hasattr(site, "open_url"):
        return site
    raise ValueError("invalid site specification: %r" % (site,))


# @anchor:wa.perceive.harvest.HarvestBroker
class HarvestBroker:
    """Cooperative deterministic multi-window harvest broker with shared lease authority."""

    MAX_READ_ERRORS = 3

    def __init__(
        self,
        cdp,
        caller_bindings=None,
        *,
        lease_authority=None,
        leases=None,
        holder="harvest-broker",
        epoch=None,
        token=None,
        clock=None,
        deadline_s=None,
        wait_budget=8,
        max_read_errors=3,
        queue_limit=32,
        submit=None,
        read=None,
        extract=None,
        is_complete=None,
    ):
        self.cdp = cdp
        self.caller_bindings = dict(caller_bindings or {})
        authority = lease_authority if lease_authority is not None else leases
        if authority is None and cdp is not None:
            authority = getattr(cdp, "_shared_lease_authority", None)
            if authority is None:
                authority = SharedLeaseAuthority()
                try:
                    setattr(cdp, "_shared_lease_authority", authority)
                except Exception:
                    pass
        if authority is None:
            authority = SharedLeaseAuthority()
        self.leases = authority
        self.holder = holder
        self.epoch = epoch
        self.default_token = token
        self.clock = clock if callable(clock) else time.monotonic
        self.deadline_s = deadline_s
        self.wait_budget = int(wait_budget)
        self.MAX_READ_ERRORS = int(max_read_errors)
        self.queues = SessionQueues(limit=queue_limit)
        self.jobs = []
        self.by_session = {}
        self.receipts = []
        self.cleanup_obligations = []
        self.tombstones = self.leases.tombstones
        self.cancelled = False
        self.ignored_events = 0
        self.pending_attach = None
        self.early_events = []
        self.is_complete = is_complete if callable(is_complete) else _default_complete
        self.submit = submit if submit is not None else Mock(return_value=None)
        self.read = read if read is not None else Mock(side_effect=self._read_fixture)
        self.extract = extract if extract is not None else Mock(side_effect=_default_extract)

    def collected_receipts(self):
        """Return a shallow copy of collected terminal receipts in completion order."""
        return list(self.receipts)

    def _check_deadline(self, job):
        if job.deadline is not None and self.clock() >= job.deadline:
            if job.receipt is None:
                self._halt(job, "TIMEOUT", "whole-job deadline exceeded")
            raise FencedError("whole-job deadline exceeded")

    def _validate_job_lease(self, job):
        if job.token is None:
            return False
        return self.leases.validate(
            job.token,
            expected_site=job.site.lease_key,
            expected_question_id=job.question_id,
            expected_holder=job.holder,
            expected_epoch=job.epoch,
            expected_token=job.requested_token,
        )

    def _gate(self, job):
        if job.stop_reason is not None:
            if job.receipt is None:
                self._halt(job, "STOP", job.stop_reason)
            raise FencedError("job halted by STOP")
        if self.cancelled or job.cancelled:
            if job.receipt is None:
                self._halt(job, "CANCELLED", "dispatch fenced")
            raise FencedError("job cancelled")
        self._check_deadline(job)
        if job.receipt is not None or job.fenced or not self._validate_job_lease(job):
            raise FencedError("job is no longer allowed to dispatch")

    def command(self, job, method, params=None, session_id=None):
        """Guarded CDP dispatch checking STOP, deadline, cancellation, and lease before send."""
        self._gate(job)
        if session_id is not None and session_id != job.session_id:
            raise ValueError("command session does not belong to this job")
        return self.cdp.send(method, params, session_id=session_id)

    def _bind(self, job):
        prior = self.by_session.get(job.session_id)
        if prior is not None and (prior.receipt is None or prior.fenced):
            raise ValueError("session already belongs to an active or fenced job")
        self.by_session[job.session_id] = job
        self.queues.register(job.session_id)
        if prior is not None:
            self.queues.by_session[job.session_id].clear()

    def _refresh_receipt_identity(self, job):
        obligation = {
            "site": job.site.name,
            "lease_key": None,
            "question_id": job.question_id,
            "generation": job.token.generation if job.token else 0,
            "target_id": job.target_id,
            "session_id": job.session_id,
            "target_owned": job.target_owned,
            "session_owned": job.session_owned,
            "create_unknown": job.create_unknown,
            "attach_unknown": job.attach_unknown,
        }
        try:
            obligation["lease_key"] = job.site.lease_key
        except Exception:
            pass
        job.cleanup_obligation.update(obligation)
        if job.receipt is not None:
            job.receipt.update(
                target_id=job.target_id,
                session_id=job.session_id,
                target_ownership="broker" if job.target_owned else "caller_or_unknown",
                session_ownership="broker" if job.session_owned else "caller_or_unknown",
            )

    def ensure_tab(self, job, selected_target=None, caller_tab=None):
        """Open or reuse a site tab under the job's lease, attach, replay early events, enable, and navigate."""
        site = job.site
        if normalize_origin(site.open_url) != site.lease_key:
            raise ValueError("requested route is outside the configured site")
        if caller_tab is not None:
            if (
                not hasattr(caller_tab, "session_id")
                or not hasattr(caller_tab, "target_id")
                or self.caller_bindings.get(caller_tab.session_id) != caller_tab.target_id
            ):
                raise ValueError("caller session/target binding is not registered")
        if selected_target is not None and (not isinstance(selected_target, str) or not selected_target.strip()):
            raise ValueError("explicit target is not a matching page")

        targets_reply = self.command(job, "Target.getTargets")
        targets = targets_reply.get("targetInfos", []) if isinstance(targets_reply, dict) else []
        matches = [t for t in targets if site.matches(t)]
        if caller_tab is not None:
            selected_target = caller_tab.target_id
        if selected_target is not None:
            matches = [t for t in matches if t.get("targetId") == selected_target]
            if len(matches) != 1:
                raise ValueError("explicit target is not a matching page")
        if len(matches) > 1:
            raise ValueError("AMBIGUOUS_TARGET: explicit selection required")

        if matches:
            job.target_id = matches[0]["targetId"]
        else:
            job.create_unknown = True
            self._refresh_receipt_identity(job)
            result = self.command(job, "Target.createTarget", {"url": site.open_url})
            tid = result.get("targetId") if isinstance(result, dict) else None
            if (
                not isinstance(tid, str)
                or not tid
                or any(isinstance(t, dict) and t.get("targetId") == tid for t in targets)
            ):
                raise ValueError("creation outcome has no fresh target identity")
            job.target_id, job.target_owned, job.create_unknown = tid, True, False
        self._refresh_receipt_identity(job)

        if caller_tab is None:
            job.attach_unknown = True
            self._refresh_receipt_identity(job)
            self.pending_attach, self.early_events = job, []
            try:
                result = self.command(
                    job,
                    "Target.attachToTarget",
                    {"targetId": job.target_id, "flatten": True},
                )
            except Exception:
                if any(self._stop_evidence(e) for e in self.early_events):
                    job.fenced = True
                raise
            finally:
                early = self.early_events
                self.pending_attach, self.early_events = None, []
            sid = result.get("sessionId") if isinstance(result, dict) else None
            if not isinstance(sid, str) or not sid:
                raise ValueError("attach outcome has no session identity")
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
        """Purge only this job's buffered load events and dispatch session-scoped Page.navigate."""
        self._gate(job)
        if job.submit_attempts:
            raise ValueError("cannot navigate an already submitted question")
        self.queues.clear_loads(job.session_id)
        job.navigation += 1
        result = self.command(
            job,
            "Page.navigate",
            {"url": job.site.open_url},
            session_id=job.session_id,
        )
        if not isinstance(result, dict):
            raise CdpError("navigate returned invalid response")
        if result.get("errorText"):
            raise CdpError(result["errorText"])
        self._gate(job)
        job.phase = "NAVIGATING"

    def start(
        self,
        site,
        question_id="question-1",
        question="One shared question",
        selected_target=None,
        caller_tab=None,
        *,
        credential=None,
        holder=None,
        epoch=None,
        token=None,
        deadline=None,
        timeout=None,
    ):
        """Admit and start a harvest job on a site under a shared per-origin lease."""
        site_obj = _coerce_site(site)
        eff_holder = holder if holder is not None else self.holder
        eff_epoch = epoch if epoch is not None else self.epoch
        eff_token = token if token is not None else self.default_token
        if isinstance(credential, LeaseToken):
            eff_holder = credential.holder
            eff_epoch = credential.epoch
            eff_token = credential.token
        elif isinstance(credential, dict):
            eff_holder = credential.get("holder", eff_holder)
            eff_epoch = credential.get("epoch", eff_epoch)
            eff_token = credential.get("token", eff_token)

        now = self.clock()
        if deadline is not None:
            job_deadline = deadline
        elif timeout is not None:
            job_deadline = now + timeout
        elif self.deadline_s is not None:
            job_deadline = now + self.deadline_s
        else:
            job_deadline = None

        job = HarvestJob(
            site=site_obj,
            question_id=question_id,
            question=question,
            holder=eff_holder,
            epoch=eff_epoch,
            requested_token=eff_token,
            deadline=job_deadline,
            waits_left=self.wait_budget,
        )
        self.jobs.append(job)
        try:
            if job.deadline is not None and self.clock() >= job.deadline:
                self._halt(job, "TIMEOUT", "whole-job deadline exceeded before lease admission")
                return job
            lease_key = site_obj.lease_key
            job.token = self.leases.acquire(
                lease_key,
                question_id,
                holder=eff_holder,
                epoch=eff_epoch,
                token=eff_token,
                credential=credential,
            )
            job.epoch = job.token.epoch
            job.requested_token = job.token.token
            if job.deadline is not None and self.clock() >= job.deadline:
                self._halt(job, "TIMEOUT", "whole-job deadline exceeded during lease admission")
                return job
            if self.cancelled or lease_key in self.tombstones:
                job.fenced = True
                return self._finish(job, "REVIEW_REQUIRED", error="terminal fence persists")
            self.ensure_tab(job, selected_target=selected_target, caller_tab=caller_tab)
        except LeaseBusy as exc:
            self._finish(job, "LEASE_BUSY", error=str(exc))
        except FencedError:
            if job.receipt is None:
                self._halt(job, "CANCELLED", "dispatch fenced")
        except Exception as exc:
            if job.receipt is None:
                self._finish(job, "SETUP_ERROR", completion="error", error=str(exc))
        return job

    def _cleanup(self, job):
        errors = []
        if job.fenced or job.cancelled or self.cancelled:
            return "deferred", errors
        commands = []
        if job.session_owned and job.session_id is not None:
            commands.append(("Target.detachFromTarget", {"sessionId": job.session_id}))
        if job.target_owned and job.target_id is not None:
            commands.append(("Target.closeTarget", {"targetId": job.target_id}))
        for method, params in commands:
            if job.fenced or job.cancelled or self.cancelled or not self._validate_job_lease(job):
                return "deferred", errors
            try:
                result = self.cdp.send(method, params)
                if method == "Target.closeTarget" and (not isinstance(result, dict) or result.get("success") is not True):
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
            "site": job.site.name,
            "target_id": job.target_id,
            "session_id": job.session_id,
            "question_id": job.question_id,
            "generation": job.token.generation if job.token else 0,
            "requested_route": job.site.open_url,
            "status": status,
            "completion_status": job.completion,
            "extraction_status": job.extraction,
            "answer": answer if status == "SUCCESS" else None,
            "error": error,
            "submit_attempts": job.submit_attempts,
            "read_attempts": job.read_attempts,
            "target_ownership": "broker" if job.target_owned else "caller_or_unknown",
            "session_ownership": "broker" if job.session_owned else "caller_or_unknown",
        }
        self._refresh_receipt_identity(job)
        cleanup, errors = "failed", []
        try:
            cleanup, errors = self._cleanup(job)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            job.receipt.update(cleanup_status=cleanup, cleanup_errors=errors)
            job.cleanup_obligation["cleanup_status"] = cleanup
            job.cleanup_obligation["cleanup_errors"] = list(errors)
            if cleanup != "complete":
                try:
                    self.tombstones.add(job.site.lease_key)
                except Exception:
                    pass
                if job.cleanup_obligation not in self.cleanup_obligations:
                    self.cleanup_obligations.append(job.cleanup_obligation)
            if job.token is not None:
                self.leases.release(
                    job.token,
                    holder=job.holder,
                    epoch=job.epoch,
                    release_token=job.token,
                )
            self.receipts.append(job.receipt)
        return job

    def _halt(self, job, status, reason):
        job.fenced = True
        try:
            self.tombstones.add(job.site.lease_key)
        except Exception:
            pass
        completion = job.completion if job.completion == "complete" else status.lower()
        extraction = job.extraction if job.extraction == "ok" else "blocked"
        self._finish(job, status, completion=completion, extraction=extraction, error=reason)

    def cancel(self, job=None):
        """Cancel a single job locally or cancel the entire harvest globally when job is None."""
        if job is None:
            self.cancelled = True
            for active in self.jobs:
                if active.receipt is None:
                    active.cancelled = True
                    self._halt(active, "CANCELLED", "caller cancelled the harvest")
        else:
            job.cancelled = True
            if job.receipt is None:
                self._halt(job, "CANCELLED", "caller cancelled this job")

    @staticmethod
    def _stop_evidence(event):
        if not isinstance(event, dict):
            return None
        params = event.get("params", {}) or {}
        url = params.get("url", (params.get("frame") or {}).get("url", ""))
        if isinstance(url, str) and url:
            parsed = urlsplit(url)
            if (
                parsed.hostname in ("www.google.com", "google.com")
                and (parsed.path == "/sorry" or parsed.path.startswith("/sorry/"))
            ):
                return "observed /sorry/ redirect"
        for n in params.get("nodes", []) or []:
            if (
                isinstance(n, dict)
                and (n.get("role") or {}).get("value") == "alert"
                and "unusual traffic" in ((n.get("name") or {}).get("value") or "").lower()
            ):
                return "observed fixture unusual-traffic alert"
        return None

    def feed(self, event):
        """Route one event envelope into its bounded session queue with provenance and STOP checks."""
        if not isinstance(event, dict):
            self.ignored_events += 1
            return
        sid = event.get("sessionId")
        job = self.by_session.get(sid) if isinstance(sid, str) and sid else None
        if job is None:
            if self.pending_attach is not None:
                if len(self.early_events) >= self.queues.limit:
                    self._halt(self.pending_attach, "EVENT_OVERFLOW", "early attachment queue is full")
                else:
                    self.early_events.append(deepcopy(event))
                return
            self.ignored_events += 1
            return

        needs_envelope = event.get("method") in ("fixture.ax", "Page.loadEventFired")
        has_provenance_keys = any(
            k in event for k in ("question_id", "generation", "navigation", "site", "target_id", "targetId")
        )
        if (needs_envelope or has_provenance_keys) and not self._correlated(job, event):
            self.ignored_events += 1
            return

        stop = self._stop_evidence(event)
        if stop:
            if job.receipt is None:
                job.stop_reason = stop
                self._halt(job, "STOP", stop)
            else:
                self.ignored_events += 1
            return

        if job.receipt is not None:
            self.ignored_events += 1
            return

        if not self.queues.put(event):
            self._halt(job, "EVENT_OVERFLOW", "bounded session queue is full")

    @staticmethod
    def _correlated(job, event):
        if not isinstance(event, dict) or job.token is None:
            return False
        if event.get("sessionId") != job.session_id:
            return False
        if event.get("question_id") != job.question_id:
            return False
        if event.get("generation") != job.token.generation:
            return False
        if event.get("navigation") != job.navigation:
            return False
        if "site" in event and event["site"] is not None:
            if event["site"] not in (job.site.name, job.site.lease_key):
                return False
        target_tag = event.get("target_id", event.get("targetId"))
        if target_tag is not None and target_tag != job.target_id:
            return False
        return True

    def _take_work(self, job, method):
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

    def step(self, job=None):
        """Advance one job (or all active jobs in admission order when job is None) by one step."""
        if job is None:
            for active in list(self.jobs):
                if active.receipt is None:
                    self.step(active)
            return
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
                    self.submit(job)
                    self._gate(job)
                except FencedError:
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
                except Exception as exc:
                    if isinstance(exc, FencedError):
                        raise
                    if isinstance(exc, TransientReadError) or exc.__class__.__name__ == "_TransientRead":
                        self._gate(job)
                        job.read_errors += 1
                        if job.read_errors >= self.MAX_READ_ERRORS:
                            self._finish(job, "READ_ERROR", completion="error", error=str(exc))
                        else:
                            self._wait_step(job)
                        return
                    raise
                if raw is not None and self.is_complete(raw):
                    job.snapshot = deepcopy(raw)
                    job.snapshot_provenance = (
                        job.site.lease_key,
                        job.target_id,
                        job.session_id,
                        job.question_id,
                        job.token.generation,
                        job.navigation,
                    )
                    job.completion, job.phase = "complete", "EXTRACT"
                else:
                    self._wait_step(job)
            elif job.phase == "EXTRACT":
                expected_prov = (
                    job.site.lease_key,
                    job.target_id,
                    job.session_id,
                    job.question_id,
                    job.token.generation if job.token else None,
                    job.navigation,
                )
                if job.completion != "complete" or job.snapshot is None or job.snapshot_provenance != expected_prov:
                    self._finish(job, "EXTRACTION_ERROR", extraction="error", error="missing or uncorrelated snapshot")
                    return
                job.extraction = "pending"
                try:
                    extracted = self.extract(job.snapshot)
                    self._gate(job)
                    if isinstance(extracted, dict):
                        if "site" in extracted and extracted["site"] not in (job.site.name, job.site.lease_key):
                            raise ValueError("extracted answer site provenance mismatch")
                        if "target_id" in extracted and extracted["target_id"] != job.target_id:
                            raise ValueError("extracted answer target provenance mismatch")
                        if "session_id" in extracted and extracted["session_id"] != job.session_id:
                            raise ValueError("extracted answer session provenance mismatch")
                        if "question_id" in extracted and extracted["question_id"] != job.question_id:
                            raise ValueError("extracted answer question provenance mismatch")
                        if "generation" in extracted and extracted["generation"] != job.token.generation:
                            raise ValueError("extracted answer generation provenance mismatch")
                        if "navigation" in extracted and extracted["navigation"] != job.navigation:
                            raise ValueError("extracted answer navigation provenance mismatch")
                        answer = extracted.get("answer")
                    else:
                        answer = extracted
                    if not isinstance(answer, str) or not answer:
                        raise ValueError("extractor returned no answer")
                    self._finish(job, "SUCCESS", extraction="ok", answer=answer)
                except FencedError:
                    raise
                except Exception as exc:
                    self._finish(job, "EXTRACTION_ERROR", extraction="error", error=str(exc))
        except FencedError:
            if job.receipt is None:
                self._halt(job, "CANCELLED", "dispatch fenced")
        except Exception as exc:
            if job.receipt is None:
                self._finish(job, "ERROR", completion="error", error=str(exc))


SessionEventBuffers = SessionQueues
