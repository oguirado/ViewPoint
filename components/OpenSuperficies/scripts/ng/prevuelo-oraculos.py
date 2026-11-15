#!/usr/bin/env python3
# Plan v3.4 A-23 (V-3) instrument: oracle pre-flight for one wave, run by the strategist before the MO emits orders.
# usage (repository root): python3 -B scripts/ng/prevuelo-oraculos.py --ola N [--stubs contexto/prevuelo/ola-N.stubs.json] [--salida F]
# Static lint (every unit of the wave, oracle and contrast, as run by `bash -o pipefail -c`):
#   L1 bash syntax · L2 `| grep -q` fed by a producer (SIGPIPE → 141) · L3 `| grep -c … |` (rc 1 when the count is 0)
#   L8 `! a | b` (a SIGPIPE turns into a pass) · R1 every `reads` path exists or is written by a dependency
#   W1 every `tee` target lies inside the unit's `writes`
# Lines inside a single-quoted `sh -c '…'` run without pipefail and are exempt from L2/L3/L8.
# Shape run (with --stubs): each unit runs in a scratch tree where `go` is a shim and the unit's binaries are stubs:
#   positive stubs ⇒ oracle rc 0 and contrast rc ≠ 0; negative stubs ⇒ oracle rc ≠ 0. Each case also runs with
#   large stub output (200k lines) to expose pipe races. rc 0 = every check green.
import argparse, json, os, re, shutil, subprocess, sys, tempfile
import yaml

R = "/home/user/OpenSuperficies"
PLAN = "PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md"
GRANDE = "seq 1 200000 | sed 's/^/l/'; "


def bloques(t):
    out = {}
    for b in re.findall(r'```yaml\n(id:.*?)```', t, re.S):
        d = yaml.safe_load(b)
        out[d["id"]] = d
    return out


def sin_sh_c(s):
    return re.sub(r"sh -c '[^']*'", "sh -c ''", s)


def lint(u, d, deps, prod):
    F = []
    for campo in ("oracle", "contrast"):
        s = d[campo]
        n = subprocess.run(["bash", "-n", "-c", s], capture_output=True, text=True)
        if n.returncode:
            F.append({"u": u, "campo": campo, "regla": "L1", "detalle": n.stderr.strip()[:200]})
        x = sin_sh_c(s)
        for m in re.finditer(r'\|\s*grep\s+-[A-Za-z]*q[A-Za-z]*', x):
            previo = x[:m.start()].rstrip()
            productor = re.split(r'&&|\|\||;', previo)[-1].strip()
            # a single-line producer (stat, file, printf of one value) cannot outlive grep
            if not re.match(r'^(stat|file|printf|echo)\b', productor):
                F.append({"u": u, "campo": campo, "regla": "L2", "detalle": x[max(0, m.start() - 60):m.end() + 20]})
        for m in re.finditer(r'\|\s*grep\s+-[A-Za-z]*c[A-Za-z]*[^|&;]*\|([^&;]*)', x):
            h = {"u": u, "campo": campo, "regla": "L3", "detalle": m.group(0)[:120]}
            # a zero count is the success case ⇒ grep rc 1 ⇒ false failure; otherwise a zero count is a failure anyway
            if not re.search(r'test\s+0\s+-eq|-eq\s+0', m.group(1)):
                h["nota"] = "fails only when the count is 0, which is itself a failure here"
            F.append(h)
        for m in re.finditer(r'!\s+[^&|;]*\|', x):
            F.append({"u": u, "campo": campo, "regla": "L8", "detalle": x[m.start():m.start() + 100]})
    for r in d["reads"]:
        if os.path.exists(os.path.join(R, r.rstrip("/"))):
            continue
        dueños = {v for w, us in prod.items() for v in us if w == r or (w.endswith("/") and r.startswith(w))}
        if not dueños & deps:
            F.append({"u": u, "campo": "reads", "regla": "R1", "detalle": r})
    for m in re.finditer(r'tee\s+([^\s|&;]+)', d["oracle"] + " " + d["contrast"]):
        p = m.group(1)
        if not any(p == w or (w.endswith("/") and p.startswith(w)) for w in d["writes"]):
            F.append({"u": u, "campo": "oracle", "regla": "W1", "detalle": p})
    return F


def forma(u, d, stubs):
    res = []
    for caso in ("pos", "neg"):
        sc = stubs.get(caso)
        if not sc:
            continue
        for grande in (False, True):
            raiz = tempfile.mkdtemp(prefix=f"prevuelo-{u}-")
            try:
                for w in d["writes"]:
                    if w.endswith("/"):
                        os.makedirs(os.path.join(raiz, w), exist_ok=True)
                os.makedirs(os.path.join(raiz, "bin"), exist_ok=True)
                os.makedirs(os.path.join(raiz, ".shim"), exist_ok=True)
                for ruta, cuerpo in sc.get("ficheros", {}).items():
                    p = os.path.join(raiz, ruta)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w") as fh:
                        fh.write("#!/bin/bash\n" + (GRANDE.replace("; ", " >/dev/null; ") if grande else "") + cuerpo + "\n")
                    os.chmod(p, 0o755)
                golist = sc.get("go_list", "true")
                with open(os.path.join(raiz, ".shim/go"), "w") as fh:
                    fh.write("#!/bin/bash\ncase \"$1\" in\n test) case \"$*\" in *mutante*) exit 1;; esac; exit 0;;\n build) exit 0;;\n"
                             " list) " + (GRANDE if grande else "") + golist + ";;\nesac\n")
                os.chmod(os.path.join(raiz, ".shim/go"), 0o755)
                env = dict(os.environ, PATH=os.path.join(raiz, ".shim") + ":" + os.environ["PATH"])
                o = subprocess.run(["bash", "-o", "pipefail", "-c", d["oracle"]], cwd=raiz, env=env, capture_output=True, timeout=120).returncode
                c = subprocess.run(["bash", "-o", "pipefail", "-c", d["contrast"]], cwd=raiz, env=env, capture_output=True, timeout=120).returncode
            finally:
                shutil.rmtree(raiz, ignore_errors=True)
            ok = (o == 0 and c != 0) if caso == "pos" else (o != 0)
            res.append({"u": u, "caso": caso, "salida_grande": grande, "oraculo_rc": o, "contraste_rc": c, "ok": ok})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ola", type=int, required=True)
    ap.add_argument("--stubs", default=None)
    ap.add_argument("--salida", default=None)
    a = ap.parse_args()
    os.chdir(R)
    t = open(PLAN, encoding="utf-8").read()
    B = bloques(t)
    olas = {json.loads(l)["ola"]: json.loads(l)["unidades"] for l in re.findall(r'^\{"ola":.*\}$', t, re.M)}
    deps = {json.loads(l)["u"]: json.loads(l)["deps"] for l in re.findall(r'^\{"u":"U-\d+","deps":.*\}$', t, re.M)}
    prod = {}
    for u, d in B.items():
        for w in d["writes"]:
            prod.setdefault(w, set()).add(u)

    def clos(u, s=None):
        s = set() if s is None else s
        for v in deps[u]:
            if v not in s:
                s.add(v)
                clos(v, s)
        return s

    unidades = olas[a.ola]
    stubs = json.load(open(a.stubs)) if a.stubs else {}
    hallazgos, formas = [], []
    for u in unidades:
        hallazgos += lint(u, B[u], clos(u), prod)
        if u in stubs:
            formas += forma(u, B[u], stubs[u])
    bloqueantes = [h for h in hallazgos if h["regla"] != "L3" or "nota" not in h]
    sin_forma = [u for u in unidades if u not in stubs]
    ok = not bloqueantes and all(f["ok"] for f in formas)
    out = {"ok": ok, "ola": a.ola, "unidades": unidades, "plan_sha256": subprocess.run(["sha256sum", PLAN], capture_output=True, text=True).stdout.split()[0],
           "hallazgos": hallazgos, "forma": formas, "sin_forma": sin_forma}
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    if a.salida:
        with open(a.salida, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
    print(txt)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
