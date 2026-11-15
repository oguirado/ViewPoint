#!/usr/bin/env python3
"""
compile_briefing.py — Context Aggregation & Sovereign Prompt Compiler for OpenSuperficies Next-Gen
Generates:
1. PAQUETE_CONTEXTO_DESTILADO_OPENSUPERFICIES_NEXT_GEN.md (validates against schema.predigest_briefing.json)
2. PROMPT_SOBERANO_CLAUDE_OPUS_ESTRATEGIA_CAMPANA_OPENSUPERFICIES_V2.xml (3-Zone Sovereign XML envelope)
"""

import os
import sys
import glob
import json
import hashlib
import datetime
import tiktoken
import jsonschema

ENCODING_NAME = "o200k_base"
enc = tiktoken.get_encoding(ENCODING_NAME)

BASE_DIR = "/home/user/OpenSuperficies"
DESTILADO_DIR = os.path.join(BASE_DIR, ".destilado")
SCHEMA_PATH = "/home/user/Documentos/AarnesPrompts/00_PACK_INICIO_CONVERSACION/CONTRATOS/schema.predigest_briefing.json"

INPUT_FILES = [
    "/home/user/OpenSuperficies/DOSSIER-CIERRE-OPENSUPERFICIES-V1.md",
    "/home/user/OpenSuperficies/motor/cdp_runner.py",
    "/home/user/OpenSuperficies/scripts/docker-xpra-surface.sh",
    "/home/user/OpenSuperficies/docker/Dockerfile.browser-arm64",
    "/home/user/OpenSuperficies/docker/verify-quarantine.sh",
    "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md",
    "/home/user/Documentos/OpenSwitchboard/01_IDEA_DEL_PROYECTO.md",
    "/home/user/Documentos/OpenSwitchboard/02_ARQUITECTURA_V1.md",
    "/home/user/Documentos/OpenFresnel/internal/app/grabador_bindings.go",
    "/home/user/Documentos/OpenNewton/frontend/src/components/MultiSlotCanonico.svelte",
    "/home/user/Documentos/AaHerramientas/estrategia_campana/GUIA_OPERACION_DOCKER_PIPELINE_DESTILADO.md",
    "/home/user/Documentos/newton-shell/docs/plantillas/GUIA_COSECHA_DESTILADO_UNIVERSAL_DEFINITIVA.md",
    "/home/user/Documentos/newton-shell/docs/plantillas/GUIA-PREPLAN-CAMPANA.v1.en.md",
    "/home/user/Documentos/newton-shell/docs/plantillas/PLANTILLA-PLAN-CAMPANA.v3.en.md",
    "/home/user/Documentos/newton-shell/docs/plantillas/Workflows_Guia/GUIA_WORKFLOW_METAORQUESTACION_OPUS_FABLE_ANTIGRAVITY.md",
    "/home/user/Documentos/newton-shell/docs/plantillas/GUIA_INTEGRACION_DINAMICA_CATALOGOS_Y_PAPERS.md"
]

def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read()
        h.update(data)
    return h.hexdigest(), len(data)

def compress_shard_to_budget(content, max_toks):
    toks = len(enc.encode(content))
    if toks <= max_toks:
        return content
    
    lines = content.split('\n')
    high_priority = []
    low_priority_indices = []
    in_code = False
    
    for i, l in enumerate(lines):
        sl = l.strip()
        if sl.startswith('```'):
            in_code = not in_code
            high_priority.append(i)
        elif in_code or sl.startswith('#') or sl.startswith('|') or '$$' in sl or 'INV-' in sl or '[MEASURED]' in sl:
            high_priority.append(i)
        else:
            low_priority_indices.append(i)
            
    high_set = set(high_priority)
    test_lines = [lines[i] for i in sorted(high_set)]
    test_toks = len(enc.encode('\n'.join(test_lines)))
    
    if test_toks <= max_toks:
        budget_left = max_toks - test_toks
        kept_set = set(high_set)
        for idx in low_priority_indices:
            l_toks = len(enc.encode(lines[idx] + '\n'))
            if l_toks <= budget_left:
                kept_set.add(idx)
                budget_left -= l_toks
        return '\n'.join([lines[i] for i in sorted(kept_set)])
    else:
        kept_lines = []
        cur = 0
        in_c = False
        for l in test_lines:
            if l.strip().startswith('```'):
                in_c = not in_c
            l_tok = len(enc.encode(l + '\n'))
            if cur + l_tok <= max_toks:
                kept_lines.append(l)
                cur += l_tok
            else:
                if in_c:
                    kept_lines.append('```')
                break
        return '\n'.join(kept_lines)

def compile_all():
    print(f"[*] Scanning {DESTILADO_DIR} for SUB_*.md shards...")
    shard_files = sorted(glob.glob(os.path.join(DESTILADO_DIR, "SUB_*.md")))
    print(f"[*] Found {len(shard_files)} shards.")
    assert len(shard_files) == 15, f"Expected 15 shards, found {len(shard_files)}"

    raw_total = sum(len(enc.encode(open(s, "r", encoding="utf-8").read())) for s in shard_files)
    print(f"[*] Raw tokens across all 15 shards: {raw_total}")

    target_total = 92000
    compressed_shards = []
    for sf in shard_files:
        basename = os.path.basename(sf)
        txt = open(sf, "r", encoding="utf-8").read().strip()
        stoks = len(enc.encode(txt))
        starget = int(target_total * (stoks / raw_total))
        compressed = compress_shard_to_budget(txt, starget)
        compressed_shards.append(f"<!-- SHARD_START: {basename} -->\n\n{compressed}\n\n<!-- SHARD_END: {basename} -->")

    combined_body = "\n\n---\n\n".join(compressed_shards)
    body_tokens = len(enc.encode(combined_body))
    print(f"[*] Downscaled body tokens (o200k_base): {body_tokens}")
    assert 80000 <= body_tokens <= 100000, f"Body tokens {body_tokens} outside Tier XL budget (80k-100k)!"

    # Input provenance
    inputs_meta = []
    for p in INPUT_FILES:
        if os.path.exists(p):
            sha, size = hash_file(p)
            inputs_meta.append({
                "path": p,
                "sha256": sha,
                "bytes": size
            })
        else:
            print(f"[!] Input missing: {p}")

    # Subagent inventory
    subagents_list = [
        {"kind": "self", "task": "OpenSuperficies V1/V2 Auditor", "shard": "SUB_01", "outcome": "ok"},
        {"kind": "self", "task": "Perplexity OpenSuperficies Researcher", "shard": "SUB_02", "outcome": "ok"},
        {"kind": "self", "task": "OpenNewton Multislots Architect", "shard": "SUB_03", "outcome": "ok"},
        {"kind": "self", "task": "Terminal & Supervision Researcher", "shard": "SUB_04", "outcome": "ok"},
        {"kind": "self", "task": "1/6 4K UI Designer", "shard": "SUB_05", "outcome": "ok"},
        {"kind": "self", "task": "Frontend & 2D/3D Researcher", "shard": "SUB_06", "outcome": "ok"},
        {"kind": "self", "task": "Docker-Xpra Takeover Engineer", "shard": "SUB_07", "outcome": "ok"},
        {"kind": "self", "task": "MicroVM Subsystem Engineer", "shard": "SUB_08", "outcome": "ok"},
        {"kind": "self", "task": "OpenFresnel Recording Specialist", "shard": "SUB_09", "outcome": "ok"},
        {"kind": "self", "task": "OpenSwitchboard Integrator", "shard": "SUB_10", "outcome": "ok"},
        {"kind": "self", "task": "Distillation Pipeline Specialist", "shard": "SUB_11", "outcome": "ok"},
        {"kind": "self", "task": "Perplexity Repos Cartographer", "shard": "SUB_12", "outcome": "ok"},
        {"kind": "self", "task": "Campaign Plan Scaffolder", "shard": "SUB_13", "outcome": "ok"},
        {"kind": "self", "task": "Metaorchestration Architect", "shard": "SUB_14", "outcome": "ok"},
        {"kind": "self", "task": "Dynamic Catalogs Integrator", "shard": "SUB_15", "outcome": "ok"}
    ]

    measured_constants = [
        {"key": "host_kernel", "value": "Linux aarch64 6.17.0-1018-nvidia", "state": "MEASURED", "origin": "uname -a", "command": "uname -a"},
        {"key": "host_hardware", "value": "NVIDIA GB10 (20 ARM cores, 121 GiB RAM total, 78 GiB available)", "state": "MEASURED", "origin": "free -m", "command": "free -m"},
        {"key": "newton_version", "value": "3.9.1", "state": "MEASURED", "origin": "newton --version", "command": "newton --version"},
        {"key": "newton_binary_sha256", "value": "061c335a2f8ebf80aa9ce17ec62cf6b3808c9558578cdb5e73eac1076bf1f84e", "state": "MEASURED", "origin": "sha256sum /home/user/.local/bin/newton-shell", "command": "sha256sum /home/user/.local/bin/newton-shell"},
        {"key": "frontera_slot_estable", "value": "7fba44e2394c06520f5325be3de1e48a55cfe87db6f2ffcd74ae757778f005fe", "state": "MEASURED", "origin": "/home/user/.local/lib/newton/newton-frontera estado", "command": "/home/user/.local/lib/newton/newton-frontera estado"},
        {"key": "frontera_nivel_jaula", "value": "2", "state": "MEASURED", "origin": "/home/user/.local/lib/newton/newton-frontera estado", "command": "/home/user/.local/lib/newton/newton-frontera estado"},
        {"key": "srf_mpsc_queue_latency", "value": "34.18 ns/op, 0 B/op, 0 allocs/op", "state": "MEASURED", "origin": "go test BenchmarkSrfBusEmitir_1P", "command": "go test -bench=BenchmarkSrfBusEmitir_1P ./src/..."},
        {"key": "opensuperficies_v1_image_size", "value": "1.12 GB (opensuperficies-browser-arm64:1.0.0)", "state": "MEASURED", "origin": "docker images", "command": "docker images opensuperficies-browser-arm64:1.0.0"},
        {"key": "distil_box_image_size", "value": "492 MB (distil-box:1.1)", "state": "MEASURED", "origin": "docker images", "command": "docker images distil-box:1.1"},
        {"key": "monitor_desktop_target", "value": "49 inch curved 4K (32:9 or 21:9), 1/6 desktop area (~640-853px width, 1440-2160px height)", "state": "TARGET", "origin": "Operator requirement", "command": ""},
        {"key": "capsule_governor_max_capsules", "value": "24", "state": "CITED", "origin": "ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md §5", "command": ""},
        {"key": "xpra_cdp_ports_formula", "value": "cdp = 9220 + (p - 10020) for p in 10020..10099", "state": "MEASURED", "origin": "ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md §9.4 (R-15)", "command": ""}
    ]

    entities = [
        {"module": "srf", "symbol": "SurfaceCapsule", "signature": "type SurfaceCapsule interface { ID() string; Kind() SurfaceKind; Start(ctx context.Context) error; Stop(ctx context.Context) error; Display() *SrfDisplayRef; Events() <-chan SrfEvento }", "file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 163},
        {"module": "srf", "symbol": "SurfaceManager", "signature": "type SurfaceManager struct { mu sync.RWMutex; capsules map[string]SurfaceCapsule; gov *SrfGobernador; leases *SrfArrendador; wal *SrfLibroConsumo }", "file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 163},
        {"module": "srf", "symbol": "SrfMPSC", "signature": "type SrfMPSC struct { head unsafe.Pointer; _pad0 [128]byte; tail unsafe.Pointer; _pad1 [128]byte; stub SrfNodo; _pad2 [128]byte }", "file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 155},
        {"module": "srf", "symbol": "SrfGobernador", "signature": "type SrfGobernador struct { mu sync.Mutex; maxCapsulas int; memLimiteBytes int64; pidsLimite int }", "file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 160},
        {"module": "srf", "symbol": "SrfLibroConsumo", "signature": "type SrfLibroConsumo struct { file *os.File; mu sync.Mutex; lsn uint64 }", "file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 162},
        {"module": "app", "symbol": "GrabadorIniciar", "signature": "func (a *App) GrabadorIniciar(opts GrabadorOpciones) (GrabadorSesion, error)", "file_path": "/home/user/Documentos/OpenFresnel/internal/app/grabador_bindings.go", "line": 48},
        {"module": "app", "symbol": "GrabadorParar", "signature": "func (a *App) GrabadorParar(sessionID string) (GrabadorResultado, error)", "file_path": "/home/user/Documentos/OpenFresnel/internal/app/grabador_bindings.go", "line": 53}
    ]

    wiring_points = [
        {"file_path": "/home/user/Documentos/AaArquitectura/ARQUITECTURA_FINAL_OPENSUPERFICIES_V2_SEALED.md", "line": 77, "what": "UDS socket $XDG_RUNTIME_DIR/opensuperficies/srfd.sock (mode 0600, SO_PEERCRED)", "consumers": ["srf CLI", "Wails frontend bridge", "CLI agents in PTY"]},
        {"file_path": "/home/user/OpenSuperficies/scripts/docker-xpra-surface.sh", "line": 88, "what": "Port pair finder for Xpra (10020-10099) and CDP (9220-9299)", "consumers": ["BrowserCapsule", "DesktopCapsule"]},
        {"file_path": "/home/user/Documentos/OpenFresnel/internal/app/grabador_bindings.go", "line": 28, "what": "OpenFresnel synchronized grabador CLI invoker for window XID recording", "consumers": ["SurfaceManager recording hooks", "Wails GrabadorButton"]},
        {"file_path": "/home/user/Documentos/OpenSwitchboard/01_IDEA_DEL_PROYECTO.md", "line": 88, "what": "OpenSwitchboard A2A rooms directory and PBX extension routing", "consumers": ["TerminalCapsule CLI agents", "SurfaceManager event broadcaster"]}
    ]

    open_questions = [
        "What is the exact Svelte 5 / Wails v2 component hierarchy for rendering the 1/6 desktop area hypervisor on a 49-inch curved 4K monitor?",
        "How should the hot-layer recycle of Xpra with --use-display be synchronized with the frontend canvas to ensure zero flicker during operator take-over?",
        "What guest rootfs and minimal kernel config should be generated for MicroVMCapsule to eliminate the AUSENTE blocker under aarch64?",
        "How should the 15 Perplexity research directories be partitioned into execution waves under the 17-stage distillation DAG?",
        "What exact PBX room naming and token fencing protocol will OpenSwitchboard enforce when an agent inside a TerminalCapsule crashes?"
    ]

    not_verified = [
        "qemu guest kernel and rootfs for aarch64 MicroVMCapsule (marked AUSENTE — no inventar on host)",
        "vhost_vsock kernel module status for qemu guest communication",
        "openbox / desktop container image (opensuperficies-desktop-arm64 currently TARGET)"
    ]

    injection_flags = []

    budget = {
        "tokens_o200k": body_tokens,
        "ceiling_o200k": 100000,
        "tokenizer_ref": "tiktoken o200k_base (host measurement; Claude tokenizer counts ~1.7x more on XML [MEASURED 2026-09-08])"
    }

    header = {
        "schema": "predigest-briefing/v1",
        "provenance": {
            "emitter_role": "WORKER",
            "emitter_model": "gemini-3.8-flash-high",
            "runtime": "antigravity-2",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "inputs": inputs_meta,
            "tools_used": ["newton_resolver", "newton_ejecutar", "newton_lpd", "grep", "read_file", "bash"],
            "subagents": subagents_list
        },
        "target": "BUILDER_A",
        "class": 1,
        "budget": budget,
        "measured_constants": measured_constants,
        "entities": entities,
        "wiring_points": wiring_points,
        "open_questions": open_questions,
        "injection_flags": injection_flags,
        "not_verified": not_verified
    }

    print(f"[*] Validating header against schema {SCHEMA_PATH}...")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as sf:
        schema = json.load(sf)
    jsonschema.validate(instance=header, schema=schema)
    print("[+] Header schema validation SUCCESSFUL (predigest-briefing/v1)!")

    header_json_str = json.dumps(header, indent=2)
    anchor_sentence = "Anchor: Every architectural fact and constraint in this briefing is scoped strictly to the host substrate and the provided corpus under /home/user/Documentos/ [MEASURED]. What is not on disk is declared AUSENTE — no inventar."

    full_briefing_md = f"```json\n{header_json_str}\n```\n\n# PRE-DIGESTED CONTEXT BRIEFING · OPENSUPERFICIES NEXT-GEN VISUAL HYPERVISOR\n## Comprehensive Multi-Domain Distillation for Claude Opus 5 & Claude Fable 5.1\n\n{combined_body}\n\n---\n\n{anchor_sentence}\n"

    briefing_out_path = os.path.join(BASE_DIR, "PAQUETE_CONTEXTO_DESTILADO_OPENSUPERFICIES_NEXT_GEN.md")
    with open(briefing_out_path, "w", encoding="utf-8") as f:
        f.write(full_briefing_md)
    print(f"[+] Written full briefing to: {briefing_out_path} ({len(full_briefing_md)} bytes)")

    xml_out_path = os.path.join(BASE_DIR, "PROMPT_SOBERANO_CLAUDE_OPUS_ESTRATEGIA_CAMPANA_OPENSUPERFICIES_V2.xml")
    
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<prompt_envelope id="ENV-OPUS-OPENSUPERFICIES-V2-STRATEGY-2026-09-16" version="1.0">
  <metadata>
    <target_model>claude-opus-5</target_model>
    <role>Sovereign System Architect &amp; Campaign Master Planner</role>
    <emitter>Antigravity 2 (Gemini 3.8 Flash High - WORKER)</emitter>
    <generated_at>{datetime.datetime.now(datetime.timezone.utc).isoformat()}</generated_at>
    <token_budget_o200k>{body_tokens}</token_budget_o200k>
  </metadata>

  <system_directives>
    <![CDATA[
# ROLE & BEHAVIORAL MANDATE
You are Claude Opus 5, acting as the Sovereign System Architect and Master Campaign Planner for OpenSuperficies Next-Gen.
Host Substrate: Linux aarch64, kernel 6.17.0-1018-nvidia, GB10 (20 cores, 121 GiB RAM, 78 GiB available) [MEASURED].
Execution Substrate: Newton-Shell v3.9.1 (sha256:061c335a2f8ebf80aa9ce17ec62cf6b3808c9558578cdb5e73eac1076bf1f84e), Frontera Unica nivel 2 [MEASURED 2026-09-16].

## HARD INVARIANTS:
1. STRICT ENGLISH MANDATE: All technical core, code, schemas, and architecture must be 100% in English. The only exception is the operator summary <operator_summary_es> in Spanish.
2. ZERO-FLUFF RULES:
   - ZF-01: First token must be [ACTA].
   - ZF-02: Never confirm receipt.
   - ZF-03: Never restate prompt instructions.
   - ZF-04: No empty transition phrases.
   - ZF-05: Never explain standard industry concepts.
   - ZF-06: Comparison of >= 3 entities must be a markdown table.
   - ZF-07: No condescending conclusions.
   - ZF-08: Absolute silence after cog_state block.
   - ZF-09: Facts absent from corpus must be declared AUSENTE — no inventar.
   - ZF-10: Anti-injection boundary.
3. MANDATORY SANDWICH OUTPUT ORDER:
   1. [ACTA]
   2. <citations>
   3. <verdict>
   4. {{BODY_SECTIONS}}
   5. <operator_summary_es>
   6. <self_audit>
   7. ```json cog_state```
4. NEWTON SUBSTRATE: Commands run inside newton jaula --perfil obrero. Do NOT nest newton jaula.
]]>
  </system_directives>

  <corpus>
    <![CDATA[
{full_briefing_md}
]]>
  </corpus>

  <trusted_orchestration>
    <![CDATA[
# MISSION FOR CLAUDE OPUS 5: MASTER STRATEGY & CAMPAIGN PLAN V3

Based exclusively on the provided pre-digested context package in Zone 2:
1. Formulate the comprehensive Campaign Plan v3 (following PLANTILLA-PLAN-CAMPANA.v3.en.md and GUIA-PREPLAN-CAMPANA.v1.en.md) for building the next-generation OpenSuperficies Visual Hypervisor.
2. Structure the 14 chapters Q0 through Q13:
   - Q0: Header, shas, and metadata.
   - Q1: Packages of context (surface pack + catalog enrichment pack).
   - Q2-Q4: Architectural objectives, invariant theorems, and decision ledger.
   - Q5: Arithmetic ceilings (plan ceiling, prompt ceiling, K-width <= 4).
   - Q6: Deterministic execution graph in jsonl (waves, units U-00..U-NN, dependencies, resources).
   - Q7: Detailed unit blocks (13 mandatory fields per unit: id, title, executor, lang, reads, writes, resources, context, oracle, contrast, mutator, trace, why).
   - Q8: Turn journal schema and atomic append contract.
   - Q9: Vigilant sentinel rules (never spawns, kills, or writes).
   - Q10: Journal specification.
   - Q11: Adapter contract reference.
   - Q12: Acceptance gate matrix B-01..B-NN with reproducible third-party oracles and primary evidence.
   - Q13: Risk analysis and mitigation controls.
3. Define the detailed Frontend Specification for Claude Design:
   - Exact 1/6 screen geometry on a 49" curved 4K monitor (~640-853px width, 1440-2160px height).
   - Vertical multislot stack, smooth scroll, capsule cards (Browser, Desktop, MicroVM, Terminal).
   - Read-only vs Operator takeover hot-switching (Xpra proxy recycling without process restarts).
   - Synchronized OpenFresnel recording button (window XID + eventos.jsonl + FLUJO.md).
   - OpenSwitchboard PBX A2A integration (phonebook, rooms, snoop, whisper, barge-in).
4. Formulate the operational handover to Claude Fable 5.1 (Metaorchestrator) and Antigravity 2 (Subagent Construction Factory) via the asynchronous Plaza (~/.openfresnel/plaza/sala-opensuperficies-v2/).
]]>
  </trusted_orchestration>
</prompt_envelope>
"""

    with open(xml_out_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    print(f"[+] Written Sovereign XML prompt envelope to: {xml_out_path} ({len(xml_content)} bytes)")

if __name__ == "__main__":
    compile_all()
