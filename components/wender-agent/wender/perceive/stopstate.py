# @module {"id":"wa.perceive.stopstate","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.ax"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.stopstate - pure wall and challenge detector (WP1-05).
ORIGIN: ported from the executable reference in tests/test_jq1_a6_perceive_primitives.py:333
and specification docs/spec/jq1_a6_perceive_primitives.md (C4 / WP1-05).
"""
import re
from urllib.parse import parse_qs, urlsplit
def _value(v):
 return v.get("value", "") if isinstance(v, dict) else v
def _role(n):
 return str(_value(n.get("role", "")))
def _name(n):
 return str(_value(n.get("name", "")))
def _normal(t):
 return re.sub(r"\s+", " ", t).strip()
def _hidden(node):
 props = {p.get("name"): _value(p.get("value")) for p in (node.get("properties") or []) if isinstance(p, dict)}
 props.update(node.get("states") or {})
 for item in node.get("attrs") or ():
  if isinstance(item, (tuple, list)) and len(item) == 2:
   props[item[0]] = item[1]
 if "hidden" in node:
  props["hidden"] = _value(node.get("hidden"))
 return props.get("hidden") in (True, "true")
# @anchor:wa.perceive.stopstate.IncompleteTree
class IncompleteTree(ValueError):
 """Malformed or incomplete nodeId / childIds graph, never a partial answer."""
def _tree(raw_nodes):
 if not isinstance(raw_nodes, list):
  raise IncompleteTree("raw_nodes must be a list")
 by_id = {}
 for node in raw_nodes:
  if not isinstance(node, dict):
   raise IncompleteTree("node must be an object")
  nid = node.get("nodeId")
  if not isinstance(nid, str) or not nid or nid in by_id:
   raise IncompleteTree("missing or duplicate nodeId")
  children = node.get("childIds", [])
  if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
   raise IncompleteTree("childIds must be a string list")
  by_id[nid] = node
 parent = {}
 for nid, node in by_id.items():
  for child in node.get("childIds", []):
   if child not in by_id:
    raise IncompleteTree("missing child " + child)
   if child in parent and parent[child] != nid:
    raise IncompleteTree("multiple parents for " + child)
   parent[child] = nid
 for nid, node in by_id.items():
  if "parentId" in node and node["parentId"] != parent.get(nid):
   raise IncompleteTree("parentId disagrees with childIds")
 ordered, ancestors, seen, active = [], {}, set(), set()
 def visit(nid, chain):
  if nid in active:
   raise IncompleteTree("cycle")
  if nid in seen:
   return
  active.add(nid)
  seen.add(nid)
  ordered.append(by_id[nid])
  ancestors[nid] = chain
  for child in by_id[nid].get("childIds", []):
   visit(child, chain + (nid,))
  active.remove(nid)
 for nid in by_id:
  if nid not in parent:
   visit(nid, ())
 if len(seen) != len(by_id):
  raise IncompleteTree("rootless cycle")
 return by_id, ordered, ancestors
def _visible(node, by_id, ancestors):
 return (not node.get("ignored", False) and not _hidden(node)
   and not any(_hidden(by_id[nid]) for nid in ancestors[node["nodeId"]]))
def _url_parts(value):
 if not isinstance(value, str):
  return None
 try:
  parsed = urlsplit(value)
  if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
   return None
  host = parsed.hostname.lower().rstrip(".")
  port = ":" + str(parsed.port) if parsed.port is not None else ""
 except ValueError:
  return None
 safe = parsed.scheme.lower() + "://" + host + port + (parsed.path or "/")
 return host, parsed.path, parse_qs(parsed.query), safe
# @anchor:wa.perceive.stopstate.detect
def detect(state_or_raw, *, url="", frame_urls=(), headers=None):
 """Pure wall/challenge detector inspecting supplied evidence only."""
 if isinstance(state_or_raw, list):
  raw, raw_format = state_or_raw, True
 elif isinstance(state_or_raw, dict):
  raw = state_or_raw.get("nodes", [])
  if not isinstance(raw, list) or any(not isinstance(n, dict) for n in raw):
   raise TypeError("STATE nodes must be a list of objects")
  raw_format = bool(raw and "nodeId" in raw[0])
  if not url:
   url = state_or_raw.get("source", {}).get("url", "") or state_or_raw.get("url", "")
  if headers is None and isinstance(state_or_raw.get("headers"), dict):
   headers = state_or_raw.get("headers")
  if not frame_urls and isinstance(state_or_raw.get("frame_urls"), (list, tuple)):
   frame_urls = state_or_raw.get("frame_urls")
 else:
  raise TypeError("state_or_raw must be a STATE dict or raw AX list")
 if raw_format:
  try:
   by_id, ordered, ancestors = _tree(raw)
   nodes = [n for n in ordered if _visible(n, by_id, ancestors)]
  except IncompleteTree:
   nodes = []
 else:
  by_id = {n.get("id"): n for n in raw if n.get("id") is not None}
  def state_visible(node):
   seen, current = set(), node
   while current:
    nid = current.get("id")
    if (nid is not None and nid in seen) or _hidden(current) or current.get("ignored", False):
     return False
    if nid is not None:
     seen.add(nid)
    pid = current.get("parent")
    current = by_id.get(pid) if pid is not None else None
   for anc_id in (node.get("ancestors") or ()):
    if anc_id in by_id and (_hidden(by_id[anc_id]) or by_id[anc_id].get("ignored", False)):
     return False
   return not node.get("ignored", False)
  nodes = [n for n in raw if state_visible(n)]
 if not frame_urls:
  f_list = []
  for n in nodes:
   src = n.get("src")
   if isinstance(src, str) and src and _role(n).casefold() in {"iframe", "frame"}:
    p = _url_parts(src)
    if p is not None and not p[1].lower().endswith((".js", ".css", ".mjs")):
     f_list.append(src)
  frame_urls = f_list
 def stop(reason, confidence, *evidence):
  return {"is_stop": True, "reason": reason, "confidence": confidence,
    "evidence": [{"kind": k, "value": v} for k, v in evidence]}
 parts = _url_parts(url)
 if parts:
  host, path, query, safe = parts
  if (host == "google.com" or host.endswith(".google.com")) and path.rstrip("/") in {"/sorry", "/sorry/index"}:
   return stop("google_sorry", 100, ("url", safe))
 frames = [p for p in (_url_parts(f) for f in frame_urls) if p is not None]
 v2_cue = next((n for n in nodes
     if (_role(n) == "checkbox" and _normal(_name(n)).casefold() in {"i'm not a robot", "no soy un robot"})
     or (_role(n) == "dialog" and _normal(_name(n)).casefold() == "recaptcha")), None)
 for host, path, query, safe in frames:
  if (host in {"www.google.com", "recaptcha.google.com", "www.recaptcha.net"}
    and re.fullmatch(r"/recaptcha/(api2|enterprise)/(anchor|bframe)/?", path)
    and "invisible" not in {x.casefold() for x in query.get("size", [])}
    and v2_cue is not None):
   return stop("recaptcha_v2", 95, ("frame_url", safe),
      ("ax_role_name", _role(v2_cue) + ": " + _normal(_name(v2_cue))))
 turnstile_cue = next((n for n in nodes
       if (_role(n).casefold() == "iframe" and _normal(_name(n)).casefold()
        in {"cloudflare security challenge", "widget containing a cloudflare security challenge"})
       or (_role(n) == "checkbox" and _normal(_name(n)).casefold() == "verify you are human")), None)
 for host, path, query, safe in frames:
  if (host == "challenges.cloudflare.com" and path.startswith("/cdn-cgi/challenge-platform/")
    and not path.lower().endswith((".js", ".css")) and turnstile_cue is not None):
   return stop("turnstile_challenge", 95, ("frame_url", safe),
      ("ax_role_name", _role(turnstile_cue) + ": " + _normal(_name(turnstile_cue))))
 normalized_headers = {str(k).lower(): str(v).strip().lower() for k, v in (headers or {}).items()}
 if (normalized_headers.get("cf-mitigated") == "challenge"
   and normalized_headers.get("content-type", "").split(";", 1)[0].strip() == "text/html"):
  return stop("cloudflare_challenge", 100, ("header", "cf-mitigated: challenge"), ("header", "content-type: text/html"))
 wall_phrases = ("Our systems have detected unusual traffic from your computer network.",
     "Nuestros sistemas han detectado tráfico inusual procedente de tu red de ordenadores.")
 text = _normal(" ".join(_name(n) for n in nodes if _role(n) in {"StaticText", "heading", "alert", "paragraph"})).casefold()
 for phrase in wall_phrases:
  if phrase.casefold() in text or phrase.rstrip(".").casefold() in text:
   return stop("unusual_traffic", 90, ("text", phrase))
 return {"is_stop": False, "reason": None, "confidence": 0, "evidence": []}
