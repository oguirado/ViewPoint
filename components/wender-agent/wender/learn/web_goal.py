# @module {"id":"wa.learn.web_goal","layer":4,"owner":"LEARN","deps":["wa.learn.cu_loop","wa.learn.model_client","wa.learn.script_live","wa.perceive.srf_identity"],"book":"HB-WA-s4-learn"}
"""Scripts-off, episode-local goal adapter. Observations are never persistence."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import time

from wender.learn.cu_loop import (BridgeDescriber, ModelDecider, run_loop,
                                  _validate_frame, _fixture_rgb)
from wender.learn.model_client import MuseBackend, ActionValidationError
from wender.learn.script_live import LegacyDispatchOwner
from wender.perceive.srf_identity import SurfaceError, SurfaceRefused, _eligible

_PHRASES = ('account', 'profile', 'billing', 'plan', 'upgrade', 'subscription',
            'subscribe', 'try pro', 'settings', 'sign out', 'logout', 'log out',
            'delete', 'share', 'publish', 'export', 'cuenta', 'perfil', 'facturacion',
            'facturación', 'mejorar', 'suscribirse', 'suscripcion', 'suscripción',
            'ajustes', 'configuracion', 'configuración', 'cerrar sesion',
            'cerrar sesión', 'eliminar', 'borrar', 'compartir', 'publicar', 'exportar')


_FAILURE_CODES = frozenset(('usage', 'config_proxy', 'not_ready', 'refused_focus',
    'malformed_capture', 'producer_failure', 'alignment', 'config',
    'goal_owner', 'goal_stale', 'goal_grounding', 'goal_deadline', 'goal_fields',
    'goal_upstream_filter', 'goal_center', 'goal_ax_preferred', 'goal_focus',
    'goal_ascii', 'goal_key', 'generation_key', 'generation_control',
    'goal_ineligible', 'goal_blocked', 'goal_duplicate', 'goal_projection',
    'goal_vision', 'goal_marks', 'goal_overlay'))


# @anchor:wa.learn.web_goal._failure_code
def _failure_code(exc):
    return exc.code if isinstance(exc, SurfaceError) and exc.code in _FAILURE_CODES else 'goal_failure'


# @anchor:wa.learn.web_goal._has
def _has(text, phrase):
    return re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text.casefold()) is not None


# @anchor:wa.learn.web_goal.GoalPolicy
@dataclass(frozen=True)
class GoalPolicy:
    goal: str
    deadline: float
    clock: object

    # @anchor:wa.learn.web_goal.GoalPolicy.__post_init__
    def __post_init__(self):
        if not isinstance(self.goal, str) or not self.goal.strip() or not self.goal.isascii():
            raise ValueError('GOAL_ASCII')

    # @anchor:wa.learn.web_goal.GoalPolicy.check_deadline
    def check_deadline(self):
        if self.clock() >= self.deadline:
            raise TimeoutError('GOAL_DEADLINE')

    # @anchor:wa.learn.web_goal.GoalPolicy.check_target
    def check_target(self, target):
        self.check_deadline()
        name = target.get('name', '')
        if not isinstance(name, str) or not _eligible(target):
            raise SurfaceRefused('goal_ineligible', 'input')
        if re.search(r'^(?:Stop|Detener)\b', name, re.I):
            raise SurfaceRefused('generation_control', 'input')
        if any(_has(name, phrase) and not _has(self.goal, phrase) for phrase in _PHRASES):
            raise SurfaceRefused('goal_blocked', 'input')


    # @anchor:wa.learn.web_goal.GoalPolicy.covers_mark
    def covers_mark(self, target):
        # Structural AX containers and text do not consume a canvas region.
        return (target.get('actionable') is True or target.get('role', '').casefold() in
                ('button', 'textbox', 'entry', 'textarea', 'searchbox', 'combobox',
                 'link', 'menuitem', 'checkbox', 'radio', 'slider', 'switch', 'tab', 'spinbutton'))


# @anchor:wa.learn.web_goal.GoalMetrics
class GoalMetrics:
    # @anchor:wa.learn.web_goal.GoalMetrics.__init__
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.provider_attempts = self.acquisition_attempts = self.read_retries = self.dispatched_inputs = 0
        self.validation_errors = []
        self.actions = []
        self.stages = {stage: {'total': 0, 'count': 0} for stage in
                       ('setup', 'acquisition', 'vision', 'model', 'dispatch', 'finalization', 'total')}

    # @anchor:wa.learn.web_goal.GoalMetrics.measure
    @contextmanager
    def measure(self, stage):
        start = self.clock()
        try:
            yield
        finally:
            self.stages[stage]['total'] += max(0, round((self.clock() - start) * 1_000_000))
            self.stages[stage]['count'] += 1


# @anchor:wa.learn.web_goal.GoalCapsule
class GoalCapsule:
    # @anchor:wa.learn.web_goal.GoalCapsule.__init__
    def __init__(self, surface, *, owner, policy, metrics, deadline, clock, sleep, vision_only=False):
        self.surface, self.owner, self.policy, self.metrics = surface, owner, policy, metrics
        self.vision_only = vision_only is True
        self.deadline, self.clock, self.sleep = deadline, clock, sleep
        self.tick = 0
        self.targets = {}
        self.failure_code = None
        self.input_effect_unknown = False

    # @anchor:wa.learn.web_goal.GoalCapsule.frame
    def frame(self, *, state=True):
        self.failure_code = None
        self.policy.check_deadline()
        if not self.owner.check_ready():
            raise SurfaceRefused('goal_owner', 'frame')
        self.surface._goal_targets = None
        with self.metrics.measure('acquisition'):
            for attempt in range(15):
                self.policy.check_deadline()
                self.metrics.acquisition_attempts += 1
                self.surface.timeout = min(30, self.deadline - self.clock())
                try:
                    self.surface.frame()
                    self.policy.check_deadline()
                    self.surface.timeout = min(30, self.deadline - self.clock())
                    # Vision-only capsules (Chromium without the debugging pipe, e.g. Google sign-in
                    # identities) have no AX: targets come only from vision-engine marks. [MEASURED]
                    ax = {'elements': [], 'truncated': False} if self.vision_only else self.surface.ax()
                    self.policy.check_deadline()
                    try:
                        frame = _validate_frame(self.surface.last_frame)
                    except (KeyError, TypeError, ValueError, OSError):
                        raise SurfaceError('malformed_capture', 'frame') from None
                    break
                except SurfaceError as exc:
                    if exc.operation not in ('frame', 'ax') or exc.code not in ('not_ready', 'refused_focus') or attempt == 14:
                        self.failure_code = _failure_code(exc)
                        raise
                    self.metrics.read_retries += 1
                    self.sleep(min(.2, max(0, self.deadline - self.clock())))
        self.policy.check_deadline()
        rows = ax['elements']
        ids = [e.get('id') for e in rows if isinstance(e, dict)]
        if len(ids) != len(rows) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            self.failure_code = 'goal_duplicate'
            raise SurfaceRefused('goal_duplicate', 'frame')
        self.tick += 1
        self.targets = {}
        projected, containers = [], {}
        for index, target in enumerate(rows):
            if not isinstance(target.get('name', ''), str) or not isinstance(target.get('role', ''), str):
                self.failure_code = 'goal_projection'
                raise SurfaceRefused('goal_projection', 'frame')
            box = target.get('box')
            if box is not None and (not isinstance(box, list) or len(box) != 4 or
                    any(type(v) is not int for v in box) or
                    min(box[:2]) < 0 or min(box[2:]) <= 0 or
                    box[0]+box[2] > frame['width'] or box[1]+box[3] > frame['height']):
                self.failure_code = 'goal_projection'
                raise SurfaceRefused('goal_projection', 'frame')
            opaque = f'e{self.tick}_{index}'
            self.targets[opaque] = copy.deepcopy(target)
            container = target.get('container')
            # Producer scope never crosses the projection boundary.
            ordinal = containers.setdefault(str(container), len(containers))
            states = target.get('states', {})
            if not isinstance(states, dict):
                self.failure_code = 'goal_projection'
                raise SurfaceRefused('goal_projection', 'frame')
            row = dict(id=opaque, role=target.get('role', ''), name=target.get('name', '')[:160],
                       bbox=None if box is None else dict(zip(('x', 'y', 'w', 'h'), box)),
                       actionable=_eligible(target), region=target.get('region') if target.get('region') in ('main', 'navigation', 'toolbar', 'sidebar') else 'other',
                       container=f'c{ordinal}', states={k: v for k, v in states.items() if k in
                       ('enabled', 'focused', 'expanded', 'checked', 'selected', 'disabled', 'hidden', 'busy') and type(v) is bool})
            if isinstance(target.get('value'), str):
                row['value_len'] = len(target['value'])
            projected.append(row)
        # Frozen table caps presentation; private custody includes all controls.
        projected = projected[:60]
        digest = hashlib.sha256(json.dumps(projected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        frame['lane'] = dict(elements=projected, state_sha256=digest, tick=self.tick, revision=self.tick)
        self.surface.publish_goal_targets(frame_seq=frame['seq'], generation=frame['generation'],
            frame_digest=frame['sha256_rgb24'], elements=self.targets, marks={})
        return frame

    # @anchor:wa.learn.web_goal.GoalCapsule.input
    def input(self, action):
        # Structural action log for diagnosis: kind, target id and text length only (never the text).
        self.metrics.actions.append({'kind': action.get('kind'), 'mark_id': action.get('mark_id'),
            'element_id': action.get('element_id'), 'keysym': action.get('keysym') if action.get('kind') == 'key' else None,
            'text_len': len(action['text']) if isinstance(action.get('text'), str) else None})
        try:
            self.surface.prepare_goal_input(action)
        except SurfaceError as exc:
            self.failure_code = _failure_code(exc)
            raise
        with self.metrics.measure('dispatch'):
            def send():
                # Revalidate at the owner's physical crossing, not just proposal time.
                self.surface.prepare_goal_input(action)
                self.metrics.dispatched_inputs += 1
                return self.surface.input(action)
            try:
                receipt = self.owner.dispatch(self.surface, action, send)
                self.policy.check_deadline()
                return {'input_mono_ns': receipt['input_mono_ns']}
            except BaseException as exc:
                self.input_effect_unknown = True
                self.failure_code = _failure_code(exc) if isinstance(exc, SurfaceError) else 'goal_input_uncertain'
                self.owner.uncertain(action)
                raise


# @anchor:wa.learn.web_goal.GoalDescriber
class GoalDescriber(BridgeDescriber):
    # @anchor:wa.learn.web_goal.GoalDescriber.__init__
    def __init__(self, ve_root=None, *, capsule, policy, metrics):
        super().__init__(ve_root, marks=True)
        self.capsule, self.policy, self.metrics = capsule, policy, metrics

    # @anchor:wa.learn.web_goal.GoalDescriber.describe
    def describe(self, frame, *, prev=None):
        self.capsule.failure_code = None
        self.policy.check_deadline()
        self.engine.timeout = min(30, self.policy.deadline - self.policy.clock())
        with self.metrics.measure('vision'):
            result = super().describe(frame, prev=prev)
        self.policy.check_deadline()
        if (result.get('schema') != 've.cu_describe/v1' or
                (result.get('width'), result.get('height'), result.get('frame_sha256')) !=
                (frame['width'], frame['height'], frame['sha256_rgb24'])):
            self.capsule.failure_code = 'goal_vision'
            raise SurfaceRefused('goal_vision', 'describe')
        marks, seen = {}, set()
        for mark in result.get('marks', ()):
            if not isinstance(mark, dict) or (type(mark.get('id')) is not int or mark['id'] <= 0) or mark['id'] in seen:
                self.capsule.failure_code = 'goal_marks'
                raise SurfaceRefused('goal_marks', 'describe')
            seen.add(mark['id'])
            if any(type(mark.get(k)) is not int for k in ('x', 'y', 'w', 'h')):
                self.capsule.failure_code = 'goal_marks'
                raise SurfaceRefused('goal_marks', 'describe')
            x, y, w, h = (mark[k] for k in ('x', 'y', 'w', 'h'))
            if min(x, y) < 0 or min(w, h) <= 0 or x+w > frame['width'] or y+h > frame['height']:
                self.capsule.failure_code = 'goal_marks'
                raise SurfaceRefused('goal_marks', 'describe')
            cx, cy = x+w//2, y+h//2
            covered = False
            for target in self.capsule.targets.values():
                box = target.get('box')
                if self.policy.covers_mark(target) and box and box[0] <= cx < box[0]+box[2] and box[1] <= cy < box[1]+box[3]:
                    covered = True
            if not covered:
                marks[mark['id']] = copy.deepcopy(mark)
        overlay = result.get('marks_png')
        try:
            overlay_path = Path(overlay).resolve(strict=True) if overlay else None
        except (OSError, TypeError, ValueError):
            self.capsule.failure_code = 'goal_overlay'
            raise SurfaceRefused('goal_overlay', 'describe') from None
        if marks and (not overlay or self._marks_dir is None or
                      not overlay_path.is_relative_to(Path(self._marks_dir.name).resolve())):
            self.capsule.failure_code = 'goal_overlay'
            raise SurfaceRefused('goal_overlay', 'describe')
        if marks:
            try:
                _, width, height = _fixture_rgb(overlay_path)
            except (OSError, TypeError, ValueError):
                self.capsule.failure_code = 'goal_overlay'
                raise SurfaceRefused('goal_overlay', 'describe') from None
            if (width, height) != (frame['width'], frame['height']):
                self.capsule.failure_code = 'goal_overlay'
                raise SurfaceRefused('goal_overlay', 'describe')
        result['marks'] = list(marks.values())
        if not marks:
            result['marks_png'] = None
        self.capsule.surface.publish_goal_targets(frame_seq=frame['seq'], generation=frame['generation'],
            frame_digest=frame['sha256_rgb24'], elements=self.capsule.targets, marks=marks)
        return result


# @anchor:wa.learn.web_goal.GoalModelDecider
class GoalModelDecider(ModelDecider):
    # @anchor:wa.learn.web_goal.GoalModelDecider.__init__
    def __init__(self, backend, *, policy, metrics):
        super().__init__(backend, images='marks')
        self.policy, self.metrics = policy, metrics

    # @anchor:wa.learn.web_goal.GoalModelDecider.decide
    def decide(self, *, goal, frame, description, feedback=None):
        self.policy.check_deadline()
        try:
            result = super().decide(goal=goal, frame=frame, description=description, feedback=feedback)
        except ActionValidationError as exc:
            # Our own validator messages (closed strings), never page text.
            self.metrics.validation_errors.append(str(exc)[:96])
            raise
        self.policy.check_deadline()
        action = result['action']
        if action['kind'] == 'click' and sum(k in action for k in ('element_id', 'mark_id')) != 1:
            raise ActionValidationError('GOAL_GROUNDING')
        if action['kind'] == 'type' and not action['text'].isascii():
            self.metrics.validation_errors.append('GOAL_ASCII')
            raise ActionValidationError('GOAL_ASCII')
        return result


# @anchor:wa.learn.web_goal.CountingMuseBackend
class CountingMuseBackend(MuseBackend):
    # @anchor:wa.learn.web_goal.CountingMuseBackend.__init__
    def __init__(self, *, metrics, **kwargs):
        super().__init__(effort='auto', **kwargs)
        self.metrics = metrics

    # @anchor:wa.learn.web_goal.CountingMuseBackend.generate
    def generate(self, request):
        self.metrics.provider_attempts += 1
        with self.metrics.measure('model'):
            return super().generate(request)


# @anchor:wa.learn.web_goal.GoalTrace
class GoalTrace:
    """Parse complete records; emit only closed codes and structural numbers."""
    # @anchor:wa.learn.web_goal.GoalTrace.__init__
    def __init__(self, destination):
        self.destination = destination
        self.pending = ''

    # @anchor:wa.learn.web_goal.GoalTrace.write
    def write(self, text):
        self.pending += text
        if len(self.pending) > 1_000_000:
            self.pending = ''
            raise ValueError('GOAL_TRACE_LIMIT')
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            raw = json.loads(line)
            projected = {'event': 'goal_step'}
            for key in ('step', 'steps', 'latency_ms', 'corrections', 'budget_used'):
                value = raw.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    projected[key] = value
            self.destination.write(json.dumps(projected, allow_nan=False) + '\n')
        return len(text)

    # @anchor:wa.learn.web_goal.GoalTrace.flush
    def flush(self):
        self.destination.flush()


# @anchor:wa.learn.web_goal.run_goal
def run_goal(surface, *, goal, trace, max_steps, max_ms, ve_root=None,
             clock=time.monotonic, sleep=time.sleep, save_frame=None, vision_only=False):
    start = clock()
    if type(max_steps) is not int or max_steps <= 0 or type(max_ms) is not int or max_ms <= 0:
        raise ValueError('GOAL_BUDGET')
    deadline = start + max_ms/1000
    policy = GoalPolicy(goal, deadline, clock)
    metrics = GoalMetrics(clock)
    owner = LegacyDispatchOwner(surface)
    capsule = describer = decider = None
    result = {'goal_verified': False, 'goal_verification_basis': 'model_done',
              'independent_goal_verified': None, 'independent_goal_unavailable_reason': 'no_independent_predicate',
              'stop_reason': 'failure', 'steps': 0, 'final_frame': None,
              'final_frame_unavailable_reason': 'no_valid_frame', 'failure_code': None,
              'input_effect_unknown': False}
    result_directory = None
    try:
        with metrics.measure('setup'):
            surface.install_goal_session(owner=owner, policy=policy, deadline=deadline, clock=clock)
            surface.paste_text = True   # goal mode pastes text: per-character typing is corrupted by rich editors (Stitch) [MEASURED]
            surface.trace = GoalTrace(trace)
            surface.start()
            capsule = GoalCapsule(surface, owner=owner, policy=policy, metrics=metrics,
                deadline=deadline, clock=clock, sleep=sleep, vision_only=vision_only)
            describer = GoalDescriber(ve_root, capsule=capsule, policy=policy, metrics=metrics)
            decider = GoalModelDecider(CountingMuseBackend(metrics=metrics), policy=policy, metrics=metrics)
            policy.check_deadline()
        remaining_ms = int((deadline-clock())*1000)
        if remaining_ms <= 0:
            raise TimeoutError('GOAL_DEADLINE')
        raw = run_loop(capsule, describer, decider, goal=goal, trace=GoalTrace(trace),
            max_steps=max_steps, max_ms=remaining_ms, clock=clock, sleep=sleep,
            pixel_only=vision_only, goal_check=None, journal_path=None, integration=None)   # no AX state -> verify by pixels
        result['steps'] = raw['steps']
        result['stop_reason'] = raw['stop_reason']
        result['input_effect_unknown'] = capsule.input_effect_unknown
        result['failure_code'] = capsule.failure_code or ('goal_loop_failure' if raw['failure'] else None)
        result['validation_errors'] = list(metrics.validation_errors)[:8]
        result['actions'] = list(metrics.actions)[:40]
        if raw['failure']:   # closed structural names only (stage, exception class), never page text
            result['failure_stage'] = str(raw['failure'].get('stage', ''))[:32]
            result['failure_error_class'] = str(raw['failure'].get('error_class', ''))[:48]
            result['failure_attempts'] = [(str(a.get('stage', ''))[:32], str(a.get('error_class', ''))[:48])
                                          for a in (raw.get('attempts') or [])][:8]
        with metrics.measure('finalization'):
            policy.check_deadline()
            if raw.get('final_frame'):
                frame = _validate_frame(raw['final_frame'])
                result_directory = Path(tempfile.mkdtemp(prefix='wgoal-result-', dir='/dev/shm'))
                path = result_directory / 'final.png'
                shutil.copyfile(frame['path'], path)
                _validate_frame(dict(frame, path=str(path)))
                if save_frame is not None:
                    shutil.copyfile(path, save_frame)
                policy.check_deadline()
                result['final_frame'] = str(path)
                result['final_frame_unavailable_reason'] = None
            result['goal_verified'] = (raw['stop_reason'] == 'done' and raw['failure'] is None
                and not capsule.input_effect_unknown and owner.check_ready() and result['final_frame'] is not None)
    except Exception as exc:
        result['goal_verified'] = False
        result['failure_code'] = 'goal_deadline' if isinstance(exc, TimeoutError) else _failure_code(exc)
        if isinstance(exc, TimeoutError):
            result['stop_reason'] = 'max_ms'
        if result_directory is not None:
            shutil.rmtree(result_directory)
            result['final_frame'] = None
            result['final_frame_unavailable_reason'] = 'finalization_failed'
    finally:
        try:
            if decider is not None:
                decider.close()
            if describer is not None and describer._marks_dir is not None:
                describer._marks_dir.cleanup()
            surface.stop()
        except Exception:
            result['goal_verified'] = False
            result['failure_code'] = 'goal_cleanup'
        owner.close()
    if clock() >= deadline:
        result['goal_verified'] = False
        result['stop_reason'] = 'max_ms'
    metrics.stages['total'] = {'total': max(0, round((clock()-start)*1_000_000)), 'count': 1}
    result.update(model_calls=metrics.provider_attempts, acquisition_attempts=metrics.acquisition_attempts,
                  read_retries=metrics.read_retries, dispatched_inputs=metrics.dispatched_inputs,
                  latency_us=metrics.stages,
                  latency_missingness=[k for k, v in metrics.stages.items() if not v['count']])
    return result
