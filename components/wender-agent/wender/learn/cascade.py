# @module {"id":"wa.learn.cascade","layer":2,"owner":"LEARN","deps":["wa.learn.calibrate","wa.learn.script_policy","wa.perceive.ax"],"book":"HB-WA-s4-learn"}
"""Transport-agnostic cascade; structural inputs only, no image or disk IO.
Origin: cu_loop.py structural control bindings and fastexec/bench/run_model.py.
"""
from dataclasses import dataclass, asdict, field
from enum import StrEnum
from types import MappingProxyType
import re
from wender.learn import calibrate
from .script_policy import validate_calibration_artifact as _validate_artifact
from wender.perceive.ax import M_ACT, BIT_DISABLED

# @anchor:wa.learn.cascade.EscalationReason
class EscalationReason(StrEnum):
    UNKNOWN_SCREEN = 'unknown_screen'
    POSTCHECK_FAILED_TWICE = 'postcheck_failed_twice'
    ABSTAIN = 'abstain'
    DISTRIBUTION_UNKNOWN = 'distribution_unknown'
    UNCALIBRATED_MODEL = 'uncalibrated_model'
    LOW_CONFIDENCE = 'low_confidence'
    BELOW_DESTRUCTIVE_TAU = 'below_destructive_tau'
    IRREVERSIBLE_GATE_DENIED = 'irreversible_gate_denied'
    LOW_MARGIN = 'low_margin'
    NON_SINGLETON_SET = 'non_singleton_set'
    OVER_OPTION_CAP = 'over_option_cap'
    BLOCKED_CONTROL = 'blocked_control'
    STALE_OBSERVATION = 'stale_observation'
    TIER_TIMEOUT = 'tier_timeout'
    TIER_TRANSPORT_ERROR = 'tier_transport_error'
    INVALID_TIER2_OPTION_MODE = 'invalid_tier2_option_mode'
    MUSE_BUDGET_EXHAUSTED = 'muse_budget_exhausted'
    IMAGEWORTHY_ROUTE = 'imageworthy_route'

# @anchor:wa.learn.cascade.MODEL_CALIBRATION_REGISTRY
_SEB = MappingProxyType(dict(status='CALIBRATED', t_star_num=1000, t_star_den=1000,
                            q_hat_promille=200, tau_default_promille=800, tau_destructive_promille=950,
                            margin_promille=150, ece_promille=28))  # [CITED] W8 frozen artifact
MODEL_CALIBRATION_REGISTRY = MappingProxyType({
    'ironbcc/seb-9b': _SEB, 'seb-9b': _SEB,
    **{name: MappingProxyType(dict(status='UNCALIBRATED')) for name in
       ('Qwen3-VL-2B', 'Qwen3.5-2B', 'convaiinnovations/laya', 'Laya-30B-A3B')}})

# @anchor:wa.learn.cascade.TierReceipt
@dataclass(frozen=True, slots=True)
class TierReceipt:
    model: str
    mapping: dict = field(default_factory=dict)
    logprobs_content: tuple | list | None = None
    generated_key: str = ''
    serving_gates: dict | None = None
    self_reported_confidence: object = None
    latency_us: int = 0
    millitokens: int = 0

    # @anchor:wa.learn.cascade.TierReceipt.metadata
    def metadata(self):
        receipt = calibrate.verify_distribution(self.mapping, self.logprobs_content, self.generated_key,
                                                serving_gates=self.serving_gates, self_reported_confidence=self.self_reported_confidence)
        return dict(model=self.model, latency_us=self.latency_us, millitokens=self.millitokens,
                    distribution=receipt.metadata())

# @anchor:wa.learn.cascade.CascadeDecision
@dataclass(frozen=True, slots=True)
class CascadeDecision:
    tier: int
    target_id: str | None = None
    action: str = 'abstain'
    reason: EscalationReason | None = None
    receipts: tuple = ()
    expected_latency_us: int = 0
    expected_millitokens: int = 0
    source: str | None = None

    # @anchor:wa.learn.cascade.CascadeDecision.metadata
    def metadata(self):
        result = asdict(self)
        result['reason'] = self.reason.value if self.reason else None
        result['receipts'] = list(result['receipts'])
        return result

# @anchor:wa.learn.cascade.intent_to_action
def intent_to_action(intent, workflow_step, control_map, element_table, *, fail_count=0, owner=None):
    if (not isinstance(intent, str) or workflow_step.get('intent') != intent or fail_count != 0
            or element_table.get('known_screen') is not True
            or type(element_table.get('blocked_control_present')) is not int
            or element_table['blocked_control_present'] != 0):
        return None
    control = control_map.get(workflow_step.get('action', {}).get('control'))
    if not control or not any(k in control for k in ('role', 'region', 'pattern', 'canon_name')):
        return None
    matches = []
    for node in element_table.get('elements', ()):
        if any(node.get(k) != control[k] for k in ('role', 'region', 'canon_name') if k in control):
            continue
        if 'pattern' in control:
            try:
                if not re.search(control['pattern'], node.get('canon_name', node.get('name', ''))):
                    continue
            except (re.error, TypeError):
                return None
        rule = control.get('container_rule')
        if rule and rule not in ('first', 'last') and node.get('container_rule') != rule:
            continue
        matches.append(node)
    if control.get('container_rule') in ('first', 'last'):
        # Ordering is not ownership. Only an independently registered complete
        # inventory may establish the current container/turn target.
        if owner is None:
            return None
        try:
            target = owner.select_control(control, element_table)
        except Exception:
            return None
        matches = [node for node in matches if node is target]
    if len(matches) != 1:
        return None
    node = matches[0]
    if 'current_epoch_gen16' in element_table and node.get('gen16') != element_table['current_epoch_gen16']:
        return None
    word = node.get('state_word')
    if (type(word) is not int or word & M_ACT != M_ACT or word & BIT_DISABLED
            or node.get('match_level') not in ('L0_EXACT', 'L1_STABLE') or not node.get('id')):
        return None
    return dict(tier=0, target_id=node['id'], match_level=node['match_level'], tokens=0)

# @anchor:wa.learn.cascade.is_image_worthy
def is_image_worthy(element_table):
    return any(element_table.get(k) is True for k in ('duplicate_names_unresolved', 'bare_digit_option',
               'container_lexical_overlap', 'deictic_goal', 'unnamed_icon_or_unmatched_mark', 'semantic_pixel_conflict'))

# @anchor:wa.learn.cascade.validate_tier2_laya_probe
def validate_tier2_laya_probe(option_mode, options=(), *, act_probability=None):
    values = options.values() if isinstance(options, dict) else options
    if (option_mode not in ('labelled', 'nodes') or act_probability is not None
            or any(isinstance(v, str) and re.match(r'^mark \d+:? bbox', v) for v in values)):
        raise ValueError('Laya requires --options labelled or --options nodes')
    return True

# @anchor:wa.learn.cascade.CascadePolicy
class CascadePolicy:
    # @anchor:wa.learn.cascade.CascadePolicy.__init__
    def __init__(self, *, tier2=None, muse=None, max_muse_calls_per_cycle=2,
                 c1_us=0, c2_us=0, c3_us=0, p_esc2_promille=0, p_esc3_promille=0,
                 c1_millitokens=0, c2_millitokens=0, c3_millitokens=0,
                 integration=None, automatic=False, calibration_context=None,
                 control_owner=None, call_budget=None):
        values = (max_muse_calls_per_cycle, c1_us, c2_us, c3_us, p_esc2_promille,
                  p_esc3_promille, c1_millitokens, c2_millitokens, c3_millitokens)
        if any(type(v) is not int or v < 0 for v in values) or max(p_esc2_promille, p_esc3_promille) > 1000:
            raise ValueError('invalid cascade budget')
        self.tier2, self.muse = tier2, muse
        self.integration, self.automatic = integration, automatic
        self.calibration_context = calibration_context
        self.control_owner, self.call_budget = control_owner, call_budget
        self.max_muse_calls_per_cycle = max_muse_calls_per_cycle
        self.muse_calls_used = 0
        self.expected_latency_us = c1_us + p_esc2_promille*c2_us//1000 + p_esc3_promille*c3_us//1000
        self.expected_millitokens = c1_millitokens + p_esc2_promille*c2_millitokens//1000 + p_esc3_promille*c3_millitokens//1000

    # @anchor:wa.learn.cascade.CascadePolicy.reset_cycle
    def reset_cycle(self):
        self.muse_calls_used = 0

    def _gate(self, receipt, step, table):
        if receipt is None:
            return None, EscalationReason.DISTRIBUTION_UNKNOWN
        if self.automatic and (not self._inference_ready()
                or self.calibration_context['artifact'].model != receipt.model):
            return None, EscalationReason.UNCALIBRATED_MODEL
        distribution = calibrate.verify_distribution(receipt.mapping, receipt.logprobs_content, receipt.generated_key,
                       serving_gates=receipt.serving_gates, self_reported_confidence=receipt.self_reported_confidence)
        if distribution.verdict != calibrate.CALIBRATED:
            return None, EscalationReason.DISTRIBUTION_UNKNOWN
        if self.automatic:
            registered = self.calibration_context['artifact']
            gates = receipt.serving_gates
            if (not isinstance(gates, dict) or gates.get('model_sha') != registered.model
                    or gates.get('tokenizer_sha') != registered.tokenizer
                    or {k: v for k, v in gates.items() if k not in ('model_sha', 'tokenizer_sha')}
                       != dict(registered.serving)):
                return None, EscalationReason.UNCALIBRATED_MODEL
            artifact = dict(registered.parameters)
        else:
            artifact = MODEL_CALIBRATION_REGISTRY.get(receipt.model, {})
            if artifact.get('status') != 'CALIBRATED':
                return None, EscalationReason.UNCALIBRATED_MODEL
        target = next((n for n in table.get('elements', ()) if n.get('id') == distribution.target_id), {})
        if distribution.target_id is not None and not target:
            return None, EscalationReason.STALE_OBSERVATION
        if target:
            word = target.get('state_word')
            if type(word) is not int or word & M_ACT != M_ACT or word & BIT_DISABLED:
                return None, EscalationReason.BLOCKED_CONTROL
            if 'current_epoch_gen16' in table and target.get('gen16') != table['current_epoch_gen16']:
                return None, EscalationReason.STALE_OBSERVATION
        result = calibrate.calibrate_decision(distribution, action_kind=step.get('action', {}).get('kind', ''),
                 destructive_or_irreversible=step.get('irreversible') is True, match_level=target.get('match_level'),
                 automatic=self.automatic, artifact_context=self.calibration_context,
                 **(dict(model_sha=receipt.model, tokenizer_sha=registered.tokenizer,
                         serving_flags=dict(registered.serving), split_sha=registered.split) if self.automatic else {}),
                 **{k: artifact[k] for k in ('t_star_num', 't_star_den', 'q_hat_promille')})
        reasons = {'BELOW_TAU': EscalationReason.LOW_CONFIDENCE,
                   'BELOW_DESTRUCTIVE_TAU': EscalationReason.BELOW_DESTRUCTIVE_TAU,
                   'IRREVERSIBLE_LEVEL_DENIED': EscalationReason.IRREVERSIBLE_GATE_DENIED,
                   'LOW_MARGIN': EscalationReason.LOW_MARGIN,
                   'NON_SINGLETON_CONFORMAL_SET': EscalationReason.NON_SINGLETON_SET,
                   'MODEL_UNCALIBRATED': EscalationReason.UNCALIBRATED_MODEL}
        if result.failure_code:
            return None, reasons.get(result.failure_code, EscalationReason.DISTRIBUTION_UNKNOWN)
        return distribution.target_id, None if distribution.target_id is not None else EscalationReason.ABSTAIN

    def _inference_ready(self):
        try:
            return type(self.calibration_context) is dict and _validate_artifact(**self.calibration_context)
        except Exception:
            return False

    # @anchor:wa.learn.cascade.CascadePolicy.decide
    def decide(self, intent, workflow_step, control_map, element_table, *, tier1=None, options=(),
               fail_count=0, option_mode='labelled', history=(), lessons=(), receipts=(), trace=None):
        trace = trace if trace is not None else []
        events = []
        def decision(tier, target=None, reason=None, source=None):
            return CascadeDecision(tier, target, 'dispatch' if target is not None else 'abstain', reason,
                                   tuple(events), self.expected_latency_us, self.expected_millitokens,
                                   source or ('structural', 'text', 'visual', 'muse')[tier])
        if (type(element_table.get('blocked_control_present')) is not int
                or element_table['blocked_control_present'] != 0):
            return decision(0, reason=EscalationReason.BLOCKED_CONTROL)
        if self.automatic:
            if self.integration is None or self.integration.check_selection(element_table) is not True:
                return decision(0, reason=EscalationReason.STALE_OBSERVATION)
            selection = self.integration.select_script(element_table)
            if selection is not None:
                return decision(0, selection, source='script')
        if element_table.get('known_screen') is not True:
            reason = EscalationReason.UNKNOWN_SCREEN
        elif fail_count >= 2:
            reason = EscalationReason.POSTCHECK_FAILED_TWICE
        else:
            structural = intent_to_action(intent, workflow_step, control_map, element_table,
                                          fail_count=fail_count, owner=self.control_owner)
            if structural:
                return decision(0, structural['target_id'])
            if len(options)+1 > 20 or (tier1 and len(tier1.mapping) > 20):
                reason = EscalationReason.OVER_OPTION_CAP
            else:
                target, reason = self._gate(tier1, workflow_step, element_table)
                if reason is None:
                    return decision(1, target)
        if is_image_worthy(element_table):
            try:
                validate_tier2_laya_probe(option_mode, options)
            except ValueError:
                return decision(2, reason=EscalationReason.INVALID_TIER2_OPTION_MODE)
            if self.tier2 is not None:
                events.append(dict(event='escalation', reason=reason.value, intent=intent, tier=2))
                try:
                    if self.automatic and not self._inference_ready():
                        return decision(2, reason=EscalationReason.UNCALIBRATED_MODEL)
                    if self.automatic and (self.call_budget is None or self.call_budget.charge('VISUAL') is not True):
                        return decision(2, reason=EscalationReason.MUSE_BUDGET_EXHAUSTED)
                    probe = self.tier2(intent, options, option_mode=option_mode)
                    target, tier2_reason = self._gate(probe, workflow_step, element_table)
                    if tier2_reason is None:
                        return decision(2, target)
                    reason = tier2_reason
                except TimeoutError:
                    reason = EscalationReason.TIER_TIMEOUT
                except (OSError, RuntimeError):
                    reason = EscalationReason.TIER_TRANSPORT_ERROR
        if self.muse is None:
            return decision(3, reason=reason)
        if self.muse_calls_used >= self.max_muse_calls_per_cycle:
            return decision(3, reason=EscalationReason.MUSE_BUDGET_EXHAUSTED)
        event = dict(event='escalation', reason=reason.value, intent=intent, tier=3)
        trace.append(event)
        events.append(event)
        self.muse_calls_used += 1
        try:
            if self.automatic and not self._inference_ready():
                return decision(3, reason=EscalationReason.UNCALIBRATED_MODEL)
            if self.automatic and (self.call_budget is None or self.call_budget.charge('MUSE') is not True):
                return decision(3, reason=EscalationReason.MUSE_BUDGET_EXHAUSTED)
            target = self.muse.choose(intent, options, history, lessons=lessons, receipts=receipts)
        except TimeoutError:
            return decision(3, reason=EscalationReason.TIER_TIMEOUT)
        except (OSError, RuntimeError):
            return decision(3, reason=EscalationReason.TIER_TRANSPORT_ERROR)
        if target == 'abstain':
            return decision(3, reason=EscalationReason.ABSTAIN)
        # Muse is an option selector, never authority to invent a target or bypass vetoes.
        if target not in {o['id'] for o in options}:
            return decision(3, reason=EscalationReason.ABSTAIN)
        node = next((n for n in element_table.get('elements', ()) if n.get('id') == target), {})
        if 'current_epoch_gen16' in element_table and node.get('gen16') != element_table['current_epoch_gen16']:
            return decision(3, reason=EscalationReason.STALE_OBSERVATION)
        word = node.get('state_word')
        if type(word) is not int or word & M_ACT != M_ACT or word & BIT_DISABLED:
            return decision(3, reason=EscalationReason.BLOCKED_CONTROL)
        if workflow_step.get('irreversible') is True or workflow_step.get('action', {}).get('kind') in ('delete', 'submit_order', 'transfer', 'destructive'):
            if node.get('match_level') not in ('L0_EXACT', 'L1_STABLE'):
                return decision(3, reason=EscalationReason.IRREVERSIBLE_GATE_DENIED)
        return decision(3, target)
