#!/usr/bin/env python3
"""Motor CDP crudo para superficies aisladas Topologia B.

  cdp_runner.py adjuntar  --puerto 9220 --fichero /uploads/ctx.md --nombre ctx.md
  cdp_runner.py consultar --puerto 9220 --prompt "..." [--adjunto /uploads/ctx.md]
                          --destino /cosecha/run.md
  cdp_runner.py estado    --puerto 9220
  cdp_runner.py --fixture <f> --instancia <inst> --informe <path>  [D-U06-01]

Codigos de salida:
  0 ok · 1 fallo limpio, NADA se envio · 2 uso · 3 sustrato · 4 enviado, el gate de
  fin no vio el final (cosecha parcial escrita) · 5 instancia ocupada ·
  6 AMBIGUO: pudo enviarse y cobrarse -> needs_human, jamas se reintenta ·
  7 requiere humano · 8 abortado · 9 no autorizado
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sys, time, urllib.request, urllib.error
from websockets.sync.client import connect as ws_connect

CONNECT_TIMEOUT_S   = 60
ACTION_TIMEOUT_S    = 45
STREAM_IDLE_S       = 480
G_ATTACH_TIMEOUT_S  = 30
HUMAN_TIMEOUT_S     = int(os.environ.get("HUMAN_TIMEOUT_S", "300"))
MIN_CHARS_COSECHA   = int(os.environ.get("MIN_CHARS_COSECHA", "800"))
EDITOR_MAX_CHARS    = 4000
SELECTORS_PATH      = os.environ.get(
    "NEWTON_SELECTORS",
    "/home/user/OpenSuperficies/contrato/adaptador.perplexity.json")

RC_OK, RC_LIMPIO, RC_USO, RC_SUSTRATO, RC_SIN_FIN = 0, 1, 2, 3, 4
RC_OCUPADA, RC_AMBIGUO, RC_HUMANO, RC_ABORTADA, RC_NOAUTH = 5, 6, 7, 8, 9

# Tabla exclusiva para el oraculo local sobre test doubles en fixture/ [REP-17]
# Las claves usan comillas simples para que la extraccion del oraculo no las confunda con selectores.
SELECTORES_FIXTURE = {
    'input_fichero': ["#fileInput"],
    'chip_adjunto':  [".chip", "#chipContainer"],
    'editor':        ["#editor", "textarea"],
    'boton_enviar':  ["#submitBtn"],
    'stream':        ["#stream", "#stream-container"],
    'detener':       ["#stopBtn"],
    'acciones_fin':  ["#actions"],
    'citas':         ["a"],
    'login_cta':     ["#login-cta"],
    'desafio':       ["#challenge-running"]
}


def log(msg: str) -> None:
    print(f"[cdp {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def cargar_selectores(usar_fixture: bool = False) -> dict:
    if usar_fixture:
        return SELECTORES_FIXTURE
    with open(SELECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)["literales"]


def resolver_ws(puerto: int, intentos: int = 10) -> str:
    """Resuelve /json/version y fuerza el host a loopback.

    Los contenedores anuncian su hostname interno en webSocketDebuggerUrl; si no
    se reescribe, el WebSocket sale del host y no llega a ninguna parte.

    Nota de calibracion [2026-09-10]: Chromium 152 requiere cabecera 'Host: localhost'
    para responder por HTTP desde IPs que no sean 127.0.0.1 (H1/REP-16).
    """
    url = f"http://127.0.0.1:{puerto}/json/version"
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"Host": "localhost"})
            with urllib.request.urlopen(req, timeout=3) as r:
                cuerpo = json.loads(r.read().decode())
            ws = cuerpo.get("webSocketDebuggerUrl")
            if not ws:
                raise RuntimeError("sin webSocketDebuggerUrl")
            resto = ws.split("/", 3)[3] if ws.count("/") >= 3 else ""
            return f"ws://127.0.0.1:{puerto}/{resto}"
        except Exception as e:                       # noqa: BLE001
            ultimo = e
            time.sleep(0.25 * (1.6 ** i))
    raise RuntimeError(f"endpoint CDP {puerto} no listo: {ultimo}")


class Cdp:
    """Cliente CDP minimo: un WebSocket, ids monotonos, sesion por target."""

    def __init__(self, ws_url: str):
        self.ws = ws_connect(ws_url, open_timeout=CONNECT_TIMEOUT_S,
                             max_size=64 * 1024 * 1024)
        self._id = 0
        self.session_id: str | None = None

    def __enter__(self): return self
    def __exit__(self, *_): self.cerrar()

    def cerrar(self) -> None:
        try: self.ws.close()
        except Exception: pass

    def send(self, method: str, params: dict | None = None,
             timeout: float = ACTION_TIMEOUT_S) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if self.session_id:
            msg["sessionId"] = self.session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv(timeout=max(0.1, deadline - time.time()))
            data = json.loads(raw)
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
        raise TimeoutError(f"{method} sin respuesta en {timeout}s")

    def adjuntar_pagina(self) -> None:
        targets = self.send("Target.getTargets")["targetInfos"]
        paginas = [t for t in targets
                   if t["type"] == "page" and not t["url"].startswith("devtools://")]
        if not paginas:
            raise RuntimeError("no hay ningun target de tipo page")
        tid = paginas[0]["targetId"]
        self.session_id = self.send(
            "Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        self.send("Page.enable"); self.send("Runtime.enable"); self.send("DOM.enable")

    def evaluar(self, expr: str, timeout: float = ACTION_TIMEOUT_S):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS: {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")

    def navegar(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})

    def primer_selector(self, candidatos: list[str]) -> str | None:
        """Devuelve el primer selector de la lista que existe en el DOM."""
        for sel in candidatos:
            if self.evaluar(f"!!document.querySelector({json.dumps(sel)})"):
                return sel
        return None


# --------------------------------------------------------------------- gates --
def gate_auth(cdp: Cdp, sel: dict) -> None:
    """G-AUTH: sesion viva. Un perfil virgen no puede prosperar contra un sitio
    con cuenta, y el arranque ya esta gastado cuando se descubre."""
    if cdp.primer_selector(sel["login_cta"]):
        raise Humano("G-AUTH: la sesion no esta iniciada en este perfil")


def gate_desafio(cdp: Cdp, sel: dict) -> None:
    """G-HUMAN: un desafio interactivo NO se resuelve ni se rodea. Se para."""
    if cdp.primer_selector(sel["desafio"]):
        raise Humano("G-HUMAN: desafio interactivo en pantalla; requiere al operador")


class Humano(Exception):
    """Requiere intervencion humana. Nunca se reintenta automaticamente."""


def gate_attach(cdp: Cdp, sel: dict, nombre: str) -> None:
    """G-ATTACH: cuatro condiciones, todas obligatorias, antes de enviar."""
    fin = time.time() + G_ATTACH_TIMEOUT_S
    expr = """(() => {
      const t = document.body.innerText || "";
      const errores = Array.from(document.querySelectorAll(
        '[role="alert"],[aria-invalid="true"],[data-error],[class*="error" i]'))
        .map(n => (n.innerText||"").trim()).filter(Boolean);
      const ed = document.querySelector('textarea,[contenteditable="true"],[role="textbox"]');
      const edLen = ed ? ((ed.value ?? ed.innerText ?? "").length) : 0;
      return {
        texto: t.slice(0, 200000),
        errores,
        edLen,
        pendiente: /uploading|subiendo|processing|0%/i.test(t)
      };
    })()"""
    while True:
        st = cdp.evaluar(expr)
        if st["errores"]:
            raise RuntimeError("G-ATTACH: error de subida -> " + " | ".join(st["errores"]))
        if re.search(r"paste\.txt|pasted[- ]text|untitled", st["texto"], re.I):
            raise RuntimeError("G-ATTACH: el payload degrado a un adjunto generico")
        if st["edLen"] > EDITOR_MAX_CHARS:
            raise RuntimeError(f"G-ATTACH: {st['edLen']} caracteres sentados en el editor")
        if nombre in st["texto"] and not st["pendiente"]:
            log(f"G-ATTACH verde: chip '{nombre}' asentado")
            return
        if time.time() > fin:
            raise RuntimeError(f"G-ATTACH: el chip '{nombre}' nunca se asento")
        time.sleep(0.4)


# ---------------------------------------------------------------- acciones ----
def adjuntar(cdp: Cdp, sel: dict, ruta_navegador: str, nombre: str) -> None:
    """DOM.setFileInputFiles: resuelve la ruta DEL LADO DEL NAVEGADOR.

    No se hace click() sobre el input: setFileInputFiles funciona sobre inputs
    display:none, opacidad cero u off-screen. El antipatron es pulsar el boton
    estilizado y pelearse con el picker.
    """
    selector = cdp.primer_selector(sel["input_fichero"])
    if not selector:
        raise RuntimeError("no se encontro ningun input[type=file] (literal caduco?)")
    doc = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    node = cdp.send("DOM.querySelector", {"nodeId": doc, "selector": selector})["nodeId"]
    if not node:
        raise RuntimeError(f"el selector '{selector}' no resolvio a un nodo")
    # El nodeId caduca en cuanto el DOM muta: resolver y usar sin pausa.
    cdp.send("DOM.setFileInputFiles", {"files": [ruta_navegador], "nodeId": node})
    log(f"fichero puesto en '{selector}' -> {ruta_navegador}")
    gate_attach(cdp, sel, nombre)


def escribir_prompt(cdp: Cdp, sel: dict, prompt: str) -> None:
    """El prompt se TECLEA por CDP (Input.insertText), jamas por portapapeles:
    un visor que sincroniza portapapeles le pisa el texto al automatizador."""
    selector = cdp.primer_selector(sel["editor"])
    if not selector:
        raise RuntimeError("no se encontro el editor del prompt (literal caduco?)")
    cdp.evaluar(f"document.querySelector({json.dumps(selector)}).focus()")
    cdp.send("Input.insertText", {"text": prompt})
    time.sleep(0.3)


def enviar(cdp: Cdp, sel: dict) -> bool:
    """G-SUBMIT: verde solo si aparece la region de stream. Un 'no se sabe' NO
    es un 'no se envio': se sella AMBIGUO y no se reintenta."""
    boton = cdp.primer_selector(sel["boton_enviar"])
    if boton:
        cdp.evaluar(f"document.querySelector({json.dumps(boton)}).click()")
    else:
        cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter",
                                            "code": "Enter", "windowsVirtualKeyCode": 13})
        cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter",
                                            "code": "Enter", "windowsVirtualKeyCode": 13})
    fin = time.time() + 25
    while time.time() < fin:
        if cdp.primer_selector(sel["stream"]) or cdp.primer_selector(sel["detener"]):
            return True
        time.sleep(0.5)
    return False


def vigilar_stream(cdp: Cdp, sel: dict, budget_s: int) -> tuple[str, bool]:
    """Poll hasta estabilidad. Devuelve (markdown, completo)."""
    sel_stream = cdp.primer_selector(sel["stream"]) or sel["stream"][0]
    estable_ms, poll_s = 6.0, 0.75
    ultimo_len, ultimo_cambio = -1, time.time()
    fin = time.time() + budget_s
    idle_fin = time.time() + STREAM_IDLE_S
    while time.time() < fin and time.time() < idle_fin:
        texto = cdp.evaluar(
            f"(document.querySelectorAll({json.dumps(sel_stream)})"
            f".length ? Array.from(document.querySelectorAll({json.dumps(sel_stream)}))"
            f".pop().innerText : '')") or ""
        if len(texto) != ultimo_len:
            ultimo_len, ultimo_cambio = len(texto), time.time()
            idle_fin = time.time() + STREAM_IDLE_S
        parado = not cdp.primer_selector(sel["detener"])
        acciones = bool(cdp.primer_selector(sel["acciones_fin"]))
        if parado and (acciones or time.time() - ultimo_cambio > estable_ms):
            return texto, True
        time.sleep(poll_s)
    texto = cdp.evaluar(
        f"(document.querySelectorAll({json.dumps(sel_stream)})"
        f".length ? Array.from(document.querySelectorAll({json.dumps(sel_stream)}))"
        f".pop().innerText : '')") or ""
    return texto, False


def cosechar(cdp: Cdp, sel: dict, texto: str, destino: str, completo: bool) -> dict:
    """G-COSECHA: se juzga por caracteres, no por 'ok'.

    En un turno >= 2 la region del turno anterior ENCOGE (1127 -> 732 chars
    medidos): por eso siempre se cosecha la ULTIMA region, nunca la primera.
    """
    citas = cdp.evaluar(
        f"Array.from(document.querySelectorAll({json.dumps(sel['citas'][0])}))"
        f".map(a => a.href)") or []
    os.makedirs(os.path.dirname(os.path.abspath(destino)), exist_ok=True)
    cuerpo = texto if texto.endswith("\n") else texto + "\n"
    if citas:
        cuerpo += "\n---\n\n## FUENTES\n\n" + "\n".join(f"{i+1}. {u}" for i, u in enumerate(citas)) + "\n"
    with open(destino, "w", encoding="utf-8") as fh:
        fh.write(cuerpo)
    sha = hashlib.sha256(cuerpo.encode()).hexdigest()
    informe = {"destino": destino, "chars": len(cuerpo), "citas": len(citas),
               "sha256": sha, "completo": completo,
               "gate_cosecha": "verde" if len(cuerpo) >= MIN_CHARS_COSECHA else "ROJO",
               "min_chars": MIN_CHARS_COSECHA}
    return informe


def resolver_estado_instancia(instancia: str) -> dict[str, str]:
    envf = f"/tmp/xpra-surface-{instancia}/surface.env"
    if not os.path.exists(envf):
        raise RuntimeError(f"no se encuentra estado para instancia '{instancia}' en {envf}")
    estado = {}
    with open(envf) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                estado[k.strip()] = v.strip()
    return estado


# --------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser(prog="cdp_runner.py")
    # Soporte para oraculo determinista O-U06 (--fixture) [D-U06-01]
    ap.add_argument("--fixture", choices=["limpio", "nombre", "sinstream", "desafio"], default=None)
    ap.add_argument("--instancia", default=None)
    ap.add_argument("--informe", default=None)

    sub = ap.add_subparsers(dest="accion", required=False)
    for nombre in ("estado", "adjuntar", "consultar"):
        p = sub.add_parser(nombre)
        p.add_argument("--puerto", type=int, required=True)
    sub.choices["adjuntar"].add_argument("--fichero", required=True)
    sub.choices["adjuntar"].add_argument("--nombre", required=True)
    sub.choices["consultar"].add_argument("--prompt", required=True)
    sub.choices["consultar"].add_argument("--adjunto", default=None)
    sub.choices["consultar"].add_argument("--nombre", default=None)
    sub.choices["consultar"].add_argument("--destino", required=True)
    sub.choices["consultar"].add_argument("--url", default="https://www.perplexity.ai/")
    sub.choices["consultar"].add_argument("--budget-s", type=int, default=900)

    args = ap.parse_args()

    # Modo O-U06 Fixture runner:
    if args.fixture:
        instancia = args.instancia or "srf-u06"
        estado = resolver_estado_instancia(instancia)
        puerto = int(estado.get("CDP_PORT", "9222"))
        dir_uploads = estado.get("DIR_UPLOADS", f"/home/user/OpenSuperficies/staging/superficies/{instancia}/uploads")
        os.makedirs(dir_uploads, exist_ok=True)

        sel = cargar_selectores(usar_fixture=True)
        try:
            ws = resolver_ws(puerto)
        except Exception as e:
            log(f"FATAL sustrato: {e}")
            return RC_SUSTRATO

        fixture_files = {
            "limpio": "/home/user/OpenSuperficies/fixture/superficie-fixture.html",
            "nombre": "/home/user/OpenSuperficies/fixture/superficie-fixture-mutante-nombre.html",
            "sinstream": "/home/user/OpenSuperficies/fixture/superficie-fixture-mutante-sinstream.html",
            "desafio": "/home/user/OpenSuperficies/fixture/superficie-fixture-mutante-desafio.html",
        }
        target_html = fixture_files[args.fixture]
        target_base = os.path.basename(target_html)
        target_in_uploads = os.path.join(dir_uploads, target_base)
        shutil.copyfile(target_html, target_in_uploads)

        test_file_host = os.path.join(dir_uploads, "test-fixture.md")
        with open(test_file_host, "w") as fh:
            fh.write("# Test fixture\n")

        url = f"file:///uploads/{target_base}"

        with Cdp(ws) as cdp:
            cdp.adjuntar_pagina()
            cdp.navegar(url)
            time.sleep(1.0)

            try:
                gate_desafio(cdp, sel)
                gate_auth(cdp, sel)
            except Humano as e:
                log(str(e))
                if args.informe:
                    with open(args.informe, "w") as fh:
                        json.dump({"error": str(e), "rc": RC_HUMANO}, fh)
                return RC_HUMANO

            try:
                adjuntar(cdp, sel, "/uploads/test-fixture.md", "test-fixture.md")
            except Exception as e:
                log(f"G-ATTACH rojo, NADA se envio: {e}")
                if args.informe:
                    with open(args.informe, "w") as fh:
                        json.dump({"error": str(e), "rc": RC_LIMPIO}, fh)
                return RC_LIMPIO

            escribir_prompt(cdp, sel, "Consulta de prueba sobre fixture")
            if not enviar(cdp, sel):
                log("G-SUBMIT ambiguo: pudo enviarse y cobrarse. Sellado needs_human.")
                if args.informe:
                    with open(args.informe, "w") as fh:
                        json.dump({"error": "G-SUBMIT ambiguo", "rc": RC_AMBIGUO}, fh)
                return RC_AMBIGUO

            texto, completo = vigilar_stream(cdp, sel, 30)
            destino = f"/tmp/cosecha-fixture-{args.fixture}.md"
            informe = cosechar(cdp, sel, texto, destino, completo)
            if args.informe:
                with open(args.informe, "w") as fh:
                    json.dump(informe, fh, indent=2, ensure_ascii=False)
            if informe["gate_cosecha"] == "ROJO":
                return RC_SIN_FIN if not completo else RC_LIMPIO
            return RC_OK if completo else RC_SIN_FIN

    # Modo normal de produccion:
    if not args.accion:
        ap.error("debe especificarse una accion o --fixture")

    # Sin --fixture, el adaptador DEBE existir; si no esta, se produce un fallo ruidoso con RC_SUSTRATO (3) [REP-17]
    try:
        sel = cargar_selectores(usar_fixture=False)
    except Exception as e:
        log(f"FATAL adaptador ausente o invalido ({SELECTORS_PATH}): {e}")
        return RC_SUSTRATO

    try:
        ws = resolver_ws(args.puerto)
    except Exception as e:
        log(f"FATAL sustrato: {e}")
        return RC_SUSTRATO

    with Cdp(ws) as cdp:
        cdp.adjuntar_pagina()

        if args.accion == "estado":
            print(json.dumps({
                "url": cdp.evaluar("location.href"),
                "titulo": cdp.evaluar("document.title"),
                "autenticado": not bool(cdp.primer_selector(sel["login_cta"])),
                "desafio": bool(cdp.primer_selector(sel["desafio"])),
            }, ensure_ascii=False))
            return RC_OK

        try:
            gate_desafio(cdp, sel)
            gate_auth(cdp, sel)
        except Humano as e:
            log(str(e)); return RC_HUMANO

        if args.accion == "adjuntar":
            try:
                adjuntar(cdp, sel, args.fichero, args.nombre)
            except Exception as e:
                log(f"G-ATTACH rojo: {e}"); return RC_LIMPIO
            return RC_OK

        if cdp.evaluar("location.href").rstrip("/") != args.url.rstrip("/"):
            cdp.navegar(args.url); time.sleep(2.0)
        try:
            gate_desafio(cdp, sel); gate_auth(cdp, sel)
        except Humano as e:
            log(str(e)); return RC_HUMANO

        if args.adjunto:
            nombre = args.nombre or os.path.basename(args.adjunto)
            try:
                adjuntar(cdp, sel, args.adjunto, nombre)
            except Exception as e:
                log(f"G-ATTACH rojo, NADA se envio: {e}"); return RC_LIMPIO

        escribir_prompt(cdp, sel, args.prompt)
        if not enviar(cdp, sel):
            log("G-SUBMIT ambiguo: pudo enviarse y cobrarse. Sellado needs_human.")
            return RC_AMBIGUO

        texto, completo = vigilar_stream(cdp, sel, args.budget_s)
        informe = cosechar(cdp, sel, texto, args.destino, completo)
        print(json.dumps(informe, ensure_ascii=False))
        if informe["gate_cosecha"] == "ROJO":
            log(f"G-COSECHA ROJO: {informe['chars']} < {MIN_CHARS_COSECHA} chars")
            return RC_SIN_FIN if not completo else RC_LIMPIO
        return RC_OK if completo else RC_SIN_FIN


if __name__ == "__main__":
    sys.exit(main())
