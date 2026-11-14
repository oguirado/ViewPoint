# vision-engine

Shared, deterministic, integer-only vision engine for visual QA of rendered frames (games, Blender, GUI agents).
Version 1.1.0 (wave W0: registries, gates, Tier-1 docs, catalogue).

Pipeline: `vadapt.*` -> `vqa.frame/v1` manifest -> S0 grid -> S1 describe -> S2 classify -> S3 pyramid -> S4 regions
-> S5 gop -> S6 pack -> judge -> S6 verify -> S7 `vmem` ingest.

Packages: `visioncore` (S0-S6, numpy + stdlib), `vmem` (visual memory, stdlib), `vadapt` (engine adapters).
Tooling in `tools/` is stdlib only. Navigation for agents: `llms.txt`, `AGENTS.md`, `catalogue/`.

Guarantees: identical frame bytes + params give byte-identical outputs; no float reaches a stored field.
Normative specification: the SPEC_INT / SPEC_ARCH / SPEC_CORE / SPEC_TIME / SPEC_VMEM chapters of campaign visual-qa-0 (not included in this repository; the repo carries their normative content as `visioncore/params.py`, `catalogue/` and `standards/schemas/`).

See `QUICKSTART.md` to run the gates and tests.

License: none declared yet (all rights reserved until the owner adds a LICENSE file).

## License

AGPL-3.0-only. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
