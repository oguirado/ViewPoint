#!/usr/bin/env python3
# FASE 0 instrument: regenerates SELLO-FASE-0-NEXTGEN.json after an operator-approved amendment.
# usage (repository root): python3 -B scripts/ng/sellar-fase0.py --motivo "<English reason>" [--plan-version X.Y] [--turno N]
# It refreshes every listed artifact's .sha256 sidecar first, recomputes the checkpoint and the plan checker,
# bumps "revision", records the superseded seal hash, and writes the seal plus its sidecar.
import argparse, datetime, hashlib, json, os, subprocess, sys

R = "/home/user/OpenSuperficies"
PLAN = "PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md"


def plan_version():
    import re
    m = re.search(r"· v(\d+\.\d+) -->", open(os.path.join(R, PLAN), encoding="utf-8").readline())
    return m.group(1) if m else None


S = "/home/user/.openfresnel/plaza/sala-opensuperficies-nextgen"
ARTEFACTOS = [
    ("PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md", "plan v{v}", True, True),
    ("ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md", "frontend spec v1.1", True, True),
    ("RELEVO-METAORQUESTACION-PLAZA-NEXTGEN.md", "handover v1.1", True, True),
    ("PROTOCOLO-PLAZA-VIVO-NEXTGEN.md", "live protocol", True, True),
    ("AUDITORIA-PREPARACION-NEXTGEN.md", "FASE 0 audit record", True, True),
    ("adaptador/ADAPTADOR-ENTORNO-CAMPANA.json", "adapter v2.1.0", True, True),
    ("contexto/PAQUETE-ENRIQUECIMIENTO-CATALOGOS-OPENSUPERFICIES-V2.md", "pack:enrich v2", True, True),
    ("contexto/PAQUETE_ENRIQUECIMIENTO_CATALOGOS_OPENSUPERFICIES.md", "pack:enrich v1 (superseded, retained)", True, False),
    ("contexto/LINEA-BASE-V1.sha256", "frozen v1 set manifest", False, True),
    ("go.mod", "module skeleton", False, True),
    ("scripts/ng/campana.sh", "campaign instrument", False, True),
    ("scripts/ng/verificar-plan.py", "plan checker", False, True),
    ("scripts/ng/sellar-fase0.py", "seal instrument", False, True),
    ("scripts/ng/prevuelo-oraculos.py", "oracle pre-flight instrument (A-23)", False, True),
    ("scripts/ng/obrero.sh", "worker dispatch instrument (A-28, A-31)", False, True),
    ("scripts/ng/display-alcance.sh", "campaign-scoped display invariant instrument (A-35, DEC-20)", False, True),
    ("scripts/ng/investigar.sh", "external research instrument (A-38)", False, True),
    ("scripts/ng/dopar.sh", "context doping pipeline (A-61, INV-19)", False, True),
    ("contexto/ANTIPATRONES.md", "measured anti-pattern list injected into every doping package (A-61)", True, True),
    ("contexto/prevuelo/ola-1.stubs.json", "wave-1 pre-flight stubs", False, True),
    ("PAQUETE_CONTEXTO_DESTILADO_OPENSUPERFICIES_NEXT_GEN.md", "pack:surface", False, False),
    ("INVESTIGACION_MICROVM_ARQUITECTURA_PERPLEXITY.md", "pack:microvm", True, False),
    ("PROMPT_SOBERANO_CLAUDE_OPUS_ESTRATEGIA_CAMPANA_OPENSUPERFICIES_V2.xml", "strategy mandate", False, False),
    ("PROMPT_SOBERANO_CLAUDE_OPUS_AUDITORIA_Y_SELLADO_FINAL.xml", "audit mandate", False, False),
    ("PROMPT_PERPLEXITY_IMAGEN_INVITADO_MICROVM.xml", "optional research prompt", True, True),
    ("DOSSIER-CIERRE-OPENSUPERFICIES-V1.md", "v1 closure dossier", False, False),
    ("INVENTARIO-ARTEFACTOS.json", "v1 inventory", False, False),
    ("historico/2026-09-16-plan-v3.0/MANIFIESTO.sha256", "archived prior versions", False, True),
    ("historico/2026-09-17-plan-v3.1/MANIFIESTO.sha256", "archived plan v3.1 (superseded by v3.2, BLQ-NG-01)", False, True),
    ("historico/2026-09-17-plan-v3.2/MANIFIESTO.sha256", "archived plan v3.2 (superseded by v3.3, A-20 BLQ-NG-04)", False, True),
    ("historico/2026-09-17-plan-v3.3/MANIFIESTO.sha256", "archived plan v3.3, protocol v1.0 and pre-v3.4 system prompts", False, True),
    ("historico/2026-09-17-plan-v3.4/MANIFIESTO.sha256", "archived plan v3.4 and protocol v1.1", False, True),
    ("historico/2026-09-17-plan-v3.5/MANIFIESTO.sha256", "archived plan v3.5", False, True),
    (S + "/prompts/PROMPT_SISTEMA_METAORQUESTADOR_OPUS5.xml", "MO system prompt", True, True),
    (S + "/prompts/PROMPT_SISTEMA_FABRICA_ANTIGRAVITY2.xml", "FO system prompt", True, True),
    (S + "/prompts/ARRANQUE-METAORQUESTADOR-V35.txt", "MO launch prompt (v3.5, points at PROTOCOLO section 9)", True, True),
    (S + "/prompts/ARRANQUE-ANTIGRAVITY2-INFORMADOR-V35.txt", "Antigravity 2 progress-reporter launch prompt (v3.5, A-34)", True, True),
    (S + "/prompts/RELEVO-METAORQUESTADOR-V38.txt", "MO handover launch prompt (v3.8)", True, True),
    ("/home/user/.openfresnel/plaza/apendar.py", "Plaza append transport", False, False),
    ("/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "sealed architecture", False, False),
    ("/home/user/Documentos/newton-shell/docs/plantillas/PLANTILLA-PLAN-CAMPANA.v3.en.md", "plan template", False, False),
    ("/home/user/Documentos/newton-shell/docs/plantillas/GUIA-PREPLAN-CAMPANA.v1.en.md", "pre-plan bridge guide", False, False),
    ("/home/user/Documentos/newton-shell/docs/plantillas/Workflows_Guia/GUIA_WORKFLOW_METAORQUESTACION_OPUS_FABLE_ANTIGRAVITY.md", "workflow doctrine", False, False),
    ("/home/user/.local/bin/newton-shell", "Newton binary (environment pin; a mismatch requires an operator-approved re-pin)", False, False),
]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def tok(p):
    r = subprocess.run(["newton-verbo", "tok"], stdin=open(p, "rb"), capture_output=True, text=True)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--motivo", required=True)
    ap.add_argument("--plan-version", default=None, help="default: read from the plan header")
    ap.add_argument("--turno", type=int, default=None, help="turno_siguiente; default: campana.sh estado")
    ap.add_argument("--preguntas", default=None, help="JSON list replacing preguntas_abiertas")
    a = ap.parse_args()
    os.chdir(R)
    a.plan_version = a.plan_version or plan_version()
    if not a.plan_version:
        print(json.dumps({"ok": False, "rc": 1, "causa": "plan_version_ilegible"})); return 1
    campana = os.path.join(R, "scripts/ng/campana.sh")
    for p, _, _, refrescar in ARTEFACTOS:
        if refrescar and subprocess.run([campana, "sidecar", p], capture_output=True).returncode != 0:
            print(json.dumps({"ok": False, "rc": 1, "causa": "sidecar", "ruta": p})); return 1
    previo = json.load(open("SELLO-FASE-0-NEXTGEN.json")) if os.path.exists("SELLO-FASE-0-NEXTGEN.json") else None
    ck = json.loads(subprocess.run([campana, "checkpoint"], capture_output=True, text=True).stdout)
    vp_run = subprocess.run([os.path.join(R, "scripts/ng/verificar-plan.py"), "PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md"], capture_output=True, text=True)
    vp = json.loads(vp_run.stdout)
    if vp_run.returncode != 0:
        print(json.dumps({"ok": False, "rc": 1, "causa": "plan_checker", "fallos": vp["fallos"][:5]})); return 1
    turno = a.turno if a.turno is not None else json.loads(subprocess.run([campana, "estado"], capture_output=True, text=True).stdout)["siguiente_turno"]
    arts = []
    for p, rol, t, _ in ARTEFACTOS:
        e = {"ruta": p, "rol": rol.format(v=a.plan_version), "sha256": sha(p), "bytes": os.path.getsize(p)}
        if t:
            e["tok_o200k"] = tok(p)
        arts.append(e)
    sello = {
        "schema": "newton-sello-fase0/v2",
        "campana": "opensuperficies-nextgen",
        "fase": "FASE 0 · COLD AUDIT AND SEAL",
        "revision": (previo or {}).get("revision", 1) + 1 if previo else 1,
        "supersede": {"sha256": sha("SELLO-FASE-0-NEXTGEN.json")} if previo else None,
        "motivo": a.motivo,
        "emitido": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "emisor": "opus5-estratega",
        "veredicto": "RATIFIED_FOR_WAVE0_AFTER_CORRECTION",
        "plan": {"version": a.plan_version, "unidades": vp["unidades"], "olas": vp["olas"], "k_max": vp["k_max"],
                 "artefactos_ssa": vp["artefactos"], "gates": 37, "verificador_fallos": 0, "techo_del_plan": 33950},
        "checkpoint_fase0": ck,
        "entorno_medido": (previo or {}).get("entorno_medido", {}) | {"newton_sha256": sha("/home/user/.local/bin/newton-shell")},
        "turno_siguiente": turno,
        "preguntas_abiertas": json.loads(a.preguntas) if a.preguntas else (previo or {}).get("preguntas_abiertas", []),
        "artefactos": arts,
    }
    with open("SELLO-FASE-0-NEXTGEN.json", "w", encoding="utf-8") as fh:
        json.dump(sello, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    subprocess.run([campana, "sidecar", "SELLO-FASE-0-NEXTGEN.json"], capture_output=True, check=True)
    print(json.dumps({"ok": True, "revision": sello["revision"], "sha256": sha("SELLO-FASE-0-NEXTGEN.json"),
                      "artefactos": len(arts), "checkpoint": ck["checkpoint"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
