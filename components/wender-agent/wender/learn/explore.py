# @module {"id":"wa.learn.explore","layer":2,"owner":"LEARN","deps":["wa.state.schema","wa.state.diff","wa.remember.program_map","wa.perceive.stopstate"],"book":"HB-WA-s3-remember"}
"""@module wa.learn.explore - deterministic program-map exploration loop."""
from abc import ABC, abstractmethod
from copy import deepcopy

from wender.remember.program_map import ProgramMap, state_key
from wender.state.diff import diff_states, summarize
from wender.state.schema import dumps


def _detect_stopstate(state):
    """Use F1 when installed; None explicitly means detection is unavailable."""
    try:
        from wender.perceive.stopstate import detect
    except ModuleNotFoundError as exc:
        if exc.name != "wender.perceive.stopstate":
            raise
        return None

    verdict = detect(state)
    if not isinstance(verdict, dict) or type(verdict.get("is_stop")) is not bool:
        raise ValueError("stopstate.detect must return an exact boolean is_stop")
    if verdict["is_stop"] and not (
            isinstance(verdict.get("reason"), str) and verdict["reason"].strip()):
        raise ValueError("stopstate.detect STOP requires a reason")
    return deepcopy(verdict)


# @anchor:wa.learn.explore.Driver
class Driver(ABC):
    """Abstract base class for state exploration drivers."""

    @abstractmethod
    def observe(self):
        """Return tuple (state, screen_fp) where state is a STATE dict and screen_fp is hex string or None."""
        pass

    @abstractmethod
    def candidate_actions(self, state):
        """Return list of action dicts with keys op, target and irreversible in deterministic order."""
        pass

    @abstractmethod
    def act(self, action):
        """Perform action, returning None or raising on failure."""
        pass


# @anchor:wa.learn.explore.explore
def explore(driver, program, max_steps=32, reset=None):
    """Deterministically explore a program using driver to build a ProgramMap.

    Returns dict with keys:
      - program_map: ProgramMap instance
      - steps: list of step record dicts (pre, action, post, changed)
      - skipped: list of skipped irreversible action dicts
      - stopped: 'budget', 'exhausted', 'error', or 'stopstate'
      - stopstate: STOP receipt, or 'unavailable' when F1 is not installed

    When installed, the real F1 detector checks every new observation before
    accepting a map node or requesting candidates. A STOP receipt is latched for
    this attempt: no later action, observation, or reset follows it. Until the
    separate F1 task lands, preserve legacy exploration and report unavailable
    detection explicitly; an unavailable detector is not a no-wall verdict.
    """
    pm = ProgramMap(program)
    visited_edges = set()
    steps = []
    skipped = []
    skipped_set = set()
    frontier = {}   # state key -> canonical reversible actions observed there (deterministic order)
    moves = {}      # state key -> {canonical action: post state key}, the transitions seen so far
    step_count = 0
    stopped = None
    stop_receipt = None
    stopstate_unavailable = False
    failure = None

    def note(sk, actions):
        for action in actions:
            action = deepcopy(action)
            canon = dumps(action)
            if action.get("irreversible") is True:
                if canon not in skipped_set:
                    skipped_set.add(canon)
                    skipped.append(action)
            elif canon not in frontier.setdefault(sk, {}):
                frontier[sk][canon] = action

    def untried(sk):
        return next((a for c, a in frontier.get(sk, {}).items() if (sk, c) not in visited_edges), None)

    def route(src):
        """Shortest known action path (BFS, insertion order) from src to a state with an untried action."""
        prev, queue = {src: None}, [src]
        while queue:
            cur = queue.pop(0)
            if cur != src and untried(cur) is not None:
                path = []
                while prev[cur] is not None:
                    cur, action = prev[cur]
                    path.append((cur, action))
                return path[::-1]
            for canon, post in moves.get(cur, {}).items():
                if post not in prev:
                    prev[post] = (cur, frontier[cur][canon])
                    queue.append(post)
        return None

    def checked_observe():
        nonlocal stopped, stop_receipt, stopstate_unavailable
        observed, fingerprint = driver.observe()
        # Drivers may reuse mutable STATE objects between observations.
        observed = deepcopy(observed)
        verdict = _detect_stopstate(observed)
        if verdict is None:
            stopstate_unavailable = True
        elif verdict["is_stop"]:
            stopped = "stopstate"
            stop_receipt = verdict
            return None
        return observed, fingerprint

    def finish():
        result = {"program_map": pm, "steps": steps, "skipped": skipped,
                  "stopped": stopped or "budget"}
        if stop_receipt is not None:
            result["stopstate"] = stop_receipt
        elif stopstate_unavailable:
            result["stopstate"] = "unavailable"
        if failure is not None:
            result["failure"] = failure
        return result

    try:
        initial = checked_observe()
    except Exception as exc:
        stopped = "error"
        failure = {"stage": "observe", "type": type(exc).__name__}
        return finish()
    if initial is None:
        return finish()
    state, screen_fp = initial

    def do(sk, action):
        nonlocal state, screen_fp, step_count
        canonical = dumps(action)
        action = deepcopy(action)
        visited_edges.add((sk, canonical))
        driver.act(deepcopy(action))
        step_count += 1
        observed = checked_observe()
        if observed is None:
            return sk
        post_state, post_screen_fp = observed
        post_sk = pm.observe(post_screen_fp, post_state.get("ax_stable_sha256"))
        note(post_sk, driver.candidate_actions(deepcopy(post_state)) or [])
        changed_str = summarize(diff_states(state, post_state))
        pm.add_edge(sk, action, post_sk, changed_str)
        moves.setdefault(sk, {})[canonical] = post_sk
        steps.append({"pre": sk, "action": deepcopy(action), "post": post_sk,
                      "changed": deepcopy(changed_str)})
        state, screen_fp = post_state, post_screen_fp
        return post_sk

    try:
        sk = pm.observe(screen_fp, state.get("ax_stable_sha256"))
        note(sk, driver.candidate_actions(deepcopy(state)) or [])
    except Exception as exc:
        stopped = "error"
        failure = {"stage": "candidates", "type": type(exc).__name__}
        return finish()
    reset_tried = False
    while stopped is None and step_count < max_steps:
        action = untried(sk)
        if action is None:
            path = route(sk)
            if path is None and reset is not None and not reset_tried:
                try:
                    reset()
                    observed = checked_observe()
                    if observed is None:
                        break
                    state, screen_fp = observed
                    sk = pm.observe(screen_fp, state.get("ax_stable_sha256"))
                    note(sk, driver.candidate_actions(deepcopy(state)) or [])
                except Exception as exc:
                    stopped = "error"
                    failure = {"stage": "reset", "type": type(exc).__name__}
                    break
                reset_tried = True
                continue
            if not path:
                stopped = "exhausted"
                break
            try:
                for expected, hop in path:   # replay known edges; if the app drifted, explore from where it is
                    if stopped is not None or step_count >= max_steps or sk != expected:
                        break
                    sk = do(sk, hop)
            except Exception:
                stopped = "error"
                break
            continue
        reset_tried = False
        try:
            sk = do(sk, action)
        except Exception:
            stopped = "error"
            break

    return finish()
