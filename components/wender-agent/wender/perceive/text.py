# @module {"id":"wa.perceive.text","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.ax","wa.perceive.stopstate"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.text - bounded AX hierarchy answer text extractor (WP1-04).
ORIGIN: ported from the executable reference in tests/test_jq1_a6_perceive_primitives.py:226
and specification docs/spec/jq1_a6_perceive_primitives.md (C4 / WP1-04).
"""
from wender.perceive.stopstate import IncompleteTree as _BaseIncompleteTree, _hidden, _name, _normal, _role, _tree, _visible
# @anchor:wa.perceive.text.IncompleteTree
IncompleteTree = _BaseIncompleteTree
def _matches(node, predicate):
 return _role(node) == predicate["role"] and _normal(_name(node)) == _normal(predicate["name"])
def _selector(predicate):
 if isinstance(predicate, tuple) and len(predicate) == 2 and all(isinstance(v, str) for v in predicate):
  return {"role": predicate[0], "name": predicate[1]}
 if not isinstance(predicate, dict) or set(predicate) != {"role", "name"} or not all(isinstance(v, str) for v in predicate.values()):
  raise ValueError("a selector is exactly {role: str, name: str}")
 return {"role": predicate["role"], "name": predicate["name"]}
def _article(node, by_id, ancestors):
 for nid in reversed(ancestors[node["nodeId"]]):
  if _role(by_id[nid]) == "article":
   return nid
 return None
def _nearest_group(node, by_id, ancestors):
 for nid in reversed(ancestors[node["nodeId"]]):
  if _role(by_id[nid]) == "group":
   return nid
 return None
def _legacy_group_owner(start_node, region_start, region_end, ordered, by_id, ancestors):
 if any(_role(n) == "article" for n in by_id.values()):
  return None
 gid = _nearest_group(start_node, by_id, ancestors)
 if gid is None:
  return None
 group_nodes = [n for n in by_id.values() if _role(n) == "group"]
 if len(group_nodes) == 1 and not _normal(_name(by_id[gid])):
  return None
 vis = [n for n in ordered if gid in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors)]
 if sum(1 for n in vis if _matches(n, region_start)) != 1:
  return None
 start_idx = ordered.index(start_node)
 for n in ordered[:start_idx]:
  if gid in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors) and _matches(n, region_end):
   return None
 for n in ordered[start_idx + 1:]:
  if gid in ancestors[n["nodeId"]] and _visible(n, by_id, ancestors):
   if _matches(n, region_end) and _nearest_group(n, by_id, ancestors) != gid:
    return None
 return gid
# @anchor:wa.perceive.text.extract
def extract(raw_nodes, region_start=None, region_end=None, excluded_roles_names=(),
   trailers=(), *, target_role=None, target_name=None, target=None):
 """Extract clean structured answer text from a raw AX node list."""
 if not isinstance(excluded_roles_names, (list, tuple)):
  raise ValueError("excluded_roles_names must be a sequence")
 excluded_sels = [_selector(pred) for pred in excluded_roles_names]
 if not isinstance(trailers, (list, tuple)) or any(not isinstance(t, str) or not _normal(t) for t in trailers):
  raise ValueError("trailers must be nonempty literal strings")
 target_sel = None
 if target is not None:
  if target_role is not None or target_name is not None:
   raise ValueError("cannot specify both target and target_role/target_name")
  target_sel = _selector(target)
 elif target_role is not None or target_name is not None:
  if not isinstance(target_role, str) or not isinstance(target_name, str):
   raise ValueError("target_role and target_name must be strings")
  target_sel = _selector({"role": target_role, "name": target_name})
 boundary_mode = region_start is not None or region_end is not None
 if boundary_mode and target_sel is not None:
  raise ValueError("cannot combine boundary selectors with target selector")
 if boundary_mode:
  start_sel, end_sel = _selector(region_start), _selector(region_end)
 def result(text, status):
  return {"text": text, "extraction_status": status}
 try:
  by_id, ordered, ancestors = _tree(raw_nodes)
 except IncompleteTree:
  return result("", "incomplete_tree")
 if boundary_mode:
  starts = [n for n in ordered if _visible(n, by_id, ancestors) and _matches(n, start_sel)]
  if not starts:
   return result("", "ambiguous_boundary")
  start = starts[-1]
  owner = _article(start, by_id, ancestors)
  is_group_owner = False
  if owner is None:
   owner = _legacy_group_owner(start, start_sel, end_sel, ordered, by_id, ancestors)
   is_group_owner = owner is not None
  if owner is None:
   return result("", "ambiguous_boundary")
  index = {n["nodeId"]: i for i, n in enumerate(ordered)}
  node_owner_fn = (lambda n: _nearest_group(n, by_id, ancestors)) if is_group_owner else (lambda n: _article(n, by_id, ancestors))
  ends = [n for n in ordered if _visible(n, by_id, ancestors) and _matches(n, end_sel)
    and node_owner_fn(n) == owner and start["nodeId"] not in ancestors[n["nodeId"]]
    and index[n["nodeId"]] > index[start["nodeId"]]]
  if len(ends) != 1:
   return result("", "ambiguous_boundary")
  candidates = ordered[index[start["nodeId"]] + 1:index[ends[0]["nodeId"]]]
  start_id = start["nodeId"]
 elif target_sel is not None:
  targets = [n for n in ordered if _visible(n, by_id, ancestors) and _matches(n, target_sel)]
  if len(targets) != 1:
   return result("", "ambiguous_boundary")
  owner = targets[0]["nodeId"]
  candidates = [n for n in ordered if owner in ancestors[n["nodeId"]]]
  start_id = None
 else:
  if not ordered:
   return result("", "incomplete_tree")
  owner, candidates, start_id = None, ordered, None
 excluded = {n["nodeId"] for n in candidates if any(_matches(n, pred) for pred in excluded_sels)}
 blocks, emitted, last_group = [], set(), None
 block_roles = {"heading", "paragraph", "listitem", "row", "code"}
 def keep(node):
  chain = ancestors[node["nodeId"]]
  if not _visible(node, by_id, ancestors):
   return False
  if owner is not None:
   if boundary_mode:
    if ( _nearest_group(node, by_id, ancestors) if is_group_owner else _article(node, by_id, ancestors)) != owner:
     return False
   elif owner not in chain:
    return False
  if start_id is not None and start_id in chain:
   return False
  return not ({node["nodeId"], *chain} & excluded)
 for node in candidates:
  nid = node["nodeId"]
  if not keep(node):
   continue
  chain = ancestors[nid]
  if any(a in emitted for a in chain):
   continue
  role = _role(node)
  if role == "InlineTextBox":
   if any(_role(by_id[a]) == "StaticText" for a in chain):
    continue
  elif role == "StaticText":
   pass
  elif role in block_roles:
   if any(nid in ancestors[n["nodeId"]] and keep(n) and _role(n) in {"StaticText", "InlineTextBox"} for n in candidates):
    continue
  else:
   continue
  emitted.add(nid)
  group = next((a for a in reversed(chain + (nid,)) if _role(by_id[a]) in block_roles), nid)
  in_code = any(_role(by_id[a]) == "code" for a in chain + (nid,))
  text = _name(node).replace("\r\n", "\n").replace("\r", "\n")
  text = text if in_code else _normal(text)
  if not text.strip():
   continue
  if blocks and group == last_group:
   blocks[-1] += ("" if in_code else " ") + text
  else:
   blocks.append(text)
  last_group = group
 trailer_set = {_normal(t) for t in trailers}
 while blocks and _normal(blocks[-1]) in trailer_set:
  blocks.pop()
 return result("\n".join(blocks), "clean")
