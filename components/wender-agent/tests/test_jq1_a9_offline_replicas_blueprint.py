"""Offline blueprint conformance; synthetic data, not live-site acceptance.

Production: ax.prune/raw_tree, browser.boxes, schema.build_state/validate_state.
ProposedProfileAdapter and ProposedProductionAdapter are LOCAL PROPOSALS: the
production wait_done, text.extract and stopstate.detect signatures are AUSENTE.
No Chromium, screenshots, replay, fixture files, or persistent state are needed.
"""
import builtins
import copy
import io
import json
import os
import re
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
import unittest
from unittest.mock import patch

from wender.act import cdp_executor
from wender.perceive import ax, browser
from wender.state import schema


SESSION = "offline-replica"
VIEWPORT = {"w": 1024, "h": 768, "scroll_x": 0, "scroll_y": 0,
            "doc_w": 1024, "doc_h": 2048, "dpr_ppm": 1000000}
NODE_CATALOG = (
    "root", "main", "model", "menu", "potencia", "effort", "status",
    "user", "user_heading", "prompt", "prompt_text", "user_copy",
    "assistant", "assistant_heading", "article", "answer_heading",
    "answer_heading_text", "paragraph", "answer_text", "list", "item",
    "item_text", "table", "row", "row_text", "code", "code_text",
    "trailer", "trailer_text", "assistant_copy", "stop", "footer",
    "footer_text", "parent_menu", "submenu", "choice", "fast", "thinking",
    "level", "levels", "high", "research_button", "panel", "collapse",
    "panel_heading", "cookies", "reject", "ai_mode", "query", "wall",
    "captcha",
)
BACKEND = {key: 1000 + i for i, key in enumerate(NODE_CATALOG)}
LONG_ANSWER = ("Una respuesta determinista conserva todo el texto original, "
               "incluidos acentos, listas y código, aunque supere sesenta y cuatro bytes.")


def _node(key, role, name, children=(), states=None, box=None, value=None):
    """Exact synthetic CDP subset; box is fixture metadata, not an AX field."""
    props = []
    for k, v in sorted((states or {}).items()):
        typ = "boolean" if isinstance(v, bool) else "integer" if type(v) is int else "token"
        props.append({"name": k, "value": {"type": typ, "value": v}})
    raw = {"nodeId": key, "backendDOMNodeId": BACKEND[key], "ignored": False,
           "role": {"type": "role", "value": role},
           "name": {"type": "computedString", "value": name},
           "childIds": list(children), "properties": props}
    if value is not None:
        raw["value"] = {"type": "number" if type(value) is int else "string", "value": value}
    return raw, box


def _snapshot(title, url, definitions, frames=()):
    nodes, quads = [], {}
    for i, (raw, box) in enumerate(definitions):
        nodes.append(raw)
        if raw["nodeId"] == "root":
            continue  # document has no border box in this declared fixture subset
        x, y, w, h = box if box is not None else (24, 32 + 24 * i, 360, 20)
        quads[raw["backendDOMNodeId"]] = [x, y, x + w, y, x + w, y + h, x, y + h]
    return {"url": url, "title": title, "nodes": nodes, "border_quads": quads,
            "viewport": dict(VIEWPORT), "frames": list(frames)}


def _shell(title, children):
    return [_node("root", "RootWebArea", title, ("main",)),
            _node("main", "main", "", children)]


def _potencia(state):
    opened, expanded = state != "closed", state in ("slider", "selected")
    children = ["model", "status"] + (["menu"] if opened else [])
    nodes = _shell("ChatGPT Potencia replica", children)
    nodes += [_node("model", "button", "Modelo", states={"expanded": opened}),
              _node("status", "status", "Potencia Alta" if state == "selected" else "Potencia Media")]
    if opened:
        nodes += [_node("menu", "menu", "Modelo", ["potencia"] + (["effort"] if expanded else [])),
                  _node("potencia", "menuitem", "Potencia", states={"expanded": expanded},
                        box=(120, 100, 160, 40))]
    if expanded:
        nodes += [_node("effort", "slider", "Potencia Alta" if state == "selected" else "Potencia Media",
                        states={"valuemin": 1, "valuemax": 4}, value=3 if state == "selected" else 2,
                        box=(300, 100, 240, 32))]
    return _snapshot("ChatGPT Potencia replica", "https://chatgpt.com/", nodes)


def _answer_nodes(site, phase, rich=False):
    label, copy_name = ("ChatGPT dijo:", "Copiar mensaje") if site == "chatgpt" else ("Gemini", "Copiar respuesta")
    busy = phase in ("streaming", "streaming_more", "research", "collapsed")
    article_children = ["answer_heading", "paragraph"]
    if rich:
        article_children += ["list", "table", "code", "trailer"]
    nodes = [_node("assistant", "group", "Respuesta de " + ("ChatGPT" if site == "chatgpt" else "Gemini"),
                   ["assistant_heading", "article"] + (["assistant_copy"] if phase == "done" else []),
                   states={"busy": busy}),
             _node("assistant_heading", "heading", label),
             _node("article", "article", "Respuesta", article_children),
             _node("answer_heading", "heading", "Resumen", ["answer_heading_text"]),
             _node("answer_heading_text", "StaticText", "Resumen"),
             _node("paragraph", "paragraph", "", ["answer_text"]),
             _node("answer_text", "StaticText", "Una respuesta" if phase in ("streaming", "research") else LONG_ANSWER)]
    if rich:
        nodes += [_node("list", "list", "", ["item"]),
                  _node("item", "listitem", "", ["item_text"]),
                  _node("item_text", "StaticText", "Primer punto"),
                  _node("table", "table", "", ["row"]),
                  _node("row", "row", "", ["row_text"]),
                  _node("row_text", "StaticText", "clave valor"),
                  _node("code", "code", "", ["code_text"]),
                  _node("code_text", "StaticText", "print('hola')"),
                  _node("trailer", "paragraph", "", ["trailer_text"]),
                  _node("trailer_text", "StaticText", "ChatGPT puede cometer errores. Última respuesta. Respuesta completada.")]
    if phase == "done":
        nodes += [_node("assistant_copy", "button", copy_name)]
    return nodes


def _stream(state):
    streaming = state in ("streaming", "streaming_more")
    children = ["user"] + (["assistant"] if state != "idle" else []) + (["stop"] if streaming else []) + ["footer"]
    nodes = _shell("ChatGPT streaming replica", children)
    nodes += [_node("user", "group", "Usuario", ["user_heading", "prompt", "user_copy"]),
              _node("user_heading", "heading", "Tú dijiste:"),
              _node("prompt", "paragraph", "", ["prompt_text"]),
              _node("prompt_text", "StaticText", "Resume esta idea"),
              _node("user_copy", "button", "Copiar mensaje")]
    if state != "idle":
        nodes += _answer_nodes("chatgpt", state, rich=True)
    if streaming:
        nodes += [_node("stop", "button", "Detener")]
    nodes += [_node("footer", "contentinfo", "ChatGPT puede cometer errores", ["footer_text"]),
              _node("footer_text", "StaticText", "Última respuesta. Respuesta completada.")]
    return _snapshot("ChatGPT streaming replica", "https://chatgpt.com/c/offline", nodes)


def _perplexity(state):
    opened = state in ("menu", "entered", "corridor", "submenu")
    nodes = _shell("Perplexity L-path target replica", ["model", "status"] + (["menu"] if opened else []))
    nodes += [_node("model", "button", "Modelo objetivo" if state == "selected" else "Modelo", states={"expanded": opened}),
              _node("status", "status", "Modelo seleccionado" if state == "selected" else "Elegir modelo")]
    if opened:
        nodes += [_node("menu", "menu", "Modelo", ["parent_menu"] + (["submenu"] if state == "submenu" else [])),
                  _node("parent_menu", "menuitem", "Modelos", states={"expanded": state == "submenu"},
                        box=(100, 100, 160, 40))]
    if state == "submenu":
        nodes += [_node("submenu", "menu", "Modelos", ["choice"], box=(300, 180, 160, 80)),
                  _node("choice", "menuitemradio", "Modelo objetivo", states={"checked": False},
                        box=(300, 200, 160, 40))]
    return _snapshot("Perplexity L-path target replica", "https://www.perplexity.ai/", nodes)


def _gemini(state):
    menu = state in ("models", "levels")
    running = state in ("research", "collapsed")
    answering = running or state == "done"
    children = ["model", "research_button", "status"]
    children += (["menu"] if menu else []) + (["panel"] if state == "research" else [])
    children += (["assistant"] if answering else []) + (["stop"] if running else [])
    nodes = _shell("Gemini research target replica", children)
    nodes += [_node("model", "button", "Gemini", states={"expanded": menu}),
              _node("research_button", "button", "Deep Research", states={"expanded": state == "research"}),
              _node("status", "status", "Investigando" if running else "Nivel Alto" if state in ("configured", "done") else "Nivel estándar",
                    states={"busy": running})]
    if menu:
        nodes += [_node("menu", "menu", "Modelo", ["fast", "thinking", "level"] + (["levels"] if state == "levels" else [])),
                  _node("fast", "menuitemradio", "Rápido", states={"checked": True}),
                  _node("thinking", "menuitemradio", "Razonamiento", states={"checked": False}),
                  _node("level", "menuitem", "Nivel", states={"expanded": state == "levels"})]
    if state == "levels":
        nodes += [_node("levels", "menu", "Nivel", ["high"]),
                  _node("high", "menuitemradio", "Alto", states={"checked": False})]
    if state == "research":
        nodes += [_node("panel", "complementary", "Deep Research", ["panel_heading", "collapse"], states={"busy": True}),
                  _node("panel_heading", "heading", "Plan de investigación"),
                  _node("collapse", "button", "Ocultar Deep Research", states={"expanded": True})]
    if answering:
        nodes += _answer_nodes("gemini", state)
    if running:
        nodes += [_node("stop", "button", "Detener")]
    return _snapshot("Gemini research target replica", "https://gemini.google.com/app/offline", nodes)


def _google(state):
    children = {"cookie": ["cookies"], "search": ["ai_mode"], "ai": ["query"],
                "query": ["query", "status"], "wall": ["wall", "captcha"]}[state]
    nodes = _shell("Google AI mode replica", children)
    if state == "cookie":
        nodes += [_node("cookies", "dialog", "Antes de continuar", ["reject"]),
                  _node("reject", "button", "Rechazar todo")]
    if state == "search":
        nodes += [_node("ai_mode", "button", "Modo IA")]
    if state in ("ai", "query"):
        nodes += [_node("query", "combobox", "Pregunta a Google", value="¿Qué es Wender?" if state == "query" else "")]
    if state == "query":
        nodes += [_node("status", "status", "Consulta preparada")]
    if state == "wall":
        nodes += [_node("wall", "alert", "Our systems have detected unusual traffic"),
                  _node("captcha", "Iframe", "reCAPTCHA")]
    return _snapshot("Google AI mode replica", "https://www.google.com/sorry/index" if state == "wall" else "https://www.google.com/",
                     nodes, frames=({"name": "reCAPTCHA", "url": "https://www.google.com/recaptcha/api2/anchor"},) if state == "wall" else ())


# Each row is (source_state, local_event, AX_nodeId_or_empty, string_value, destination_state).
# These are fixture events, NOT new production executor operation signatures.
REPLICAS = {
    "chatgpt_potencia": {"initial": "closed", "terminal": (), "states": {s: _potencia(s) for s in ("closed", "menu", "slider", "selected")},
        "transitions": (
            ("closed", "js_click", "model", "", "menu"),
            ("menu", "js_click", "potencia", "", "menu"),
            ("menu", "pointer_click", "potencia", "", "slider"),
            ("slider", "press", "effort", "ArrowRight", "selected"),
            ("selected", "press", "effort", "ArrowLeft", "slider"),
            ("menu", "press", "model", "Escape", "closed"),
            ("slider", "press", "effort", "Escape", "closed"))},
    "chatgpt_stream": {"initial": "idle", "terminal": (), "states": {s: _stream(s) for s in ("idle", "streaming", "streaming_more", "settling", "done")},
        "transitions": (("idle", "submit", "", "", "streaming"),
                        ("streaming", "tick", "", "chunk", "streaming_more"),
                        ("streaming_more", "tick", "", "stop_removed", "settling"),
                        ("settling", "tick", "", "assistant_copy", "done"))},
    "perplexity_lpath": {"initial": "closed", "terminal": (), "states": {s: _perplexity(s) for s in ("closed", "menu", "entered", "corridor", "submenu", "selected")},
        "transitions": (("closed", "js_click", "model", "", "menu"),
                        ("menu", "move", "", "180,120", "entered"),
                        ("entered", "move", "", "380,120", "corridor"),
                        ("corridor", "move", "", "380,220", "submenu"),
                        ("menu", "move", "", "280,170", "closed"),
                        ("entered", "move", "", "280,170", "closed"),
                        ("submenu", "js_click", "choice", "", "selected"))},
    "gemini_research": {"initial": "closed", "terminal": (), "states": {s: _gemini(s) for s in ("closed", "models", "levels", "configured", "research", "collapsed", "done")},
        "transitions": (("closed", "js_click", "model", "", "models"),
                        ("models", "js_click", "level", "", "levels"),
                        ("levels", "js_click", "high", "", "configured"),
                        ("configured", "js_click", "research_button", "", "research"),
                        ("research", "js_click", "collapse", "", "collapsed"),
                        ("collapsed", "tick", "", "research_complete", "done"))},
    "google_ai_wall": {"initial": "cookie", "terminal": ("wall",), "states": {s: _google(s) for s in ("cookie", "search", "ai", "query", "wall")},
        "transitions": (("cookie", "js_click", "reject", "", "search"),
                        ("search", "js_click", "ai_mode", "", "ai"),
                        ("ai", "insert_text", "query", "¿Qué es Wender?", "query"),
                        ("query", "press", "query", "Enter", "wall"))},
}


STOP_RULES = {"url_path_prefixes": ["/sorry/"], "frame_name_contains": ["recaptcha", "turnstile"],
              "frame_url_contains": ["/recaptcha/", "challenges.cloudflare.com"],
              "text_contains": ["unusual traffic"], "terminal": True}


def _profile(site, anchor=None, container=None, completion=None):
    wait = extract = None
    if anchor:
        wait = {"anchor": {"role": "heading", "name": anchor},
                "container": {"role": "group", "name": container},
                "completion": {"role": "button", "name": completion},
                "blocking": [{"role": "button", "name": "Detener"}],
                "stable_reads": 2, "max_reads": 4}
        extract = {"region_start": dict(wait["anchor"]), "container": dict(wait["container"]),
                   "region_end": dict(wait["completion"]),
                   "block_roles": ["paragraph", "heading", "listitem", "row", "code"],
                   "trailer_prefixes": ["ChatGPT puede cometer errores", "Última respuesta", "Respuesta completada"]}
    return {"schema": "offline-site-profile/v1", "site": site, "wait_done": wait,
            "text.extract": extract, "stopstate.detect": copy.deepcopy(STOP_RULES)}


PROFILES = {site: _profile(site) for site in REPLICAS}
PROFILES["chatgpt_stream"] = _profile("chatgpt_stream", "ChatGPT dijo:", "Respuesta de ChatGPT", "Copiar mensaje")
PROFILES["gemini_research"] = _profile("gemini_research", "Gemini", "Respuesta de Gemini", "Copiar respuesta")
PROFILES["google_ai_wall"] = _profile("google_ai_wall", "Respuesta de IA", "Respuesta de Google", "Copiar respuesta")


class MissingTransition(ValueError):
    pass


class UnsupportedCdp(ValueError):
    pass


class FakeCdp:
    """RAM-only, exact-table reducer and narrow CDP reader; no generic success reply."""
    READ_METHODS = ("Accessibility.enable", "Accessibility.disable", "Accessibility.getFullAXTree",
                    "DOM.getDocument", "DOM.getBoxModel")

    def __init__(self, site, definition=None):
        self.site = site
        self.definition = copy.deepcopy(REPLICAS[site] if definition is None else definition)
        self.state = self.definition["initial"]
        self.table = {}
        for row in self.definition["transitions"]:
            key, destination = tuple(row[:4]), row[4]
            if key in self.table:
                raise ValueError("duplicate transition: %r" % (key,))
            if key[0] not in self.definition["states"] or destination not in self.definition["states"]:
                raise ValueError("undefined state")
            if key[0] in self.definition["terminal"]:
                raise ValueError("terminal state has an outgoing transition")
            self.table[key] = destination
        self.calls = []

    def snapshot(self):
        return copy.deepcopy(self.definition["states"][self.state])

    def step(self, event, target="", value=""):
        key = (self.state, event, target, value)
        if key not in self.table:
            raise MissingTransition("no declared transition: %r" % (key,))
        self.state = self.table[key]
        return self.snapshot()

    def send(self, method, params=None, session_id=None):
        if session_id != SESSION:
            raise UnsupportedCdp("unknown session")
        params = {} if params is None else params
        if method not in self.READ_METHODS:
            raise UnsupportedCdp(method)
        snap = self.snapshot()
        if method in ("Accessibility.enable", "Accessibility.disable", "Accessibility.getFullAXTree"):
            if params:
                raise UnsupportedCdp("unexpected AX parameters")
            reply = {"nodes": snap["nodes"]} if method.endswith("getFullAXTree") else {}
        elif method == "DOM.getDocument":
            if params != {"depth": 0}:
                raise UnsupportedCdp("only depth=0 is declared")
            reply = {"root": {"nodeId": 1, "backendNodeId": BACKEND["root"], "nodeType": 9,
                              "nodeName": "#document", "localName": "", "nodeValue": ""}}
        else:
            if set(params) != {"backendNodeId"} or type(params["backendNodeId"]) is not int:
                raise UnsupportedCdp("expected integer backendNodeId")
            bid = params["backendNodeId"]
            if bid not in snap["border_quads"]:
                raise ValueError("node has no layout box")
            reply = {"model": {"border": snap["border_quads"][bid]}}
        self.calls.append((method, copy.deepcopy(params), session_id))
        return reply


class PotenciaExecutorCdp(FakeCdp):
    """Narrow WP1-03 bridge from the implemented executor to the same local table.

    Only Potencia's fixed JS click and moved/pressed/released center sequence
    are recognized. This is not a general mouse, keyboard, or browser emulator.
    """
    def __init__(self):
        super().__init__("chatgpt_potencia")
        self.step("js_click", "model")
        self.pointer_events = []

    def send(self, method, params=None, session_id=None):
        if method in self.READ_METHODS:
            return super().send(method, params, session_id)
        if session_id != SESSION:
            raise UnsupportedCdp("unknown session")
        expected = {"backendNodeId": BACKEND["potencia"]}
        if method in ("DOM.scrollIntoViewIfNeeded", "DOM.resolveNode") and params == expected:
            result = {"object": {"objectId": "offline:potencia"}} if method == "DOM.resolveNode" else {}
        elif method == "Runtime.callFunctionOn" and params == {
                "objectId": "offline:potencia", "functionDeclaration": cdp_executor.JS_BY_OP["click"],
                "arguments": [], "returnByValue": True}:
            self.step("js_click", "potencia")
            result = {"result": {"type": "undefined"}}
        elif method == "Input.dispatchMouseEvent":
            sequence = [
                {"type": "mouseMoved", "x": 200, "y": 120},
                {"type": "mousePressed", "x": 200, "y": 120, "button": "left", "clickCount": 1},
                {"type": "mouseReleased", "x": 200, "y": 120, "button": "left", "clickCount": 1},
            ]
            index = len(self.pointer_events)
            if index >= len(sequence) or params != sequence[index]:
                raise UnsupportedCdp("undeclared pointer sequence")
            if index == 2:
                self.step("pointer_click", "potencia")
            self.pointer_events.append(copy.deepcopy(params))
            result = {}
        else:
            raise UnsupportedCdp(method)
        self.calls.append((method, copy.deepcopy(params), session_id))
        return result


def _run_potencia_executor(expect=None):
    fake = PotenciaExecutorCdp()
    nodes = ax.prune(fake.snapshot()["nodes"])
    target = next(n for n in nodes if n["node_id"] == "potencia")
    action = {"op": "click", "target": target["id"], "irreversible": False}
    if expect is not None:
        action["expect"] = expect
    receipt = cdp_executor.run_ax_action(
        fake, SESSION, nodes, action, lambda: ax.prune(fake.snapshot()["nodes"]),
        retries=1, wait_ms=0, sleep=lambda _: None, allow_coordinates=True)
    return fake, receipt


def _raw_order(snapshot):
    """Local raw DFS, deliberately not ax.prune (article text must remain available)."""
    by_id = {n["nodeId"]: n for n in snapshot["nodes"]}
    out, parents, seen = [], {}, set()
    stack = [("root", None)]
    while stack:
        key, parent = stack.pop()
        if key in seen:
            raise ValueError("duplicate/cyclic raw node")
        seen.add(key)
        node = by_id[key]
        out.append(node)
        parents[key] = parent
        stack.extend((child, key) for child in reversed(node["childIds"]))
    if seen != set(by_id):
        raise ValueError("unreachable raw node")
    return out, parents


def _matches(node, predicate):
    return all(node[field]["value"] == predicate[field] for field in ("role", "name"))


def _region(snapshot, start, container, end):
    ordered, parents = _raw_order(snapshot)
    by_id = {n["nodeId"]: n for n in ordered}
    anchors = [i for i, n in enumerate(ordered) if _matches(n, start)]
    if not anchors:
        return []
    begin = anchors[-1]
    ancestor = parents[ordered[begin]["nodeId"]]
    while ancestor is not None and not _matches(by_id[ancestor], container):
        ancestor = parents[ancestor]
    if ancestor is None:
        return []
    def inside(key):
        while key is not None:
            if key == ancestor:
                return True
            key = parents[key]
        return False
    region = []
    for node in ordered[begin + 1:]:
        if not inside(node["nodeId"]):
            return []  # incomplete/missing end, never borrow another turn's copy
        if _matches(node, end):
            return region
        region.append(node)
    return []


class ProposedProfileAdapter:
    """LOCAL executable rule oracle, not a production API or an observed site profile."""
    @staticmethod
    def detect(snapshot, rules):
        from urllib.parse import urlsplit
        reasons = []
        if any(urlsplit(snapshot["url"]).path.startswith(p) for p in rules["url_path_prefixes"]):
            reasons.append("url")
        for frame in snapshot["frames"]:
            if (any(s in frame["name"].lower() for s in rules["frame_name_contains"])
                    or any(s in frame["url"].lower() for s in rules["frame_url_contains"])):
                reasons.append("frame")
                break
        raw, _ = _raw_order(snapshot)
        if any(s in n["name"]["value"].lower() for s in rules["text_contains"] for n in raw):
            reasons.append("text")
        return {"status": "STOP" if reasons else "CONTINUE", "reasons": reasons,
                "handoff": "human" if reasons else None}

    @staticmethod
    def wait_done(snapshots, rules, *, sleep):
        """Bounded scripted reads; stability covers raw answer region, not pruned hashes."""
        previous, stable, reads = None, 0, 0
        for snap in islice(snapshots, rules["max_reads"]):
            reads += 1
            nodes, _ = _raw_order(snap)
            blocked = any(_matches(n, pred) for pred in rules["blocking"] for n in nodes)
            blocked |= any(p["name"] == "busy" and p["value"]["value"] is True for n in nodes for p in n["properties"])
            region = _region(snap, rules["anchor"], rules["container"], rules["completion"])
            signature = schema.dumps(region) if region and not blocked else None
            stable = stable + 1 if signature is not None and signature == previous else 1 if signature is not None else 0
            previous = signature
            if stable >= rules["stable_reads"]:
                return {"status": "DONE", "reads": reads}
            if reads < rules["max_reads"]:
                sleep(0)  # injected no-op: no clock, delay, scheduling, or network
        return {"status": "PENDING", "reads": reads}

    @staticmethod
    def extract(snapshot, rules):
        region = _region(snapshot, rules["region_start"], rules["container"], rules["region_end"])
        _, parents = _raw_order(snapshot)
        by_id = {n["nodeId"]: n for n in region}
        blocks = []
        for node in region:
            if node["role"]["value"] != "StaticText":
                continue
            text = node["name"]["value"]
            if any(text.startswith(prefix) for prefix in rules["trailer_prefixes"]):
                break
            key = parents[node["nodeId"]]
            while key in by_id and by_id[key]["role"]["value"] not in rules["block_roles"]:
                key = parents[key]
            if key not in by_id:
                continue
            if blocks and blocks[-1]["node"] == key:
                blocks[-1]["text"] += " " + text
            else:
                blocks.append({"node": key, "role": by_id[key]["role"]["value"], "text": text})
        return [{"role": b["role"], "text": b["text"]} for b in blocks]


class ProposedProductionAdapter:
    """Explicit fixture adapters mapping blueprint snapshots/profiles to canonical F1 interfaces."""
    @staticmethod
    def _adapt_turn_nodes(snapshot, container=None):
        nodes = copy.deepcopy(snapshot["nodes"])
        container_ids = set()
        for node in nodes:
            if container and _matches(node, container):
                node["role"]["value"] = "article"
                container_ids.add(node["nodeId"])
        if container_ids:
            for node in nodes:
                if node["nodeId"] not in container_ids and node["role"]["value"] == "article":
                    node["role"]["value"] = "region"
        return nodes

    @staticmethod
    def wait_done(implementation, snapshots, rules):
        container = rules.get("container")
        adapted = [ProposedProductionAdapter._adapt_turn_nodes(s, container) for s in snapshots]
        idx = 0

        def snapshot_reader():
            nonlocal idx
            if not adapted:
                return []
            res = adapted[min(idx, len(adapted) - 1)]
            idx += 1
            return copy.deepcopy(res)

        now_ms = 0

        def clock():
            return now_ms

        def sleep(ms):
            nonlocal now_ms
            if type(ms) is not int or ms < 0:
                raise ValueError("sleep expects non-negative integer milliseconds")
            now_ms += ms

        canonical_rules = {
            "absent_roles_names": [dict(p) for p in rules.get("blocking", ())],
            "after_last_anchor": dict(rules["anchor"]) if rules.get("anchor") else None,
            "present_roles_names": [dict(rules["completion"])] if rules.get("completion") else [],
            "quiet_snapshots": int(rules.get("stable_reads", 2)),
        }
        return implementation(
            snapshot_reader,
            canonical_rules,
            clock=clock,
            sleep=sleep,
            max_polls=int(rules.get("max_reads", 4)),
            poll_interval_ms=100,
        )

    @staticmethod
    def extract(implementation, snapshot, rules):
        raw_nodes = ProposedProductionAdapter._adapt_turn_nodes(snapshot, rules.get("container"))
        prefixes = tuple(rules.get("trailer_prefixes", ()))
        trailers = list(prefixes)
        for node in raw_nodes:
            if node["role"]["value"] == "StaticText":
                txt = node["name"]["value"].strip()
                if txt and any(txt.startswith(p) for p in prefixes) and txt not in trailers:
                    trailers.append(txt)
        return implementation(
            raw_nodes,
            dict(rules["region_start"]),
            dict(rules["region_end"]),
            excluded_roles_names=(),
            trailers=tuple(trailers),
        )

    @staticmethod
    def detect(implementation, snapshot, rules):
        frame_urls = tuple(
            f["url"] for f in snapshot.get("frames", ())
            if isinstance(f, dict) and isinstance(f.get("url"), str)
        )
        return implementation(
            copy.deepcopy(snapshot["nodes"]),
            url=snapshot.get("url", ""),
            frame_urls=frame_urls,
            headers=dict(snapshot.get("headers") or {}),
        )


def _state(cdp):
    raw = ax.raw_tree(cdp, SESSION)
    pruned = ax.prune(raw)
    snap = cdp.snapshot()
    boxes = browser.boxes(cdp, SESSION, pruned)
    ax.release(cdp, SESSION)
    return schema.build_state(pruned, kind="cdp", url=snap["url"], title=ax.normalize_name(snap["title"]),
                              ax_sha256=ax.snapshot_sha(pruned, True), ax_stable_sha256=ax.snapshot_sha(pruned, False),
                              prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION}, raw_count=len(raw),
                              boxes=boxes, viewport=snap["viewport"], actionable_roles=ax.ACTIONABLE_ROLES)


@contextmanager
def _offline_guard():
    """Fail before filesystem mutation, networking, processes, or wall-clock sleep."""
    attempts = []
    def deny(*args, **kwargs):
        attempts.append("side_effect")
        raise AssertionError("offline blueprint attempted a side effect")
    def checked_open(original):
        def call(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                return deny()
            return original(file, mode, *args, **kwargs)
        return call
    original_os_open = os.open
    def checked_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return deny()
        return original_os_open(path, flags, *args, **kwargs)
    with ExitStack() as stack:
        stack.enter_context(patch("builtins.open", checked_open(builtins.open)))
        stack.enter_context(patch("io.open", checked_open(io.open)))
        stack.enter_context(patch("os.open", checked_os_open))
        for name in ("os.mkdir", "os.remove", "os.unlink", "os.rename", "os.replace", "os.rmdir",
                     "socket.socket", "subprocess.Popen", "time.sleep"):
            stack.enter_context(patch(name, deny))
        yield attempts


class BlueprintFixtureConformance(unittest.TestCase):
    """Passing tests for declared data and local adapters, with no production gap pins."""
    def test_tables_are_deterministic_complete_and_all_states_reachable(self):
        self.assertEqual(len(REPLICAS), 5)
        for site, definition in REPLICAS.items():
            with self.subTest(site=site):
                reached, queue = {definition["initial"]}, [definition["initial"]]
                while queue:
                    source = queue.pop()
                    for row in definition["transitions"]:
                        if row[0] != source:
                            continue
                        left, right = FakeCdp(site), FakeCdp(site)
                        left.state = right.state = source
                        a, b = left.step(*row[1:4]), right.step(*row[1:4])
                        self.assertEqual(left.state, row[4])
                        self.assertEqual(a, b)
                        self.assertEqual(a, definition["states"][row[4]])
                        if row[4] not in reached:
                            reached.add(row[4])
                            queue.append(row[4])
                self.assertEqual(reached, set(definition["states"]))

    def test_duplicate_and_missing_transitions_fail_without_mutation(self):
        for site, definition in REPLICAS.items():
            broken = copy.deepcopy(definition)
            broken["transitions"] += (broken["transitions"][0],)
            with self.subTest(site=site), self.assertRaisesRegex(ValueError, "duplicate transition"):
                FakeCdp(site, broken)
        fake = FakeCdp("perplexity_lpath")
        fake.step("js_click", "model")
        before = fake.snapshot()
        # move is supported; neither parent entry nor the elbow may be skipped.
        for shortcut in ("380,120", "380,220"):
            with self.assertRaises(MissingTransition):
                fake.step("move", "", shortcut)
            self.assertEqual(fake.state, "menu")
            self.assertEqual(fake.snapshot(), before)

    def test_all_raw_trees_and_border_quads_have_the_exact_declared_shape(self):
        required = {"nodeId", "backendDOMNodeId", "ignored", "role", "name", "childIds", "properties"}
        for site, definition in REPLICAS.items():
            for state, snap in definition["states"].items():
                with self.subTest(site=site, state=state):
                    ordered, parents = _raw_order(snap)
                    self.assertEqual(len(ordered), len(snap["nodes"]))
                    self.assertEqual([k for k, p in parents.items() if p is None], ["root"])
                    self.assertEqual(len({n["backendDOMNodeId"] for n in ordered}), len(ordered))
                    self.assertEqual(set(snap["viewport"]), set(VIEWPORT))
                    for node in ordered:
                        self.assertTrue(required <= set(node) <= required | {"value"})
                        self.assertEqual(type(node["nodeId"]), str)
                        self.assertEqual(type(node["backendDOMNodeId"]), int)
                        self.assertGreater(node["backendDOMNodeId"], 0)
                        self.assertEqual(node["backendDOMNodeId"], BACKEND[node["nodeId"]])
                        self.assertIs(node["ignored"], False)
                        self.assertEqual(set(node["role"]), {"type", "value"})
                        self.assertEqual(node["role"]["type"], "role")
                        self.assertEqual(set(node["name"]), {"type", "value"})
                        self.assertEqual(node["name"]["type"], "computedString")
                        self.assertIsInstance(node["role"]["value"], str)
                        self.assertIsInstance(node["name"]["value"], str)
                        self.assertEqual(type(node["childIds"]), list)
                        self.assertTrue(all(type(v) is str for v in node["childIds"]))
                        self.assertEqual(type(node["properties"]), list)
                        self.assertEqual(len(node["childIds"]), len(set(node["childIds"])))
                        self.assertEqual(len(node["properties"]), len({p["name"] for p in node["properties"]}))
                        for prop in node["properties"]:
                            self.assertEqual(set(prop), {"name", "value"})
                            self.assertEqual(set(prop["value"]), {"type", "value"})
                            self.assertIn(prop["value"]["type"], ("boolean", "integer", "token"))
                            self.assertIsInstance(prop["name"], str)
                            self.assertEqual(type(prop["value"]["value"]), {"boolean": bool, "integer": int, "token": str}[prop["value"]["type"]])
                        if "value" in node:
                            self.assertEqual(set(node["value"]), {"type", "value"})
                            self.assertIn(node["value"]["type"], ("number", "string"))
                            self.assertEqual(type(node["value"]["value"]), {"number": int, "string": str}[node["value"]["type"]])
                    self.assertEqual(set(snap["border_quads"]), {n["backendDOMNodeId"] for n in ordered if n["nodeId"] != "root"})
                    for quad in snap["border_quads"].values():
                        self.assertEqual(len(quad), 8)
                        self.assertTrue(all(type(v) is int for v in quad))
                        x, y, right, _, _, bottom, _, _ = quad
                        self.assertEqual(quad, [x, y, right, y, right, bottom, x, bottom])
                        self.assertGreater(right, x)
                        self.assertGreater(bottom, y)
                    schema.dumps(snap)  # rejects floats in every nested declaration

    def test_replies_are_isolated_and_unknown_methods_are_rejected(self):
        fake = FakeCdp("chatgpt_potencia")
        raw = fake.send("Accessibility.getFullAXTree", session_id=SESSION)
        raw["nodes"].clear()
        self.assertTrue(fake.snapshot()["nodes"])
        for method in ("DOM.querySelector", "Runtime.evaluate", "Page.captureScreenshot", "Network.enable"):
            with self.assertRaises(UnsupportedCdp):
                fake.send(method, session_id=SESSION)

    def test_potencia_pointer_and_slider_visible_selection(self):
        fake = FakeCdp("chatgpt_potencia")
        fake.step("js_click", "model")
        before = fake.snapshot()
        self.assertEqual(fake.step("js_click", "potencia"), before)
        fake.step("pointer_click", "potencia")
        self.assertEqual(fake.state, "slider")
        fake.step("press", "effort", "ArrowRight")
        by_id = {n["nodeId"]: n for n in fake.snapshot()["nodes"]}
        self.assertEqual(by_id["effort"]["value"], {"type": "number", "value": 3})
        self.assertEqual(by_id["status"]["name"]["value"], "Potencia Alta")

    def test_perplexity_l_path_and_diagonal_trap(self):
        fake = FakeCdp("perplexity_lpath")
        fake.step("js_click", "model")
        fake.step("move", "", "180,120")
        fake.step("move", "", "280,170")
        self.assertEqual(fake.state, "closed")
        fake.step("js_click", "model")
        for point in ("180,120", "380,120", "380,220"):
            fake.step("move", "", point)
        self.assertEqual(fake.state, "submenu")
        fake.step("js_click", "choice")
        self.assertEqual(fake.state, "selected")

    def test_profile_schemas_and_wp1_01_ax_only_rules(self):
        for site, profile in PROFILES.items():
            self.assertEqual(set(profile), {"schema", "site", "wait_done", "text.extract", "stopstate.detect"})
            self.assertEqual(profile["site"], site)
            self.assertEqual(profile["schema"], "offline-site-profile/v1")
            stop = profile["stopstate.detect"]
            self.assertEqual(set(stop), {"url_path_prefixes", "frame_name_contains", "frame_url_contains", "text_contains", "terminal"})
            self.assertIs(stop["terminal"], True)
            if profile["wait_done"] is not None:
                wait = profile["wait_done"]
                self.assertEqual(set(wait), {"anchor", "container", "completion", "blocking", "stable_reads", "max_reads"})
                for pred in [wait["anchor"], wait["container"], wait["completion"]] + wait["blocking"]:
                    self.assertEqual(set(pred), {"role", "name"})
                    self.assertTrue(all(type(v) is str for v in pred.values()))
                self.assertTrue(1 <= wait["stable_reads"] <= wait["max_reads"])
                self.assertTrue(all(type(wait[k]) is int for k in ("stable_reads", "max_reads")))
                extract = profile["text.extract"]
                self.assertEqual(set(extract), {"region_start", "container", "region_end", "block_roles", "trailer_prefixes"})
                self.assertEqual(extract["region_start"], wait["anchor"])
                self.assertEqual(extract["region_end"], wait["completion"])
            else:
                self.assertIsNone(profile["text.extract"])
            serialized = schema.dumps(profile)
            for forbidden in ("data-message-author-role", "querySelector", "css_selector", "xpath", "getElementById"):
                self.assertNotIn(forbidden, serialized)
        self.assertFalse(any("querySelector" in method for method in FakeCdp.READ_METHODS))

    def test_chatgpt_user_copy_and_settling_are_not_completion(self):
        states, rules = REPLICAS["chatgpt_stream"]["states"], PROFILES["chatgpt_stream"]["wait_done"]
        for phase in ("idle", "streaming", "streaming_more", "settling"):
            with self.subTest(phase=phase):
                got = ProposedProfileAdapter.wait_done([states[phase]] * 4, rules, sleep=lambda _: None)
                self.assertEqual(got, {"status": "PENDING", "reads": 4})
        got = ProposedProfileAdapter.wait_done([states[s] for s in ("streaming_more", "settling", "done", "done")], rules, sleep=lambda _: None)
        self.assertEqual(got, {"status": "DONE", "reads": 4})

    def test_wait_budget_and_last_assistant_anchor_are_explicit(self):
        states, rules = REPLICAS["chatgpt_stream"]["states"], PROFILES["chatgpt_stream"]["wait_done"]
        def budgeted_snapshots():
            yield from [states["idle"]] * 4
            self.fail("wait_done consumed a snapshot beyond its declared read budget")
        self.assertEqual(ProposedProfileAdapter.wait_done(budgeted_snapshots(), rules, sleep=lambda _: None),
                         {"status": "PENDING", "reads": 4})
        # A new trailing assistant heading makes the old completion button ineligible.
        late = copy.deepcopy(states["done"])
        old = next(n for n in late["nodes"] if n["nodeId"] == "assistant")
        old["childIds"].append("late_heading")
        extra = copy.deepcopy(next(n for n in late["nodes"] if n["nodeId"] == "assistant_heading"))
        extra.update(nodeId="late_heading", backendDOMNodeId=9000)
        late["nodes"].append(extra)
        self.assertEqual(ProposedProfileAdapter.wait_done([late] * 2, rules, sleep=lambda _: None)["status"], "PENDING")

    def test_raw_extraction_opaque_article_blocks_and_footer_cut(self):
        snap, rules = REPLICAS["chatgpt_stream"]["states"]["done"], PROFILES["chatgpt_stream"]["text.extract"]
        self.assertGreater(len(LONG_ANSWER.encode("utf-8")), 64)
        self.assertEqual(ProposedProfileAdapter.extract(snap, rules), [
            {"role": "heading", "text": "Resumen"}, {"role": "paragraph", "text": LONG_ANSWER},
            {"role": "listitem", "text": "Primer punto"}, {"role": "row", "text": "clave valor"},
            {"role": "code", "text": "print('hola')"}])
        self.assertEqual(ProposedProfileAdapter.extract(REPLICAS["chatgpt_stream"]["states"]["settling"], rules), [])

    def test_gemini_collapsing_research_is_not_completion(self):
        fake = FakeCdp("gemini_research")
        for event in (("js_click", "model"), ("js_click", "level"), ("js_click", "high"),
                      ("js_click", "research_button"), ("js_click", "collapse")):
            fake.step(*event)
        self.assertEqual(fake.state, "collapsed")
        rules = PROFILES[fake.site]
        self.assertEqual(ProposedProfileAdapter.wait_done([fake.snapshot()] * 4, rules["wait_done"], sleep=lambda _: None)["status"], "PENDING")
        fake.step("tick", "", "research_complete")
        self.assertEqual(ProposedProfileAdapter.wait_done([fake.snapshot()] * 2, rules["wait_done"], sleep=lambda _: None), {"status": "DONE", "reads": 2})
        self.assertEqual(ProposedProfileAdapter.extract(fake.snapshot(), rules["text.extract"])[1]["text"], LONG_ANSWER)

    def test_google_wall_is_terminal_and_each_stop_signal_is_sufficient(self):
        fake = FakeCdp("google_ai_wall")
        for row in REPLICAS[fake.site]["transitions"]:
            fake.step(*row[1:4])
        rules, before = PROFILES[fake.site]["stopstate.detect"], fake.snapshot()
        self.assertEqual(ProposedProfileAdapter.detect(before, rules), {"status": "STOP", "reasons": ["url", "frame", "text"], "handoff": "human"})
        with self.assertRaises(MissingTransition):
            fake.step("press", "query", "Enter")
        self.assertEqual(fake.snapshot(), before)
        for keep in ("url", "frame", "text"):
            sample = copy.deepcopy(before)
            if keep != "url":
                sample["url"] = "https://www.google.com/"
            if keep != "frame":
                sample["frames"] = []
            if keep != "text":
                next(n for n in sample["nodes"] if n["nodeId"] == "wall")["name"]["value"] = "Challenge"
            self.assertEqual(ProposedProfileAdapter.detect(sample, rules)["reasons"], [keep])
        for site, definition in REPLICAS.items():
            self.assertEqual(ProposedProfileAdapter.detect(definition["states"][definition["initial"]], PROFILES[site]["stopstate.detect"])["status"], "CONTINUE")

    def test_reasoning_artifact_covers_replicas_and_unsupported_operations(self):
        path = Path(__file__).resolve().parents[1] / "docs/spec/jq1_a9_offline_replicas_blueprint.md"
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.strip())
        for required in (*REPLICAS, "tests/fixtures/sites/", "Accessibility.getFullAXTree", "DOM.getBoxModel",
                         "wait_done", "text.extract", "stopstate.detect", "AUSENTE", "press", "insert_text",
                         "WP1-01", "WP1-02", "WP1-03", "WP1-04", "WP1-05", "Storage Law", "target", "ProposedProfileAdapter"):
            self.assertTrue(required in text, "reasoning artifact missing marker: " + required)
        for path in ("wender/perceive/ax.py", "wender/perceive/browser.py", "wender/act/cdp_executor.py",
                     "wender/learn/chrome_driver.py", "wender/learn/replay.py", "docs/spec/KNOWN_ISSUES_P1_WEB.md"):
            self.assertRegex(text, path.replace(".", r"\.") + r":\d+")
        recipes = re.findall(r"```(?:python|javascript|html)\n(.*?)\n```", text, flags=re.S)
        self.assertTrue(any("<!doctype html>" in recipe for recipe in recipes),
                        "reasoning artifact must include its self-contained HTML/JS recipe")
        for recipe in recipes:
            for forbidden in ("data-message-author-role", "querySelector", "getElementById", "document.evaluate"):
                self.assertNotIn(forbidden, recipe, "WP1-01: recipe must retain references and use AX predicates")


class ProductionAxStateConformance(unittest.TestCase):
    """Passing production integration assertions for every declared state and edge."""
    def _assert_snapshot(self, fake):
        raw = fake.snapshot()["nodes"]
        original = copy.deepcopy(raw)
        pruned = ax.prune(raw)
        self.assertEqual(raw, original)
        self.assertEqual(pruned, ax.prune(list(reversed(raw))))
        state = _state(fake)
        self.assertEqual(schema.validate_state(state), [])
        self.assertEqual(schema.dumps(state), schema.dumps(_state(fake)))
        self.assertEqual(state["counts"]["raw"], len(raw))
        self.assertEqual(state["ax_sha256"], ax.snapshot_sha(pruned, True))
        self.assertEqual(state["ax_stable_sha256"], ax.snapshot_sha(pruned, False))
        self.assertEqual([n["node_id"] for n in pruned if n["role"] == "StaticText"], [])
        self.assertNotIn("answer_text", [n["node_id"] for n in pruned])
        for node in state["nodes"]:
            self.assertNotIn("backend_id", node)
            raw_id = next(p["node_id"] for p in pruned if p["id"] == node["id"])
            if raw_id == "root":
                self.assertIsNone(node["box"])
            else:
                quad = fake.snapshot()["border_quads"][BACKEND[raw_id]]
                self.assertEqual(node["box"], [quad[0], quad[1], quad[2] - quad[0], quad[5] - quad[1]])

    def test_initial_and_every_post_transition_tree_prune_and_validate(self):
        for site, definition in REPLICAS.items():
            with self.subTest(site=site, state="initial"):
                self._assert_snapshot(FakeCdp(site))
            for row in definition["transitions"]:
                with self.subTest(site=site, edge=row):
                    fake = FakeCdp(site)
                    fake.state = row[0]
                    self._assert_snapshot(fake)
                    fake.step(*row[1:4])
                    self._assert_snapshot(fake)

    def test_opaque_answer_and_slider_raw_value_are_not_hashed_as_text(self):
        a, b = REPLICAS["chatgpt_stream"]["states"]["streaming"], REPLICAS["chatgpt_stream"]["states"]["streaming_more"]
        self.assertNotEqual(a["nodes"], b["nodes"])
        self.assertEqual(ax.snapshot_sha(ax.prune(a["nodes"])), ax.snapshot_sha(ax.prune(b["nodes"])))
        slider = copy.deepcopy(REPLICAS["chatgpt_potencia"]["states"]["slider"])
        sha = ax.snapshot_sha(ax.prune(slider["nodes"]))
        next(n for n in slider["nodes"] if n["nodeId"] == "effort")["value"]["value"] = 3
        self.assertEqual(ax.snapshot_sha(ax.prune(slider["nodes"])), sha)
        selected = REPLICAS["chatgpt_potencia"]["states"]["selected"]
        self.assertNotEqual(ax.snapshot_sha(ax.prune(selected["nodes"])), sha)

    def test_storage_law_zero_writes_for_all_blueprint_states_and_transitions(self):
        with _offline_guard() as attempts:
            for site, definition in REPLICAS.items():
                for phase in definition["states"]:
                    fake = FakeCdp(site)
                    fake.state = phase
                    self.assertEqual(schema.validate_state(_state(fake)), [])
                    snap, rules = fake.snapshot(), PROFILES[site]
                    ProposedProfileAdapter.detect(snap, rules["stopstate.detect"])
                    if rules["wait_done"] is not None:
                        ProposedProfileAdapter.wait_done([snap] * 4, rules["wait_done"], sleep=lambda _: None)
                        ProposedProfileAdapter.extract(snap, rules["text.extract"])
                for row in definition["transitions"]:
                    fake = FakeCdp(site)
                    fake.state = row[0]
                    fake.step(*row[1:4])
                    self.assertEqual(schema.validate_state(_state(fake)), [])
        self.assertEqual(attempts, [])

    def test_write_guard_rejects_before_any_disk_mutation(self):
        for operation in (lambda: builtins.open("unused-blueprint-file", "wb"),
                          lambda: Path("unused-blueprint-file").write_bytes(b"never written"),
                          lambda: os.open("unused-blueprint-file", os.O_WRONLY | os.O_CREAT, 0o600)):
            with _offline_guard() as attempts:
                with self.assertRaisesRegex(AssertionError, "side effect"):
                    operation()
            self.assertEqual(attempts, ["side_effect"])


class MissingProductionOperations(unittest.TestCase):
    def test_wp1_02_production_wait_ignores_user_copy(self):
        # WP1-02: canonical F1 completion receipt; user-copy witness in idle state must not complete.
        from wender.perceive.wait import wait_done
        snap = REPLICAS["chatgpt_stream"]["states"]["idle"]
        result = ProposedProductionAdapter.wait_done(wait_done, [snap] * 4, PROFILES["chatgpt_stream"]["wait_done"])
        self.assertEqual(result, {"done": False, "polls": 4, "completion_status": "timeout"})

    def test_wp1_04_production_extract_removes_footer(self):
        # WP1-04: canonical F1 text/extraction_status envelope; footer and trailer witnesses excluded.
        from wender.perceive.text import extract
        snap = REPLICAS["chatgpt_stream"]["states"]["done"]
        result = ProposedProductionAdapter.extract(extract, snap, PROFILES["chatgpt_stream"]["text.extract"])
        expected_lines = ["Resumen", LONG_ANSWER, "Primer punto", "clave valor", "print('hola')"]
        self.assertEqual(result, {"text": "\n".join(expected_lines), "extraction_status": "clean"})
        self.assertEqual(result["text"].splitlines()[1], LONG_ANSWER)
        self.assertEqual(len(result["text"].splitlines()), 5)
        self.assertNotIn("ChatGPT puede cometer errores", result["text"])

    def test_wp1_05_production_wall_requires_human_handoff(self):
        # WP1-05: canonical F1 is_stop/reason/confidence/evidence envelope; never solve or evade the stop wall.
        from wender.perceive.stopstate import detect
        result = ProposedProductionAdapter.detect(detect, REPLICAS["google_ai_wall"]["states"]["wall"], STOP_RULES)
        self.assertEqual(
            result,
            {
                "is_stop": True,
                "reason": "google_sorry",
                "confidence": 100,
                "evidence": [{"kind": "url", "value": "https://www.google.com/sorry/index"}],
            },
        )


class ProductionPointerFixture(unittest.TestCase):
    def test_explicit_expansion_expectation_opens_potencia_by_pointer(self):
        with _offline_guard() as attempts:
            fake, receipt = _run_potencia_executor({"target_states": {"expanded": "true"}})
            self.assertEqual(receipt["ok"], 1)
            self.assertEqual(receipt["path"], "coordinates")
            self.assertEqual(fake.state, "slider")
            self.assertEqual([event["type"] for event in fake.pointer_events],
                             ["mouseMoved", "mousePressed", "mouseReleased"])
            self.assertEqual(schema.validate_state(_state(fake)), [])
        self.assertEqual(attempts, [])


class KnownExecutorGap(unittest.TestCase):
    def setUp(self):
        # Setup is outside expectedFailure: transport/setup errors must fail normally.
        self.fake, self.receipt = _run_potencia_executor()
        self.assertEqual(self.receipt["ok"], 1)
        self.assertEqual(schema.validate_state(_state(self.fake)), [])

    def test_wp1_03_unchanged_js_click_should_open_pointer_submenu(self):
        # WP1-03: absent expect currently accepts a surviving, unchanged JS-click target.
        self.assertEqual(self.fake.state, "slider")


if __name__ == "__main__":
    unittest.main()
