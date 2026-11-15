#!/usr/bin/env python3
# FASE 0 instrument: structural gates G0 G1 G4 G5 G6, exact Kahn levels and hidden-dependency closure for PLAN v3.x.
# usage: scripts/ng/verificar-plan.py PLAN.md   (rc 0 = all gates green; run from the repository root)
import re, json, sys, os, collections
import yaml
P = sys.argv[1]
t = open(P, encoding="utf-8").read()
olas = [json.loads(l) for l in re.findall(r'^\{"ola":.*\}$', t, re.M)]
deps = {json.loads(l)["u"]: json.loads(l)["deps"] for l in re.findall(r'^\{"u":"U-\d+","deps":.*\}$', t, re.M)}
recs = {json.loads(l)["u"]: set(json.loads(l)["rec"]) for l in re.findall(r'^\{"u":"U-\d+","rec":.*\}$', t, re.M)}
bloques = {}
for b in re.findall(r'```yaml\n(id:.*?)```', t, re.S):
    d = yaml.safe_load(b); bloques[d["id"]] = d
REQ = ["id","title","executor","lang","reads","writes","resources","context","oracle","contrast","mutator","trace","why"]
F = []
units = sorted(bloques)
# G0
for u,d in bloques.items():
    miss = [k for k in REQ if k not in d or d[k] in (None, "", [])]
    if miss: F.append(f"G0 {u} missing {miss}")
# level map & exact Kahn
lvl = {u:o["ola"] for o in olas for u in o["unidades"]}
if set(lvl) != set(bloques) or set(deps) != set(bloques) or set(recs) != set(bloques):
    F.append(f"SETS lvl={len(lvl)} deps={len(deps)} recs={len(recs)} blocks={len(bloques)}")
kahn = {}
def L(u):
    if u in kahn: return kahn[u]
    kahn[u] = 0 if not deps[u] else 1 + max(L(v) for v in deps[u]); return kahn[u]
for u in deps:
    if L(u) != lvl.get(u): F.append(f"KAHN {u} wave={lvl.get(u)} kahn={L(u)}")
# G4 / G5
for o in olas:
    if o["k"] != len(o["unidades"]) or o["k"] > 4: F.append(f"G4 {o}")
    us = o["unidades"]
    for i in range(len(us)):
        for j in range(i+1, len(us)):
            x = recs[us[i]] & recs[us[j]]
            if x: F.append(f"G5 {us[i]} {us[j]} {x}")
# G1 SSA
prod = collections.defaultdict(list)
for u,d in bloques.items():
    for w in d["writes"]: prod[w].append(u)
for w,us in prod.items():
    if len(us) > 1: F.append(f"G1 {w} {us}")
# prefix overlap (a dir write containing another unit's file write)
for a,ua in prod.items():
    if a.endswith("/"):
        for b,ub in prod.items():
            if b != a and b.startswith(a) and ub != ua: F.append(f"G1dir {a}{ua} contains {b}{ub}")
# G6
for u,d in bloques.items():
    if d["oracle"].strip() == d["contrast"].strip(): F.append(f"G6 {u}")
# closure of deps
def clos(u, s=None):
    s = s or set()
    for v in deps[u]:
        if v not in s: s.add(v); clos(v, s)
    return s
FASE0 = {"go.mod","scripts/ng/campana.sh","contexto/LINEA-BASE-V1.sha256","SELLO-FASE-0-NEXTGEN.json"}
def producers(path):
    out = set()
    for w,us in prod.items():
        pp = path.rstrip("/") + "/"
        if w == path or (w.endswith("/") and path.startswith(w)) or w.startswith(pp):
            out |= set(us)
    return out
def available(u, path):
    if path in FASE0: return True
    ps = producers(path)
    if ps & ({u} | clos(u)): return True
    if not ps and os.path.exists(path.rstrip("/")): return True
    return False
for u,d in bloques.items():
    for r in d["reads"]:
        if not available(u, r) and not (u in producers(r) and r.endswith('/')):
            F.append(f"READ {u} cannot see {r} (producers={sorted(producers(r))})")
    txt = d["oracle"] + " " + d["contrast"]
    built = set(re.findall(r'\./cmd/([a-z0-9-]+)', txt))
    if "./cmd/..." in txt:
        built |= {w.split('/')[1] for w in prod if w.startswith('cmd/')}
    for b in set(re.findall(r'(?<![\w/])bin/(srf[a-z0-9-]*)\b', txt)):
        if b not in built: F.append(f"BIN {u} uses bin/{b} but does not build it")
        if not available(u, f"cmd/{b}/main.go"): F.append(f"BIN {u} bin/{b} source not visible")
    for c in built:
        if not available(u, f"cmd/{c}/main.go"): F.append(f"CMD {u} builds ./cmd/{c} not visible")
    for p in set(re.findall(r'\b((?:scripts/ng|fixture(?:/ng)?|guest|cockpit|src|docker/ng)/[A-Za-z0-9_./-]+)', txt)):
        p = p.rstrip('.')
        if '/build/bin/' in p: continue
        if p.endswith('/...'): p = p[:-4] + '/'
        if not available(u, p): F.append(f"PATH {u} references {p} (producers={sorted(producers(p))})")
print(json.dumps({"unidades":len(bloques),"olas":len(olas),"artefactos":len(prod),"k_max":max(o["k"] for o in olas),"fallos":F}, indent=1, ensure_ascii=False))
sys.exit(1 if F else 0)
