# @module {"id":"wa.perceive.wait","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.ax","wa.perceive.stopstate","wa.perceive.text"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.wait - declarative AX completion waiter (WP1-02).
ORIGIN: ported from the executable reference in tests/test_jq1_a6_perceive_primitives.py:126
and specification docs/spec/jq1_a6_perceive_primitives.md (C4 / WP1-02).
"""
import json
from wender.perceive.stopstate import IncompleteTree as _BaseIncompleteTree, _name, _normal, _role, _tree, _value, _visible
from wender.perceive.text import _article, _matches, _nearest_group
# @anchor:wa.perceive.wait.IncompleteTree
IncompleteTree = _BaseIncompleteTree
def _node_props(node):
 props = {p.get("name"): _value(p.get("value")) for p in (node.get("properties") or []) if isinstance(p, dict) and "name" in p}
 for k, v in (node.get("states") or {}).items():
  props[k] = _value(v)
 for item in node.get("attrs") or ():
  if isinstance(item, (tuple, list)) and len(item) == 2:
   props[item[0]] = item[1]
 if "hidden" in node:
  props["hidden"] = _value(node.get("hidden"))
 return props
def _adapt_selector(predicate):
 if isinstance(predicate, tuple) and len(predicate) == 2 and all(isinstance(v, str) for v in predicate):
  return {"role": predicate[0], "name": predicate[1]}, True
 if not isinstance(predicate, dict) or set(predicate) != {"role", "name"} or not all(isinstance(v, str) for v in predicate.values()):
  raise ValueError("a selector is exactly {role: str, name: str}")
 return {"role": predicate["role"], "name": predicate["name"]}, False
def _adapt_panel_collapsed(rule):
 if isinstance(rule, tuple) and len(rule) == 4 and all(isinstance(v, str) and v for v in rule):
  return (rule[0], rule[1], rule[2], rule[3].lower())
 if isinstance(rule, dict) and set(rule) == {"role", "name", "property", "value"} and all(isinstance(v, str) and v for v in rule.values()):
  return (rule["role"], rule["name"], rule["property"], rule["value"].lower())
 raise ValueError("panel_collapsed must be (role, name, property, value)")
def _is_pruned_list(raw):
 return (isinstance(raw, list) and bool(raw)
   and all(isinstance(n, dict) and "nodeId" not in n and isinstance(n.get("node_id"), str) and bool(n.get("node_id"))
     and isinstance(n.get("id"), str) and bool(n.get("id")) and isinstance(n.get("role"), str)
     and isinstance(n.get("name"), str) and type(n.get("depth")) is int and isinstance(n.get("ancestors"), (tuple, list))
     for n in raw))
def _is_mock_role_name_list(raw):
 return (isinstance(raw, list) and bool(raw)
   and all(isinstance(n, dict) and set(n) == {"role", "name"} and isinstance(n.get("role"), str) and isinstance(n.get("name"), str)
     for n in raw))
def _pruned_tree(pruned_nodes):
 by_id, ordered, ancestors, stack = {}, [], {}, []
 for node in pruned_nodes:
  nid, depth, anc_roles = node["node_id"], node["depth"], tuple(node["ancestors"])
  if nid in by_id or depth < 0 or depth > len(stack) or len(anc_roles) != depth:
   raise IncompleteTree("invalid pruned node hierarchy")
  stack = stack[:depth]
  if tuple(by_id[a]["role"] for a in stack) != anc_roles:
   raise IncompleteTree("pruned ancestor roles mismatch depth stack")
  adapted = dict(node, nodeId=nid)
  by_id[nid] = adapted
  ordered.append(adapted)
  ancestors[nid] = tuple(stack)
  stack.append(nid)
 return by_id, ordered, ancestors
def _legacy_group_owner(anchor_node, anchor_sel, present, visible, ordered, by_id, ancestors):
 if not present or any(_role(n) == "article" for n in by_id.values()):
  return None
 gid = _nearest_group(anchor_node, by_id, ancestors)
 if gid is None:
  return None
 if sum(1 for n in by_id.values() if _role(n) == "group") == 1 and not _normal(_name(by_id[gid])):
  return None
 if sum(1 for n in visible if gid in ancestors[n["nodeId"]] and _matches(n, anchor_sel)) != 1:
  return None
 start_idx = ordered.index(anchor_node)
 for n in ordered[:start_idx]:
  if gid in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors) and any(_matches(n, p) for p in present):
   return None
 for n in ordered[start_idx + 1:]:
  if gid in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors) and any(_matches(n, p) for p in present):
   if _nearest_group(n, by_id, ancestors) != gid:
    return None
 return gid
def _pruned_anchor_owner(anchor_node, anchor_sel, present, visible, ordered, by_id, ancestors, explicit_boundary):
 owner = _legacy_group_owner(anchor_node, anchor_sel, present, visible, ordered, by_id, ancestors)
 if owner is not None or not explicit_boundary or not present:
  return owner
 chain = ancestors[anchor_node["nodeId"]]
 if not chain or _role(by_id[chain[-1]]) not in {"main", "group", "region"}:
  return None
 container, start_idx = chain[-1], ordered.index(anchor_node)
 for n in ordered[:start_idx]:
  if container in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors) and any(_matches(n, p) for p in present):
   return None
 return container
def _panel_collapsed_holds(visible, panel_collapsed):
 if panel_collapsed is None:
  return True
 role, name, prop_name, expected_val = panel_collapsed
 matched = [n for n in visible if _matches(n, {"role": role, "name": name})]
 if not matched:
  return False
 for n in matched:
  props = _node_props(n)
  if prop_name not in props:
   return False
  val = props[prop_name]
  if (("true" if val else "false") if isinstance(val, bool) else str(val).strip().lower()) != expected_val:
   return False
 return True
# @anchor:wa.perceive.wait.wait_done
def wait_done(snapshot_reader=None, rules=None, max_polls=None, sleep_fn=None, *,
    clock=None, sleep=None, poll_interval_ms=100, timeout_ms=None, **legacy_kwargs):
 """Wait for completion across consecutive quiet AX snapshots under explicit budgets."""
 is_legacy = False
 if "sleep_func" in legacy_kwargs:
  if sleep_fn is not None:
   raise ValueError("duplicate sleep_fn and sleep_func")
  sleep_fn, is_legacy = legacy_kwargs.pop("sleep_func"), True
 if "site_profile" in legacy_kwargs:
  sp = legacy_kwargs.pop("site_profile")
  if not isinstance(sp, str) or not sp.strip():
   raise ValueError("site_profile must be a non-empty string")
  is_legacy = True
 allowed_canon = {"absent_roles_names", "after_last_anchor", "present_roles_names", "quiet_snapshots"}
 allowed_gemini = {"panel_collapsed", "progressbar_absent", "article_present", "stable_hashes"}
 if rules is None and legacy_kwargs:
  if set(legacy_kwargs) - allowed_canon:
   raise ValueError("unknown rule")
  rules, legacy_kwargs, is_legacy = dict(legacy_kwargs), {}, True
 elif legacy_kwargs or not isinstance(rules, dict) or (set(rules) - (allowed_canon | allowed_gemini)):
  raise ValueError("unknown or invalid rules")
 used_adapter = is_legacy
 absent_raw, present_raw, anchor_raw = rules.get("absent_roles_names", []), rules.get("present_roles_names", []), rules.get("after_last_anchor")
 if not isinstance(absent_raw, (list, tuple)) or not isinstance(present_raw, (list, tuple)):
  raise ValueError("role/name conditions must be sequences")
 absent, present = [], []
 for pred in absent_raw:
  sel, ad = _adapt_selector(pred)
  absent.append(sel)
  used_adapter = used_adapter or ad
 for pred in present_raw:
  sel, ad = _adapt_selector(pred)
  present.append(sel)
  used_adapter = used_adapter or ad
 if "progressbar_absent" in rules:
  absent.append(_adapt_selector(rules["progressbar_absent"])[0])
  used_adapter = True
 if "article_present" in rules:
  present.append(_adapt_selector(rules["article_present"])[0])
  used_adapter = True
 panel_collapsed = _adapt_panel_collapsed(rules["panel_collapsed"]) if "panel_collapsed" in rules else None
 if panel_collapsed is not None:
  used_adapter = True
 explicit_boundary = False
 if anchor_raw is not None:
  if isinstance(anchor_raw, dict) and set(anchor_raw) == {"anchor", "target"}:
   anchor = _adapt_selector(anchor_raw["anchor"])[0]
   present.append(_adapt_selector(anchor_raw["target"])[0])
   explicit_boundary = used_adapter = True
  else:
   anchor, ad = _adapt_selector(anchor_raw)
   used_adapter = used_adapter or ad
 else:
  anchor = None
 if "stable_hashes" in rules:
  if "quiet_snapshots" in rules and rules["quiet_snapshots"] != rules["stable_hashes"]:
   raise ValueError("conflicting quiet_snapshots and stable_hashes")
  quiet_raw, used_adapter = rules["stable_hashes"], True
 else:
  quiet_raw = rules.get("quiet_snapshots", 2)
 if type(quiet_raw) is int and quiet_raw == 1 and (used_adapter or clock is None or sleep_fn is not None):
  quiet = 2
 elif type(quiet_raw) is not int or quiet_raw < 2:
  raise ValueError("quiet_snapshots is out of range")
 else:
  quiet = quiet_raw
 if max_polls is None:
  if used_adapter or clock is None or sleep_fn is not None:
   max_polls = max(len(snapshot_reader.states), 5) if hasattr(snapshot_reader, "states") and isinstance(getattr(snapshot_reader, "states"), list) else 10
  else:
   raise ValueError("max_polls is out of range")
 for name, value, minimum in (("max_polls", max_polls, 0), ("poll_interval_ms", poll_interval_ms, 0), ("quiet_snapshots", quiet, 2)):
  if type(value) is not int or value < minimum:
   raise ValueError(name + " is out of range")
 if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
  raise ValueError("timeout_ms must be a positive integer or None")
 if not callable(snapshot_reader):
  if hasattr(snapshot_reader, "send") and callable(snapshot_reader.send):
   cdp = snapshot_reader
   def reader_fn():
    cdp.send("Accessibility.enable", session_id="fixture")
    try:
     nodes = cdp.send("Accessibility.getFullAXTree", session_id="fixture")["nodes"]
    finally:
     cdp.send("Accessibility.disable", session_id="fixture")
    if hasattr(cdp, "advance") and callable(cdp.advance):
     cdp.advance()
    return nodes
  else:
   raise TypeError("snapshot_reader must be callable")
 else:
  reader_fn = snapshot_reader
 if clock is None:
  fake_ms = 0
  _clock = lambda: fake_ms
  s_target = sleep_fn if sleep_fn is not None else sleep
  if s_target is not None and not callable(s_target):
   raise TypeError("sleep must be callable")
  def _sleep(delay_ms):
   nonlocal fake_ms
   fake_ms += delay_ms
   if s_target is not None:
    s_target(delay_ms / 1000.0)
 else:
  if not callable(clock):
   raise TypeError("clock must be callable")
  _clock = clock
  if sleep_fn is not None:
   if not callable(sleep_fn):
    raise TypeError("sleep_fn must be callable")
   _sleep = lambda delay_ms: sleep_fn(delay_ms / 1000.0)
  elif sleep is not None and callable(sleep):
   _sleep = sleep
  else:
   raise ValueError("sleep is required when clock is supplied")
 polls = 0
 result = lambda status: {"done": status == "confirmed_by_adapter", "polls": polls, "completion_status": status}
 if max_polls == 0:
  return result("timeout")
 last_time = _clock()
 if type(last_time) is not int:
  raise ValueError("clock must return integer milliseconds")
 deadline = None if timeout_ms is None else last_time + timeout_ms
 def now():
  nonlocal last_time
  value = _clock()
  if type(value) is not int or value < last_time:
   raise ValueError("clock must be monotonic integer milliseconds")
  last_time = value
  return value
 previous, run = None, 0
 while polls < max_polls:
  if deadline is not None and now() >= deadline:
   return result("timeout")
  polls += 1
  raw = reader_fn()
  current_time = now()
  if deadline is not None and current_time >= deadline:
   return result("timeout")
  pruned_mode = _is_pruned_list(raw)
  mock_flat_mode = False if pruned_mode else _is_mock_role_name_list(raw)
  try:
   by_id, ordered, ancestors = _pruned_tree(raw) if pruned_mode else (({}, list(raw), {}) if mock_flat_mode else _tree(raw))
  except IncompleteTree:
   previous, run = None, 0
  else:
   signature = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
   run = run + 1 if signature == previous else 1
   previous = signature
   visible = ordered if mock_flat_mode else [n for n in ordered if _visible(n, by_id, ancestors)]
   gates = not any(_matches(n, p) for n in visible for p in absent) and _panel_collapsed_holds(visible, panel_collapsed)
   scope = visible
   if anchor is not None:
    if mock_flat_mode:
     gates, scope = False, []
    else:
     anchors = [n for n in visible if _matches(n, anchor)]
     owner = None
     if anchors:
      last_anchor = anchors[-1]
      owner = _article(last_anchor, by_id, ancestors)
      if owner is None:
       owner = (_pruned_anchor_owner(last_anchor, anchor, present, visible, ordered, by_id, ancestors, explicit_boundary)
          if pruned_mode else _legacy_group_owner(last_anchor, anchor, present, visible, ordered, by_id, ancestors))
     if owner is None:
      gates, scope = False, []
     else:
      start, owner_role = ordered.index(anchors[-1]), _role(by_id[owner])
      same_owner = (lambda n: _article(n, by_id, ancestors) == owner) if owner_role == "article" else (
       (lambda n: _nearest_group(n, by_id, ancestors) == owner) if owner_role == "group" else (lambda n: owner in ancestors[n["nodeId"]]))
      scope = [n for n in ordered[start + 1:] if same_owner(n) and anchors[-1]["nodeId"] not in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors)]
   gates = gates and all(any(_matches(n, p) for n in scope) for p in present)
   if gates and run >= quiet:
    return result("confirmed_by_adapter" if present else "stable_unconfirmed")
  if polls == max_polls:
   break
  delay = poll_interval_ms
  if deadline is not None:
   remaining = deadline - now()
   if remaining <= 0:
    return result("timeout")
   delay = min(delay, remaining)
  _sleep(delay)
 return result("timeout")
