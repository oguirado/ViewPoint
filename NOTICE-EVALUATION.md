# Notice: licensing of this repository and its components

Everything in this repository is licensed under the
[GNU Affero General Public License, version 3 only](LICENSE) (SPDX: `AGPL-3.0-only`),
unless a file or directory carries its own licence notice.
Copyright (c) 2026 oguirado (https://github.com/oguirado).

## Components

| Path | Licence | Notes |
|---|---|---|
| MetaViewer core (`core/`, `daemon-adapter/`, `contracts/`, `agents/`, `ui/`, `tools/`, ...) | `AGPL-3.0-only` | Pre-existing project, see [docs/PREEXISTING.md](docs/PREEXISTING.md) |
| `components/vision-engine/` | `AGPL-3.0-only` ([its LICENSE](components/vision-engine/LICENSE), [NOTICE](components/vision-engine/NOTICE)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `components/wender-agent/` | `AGPL-3.0-only` ([its LICENSE](components/wender-agent/LICENSE), [NOTICE](components/wender-agent/NOTICE)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `components/OpenSuperficies/` | `AGPL-3.0-only` ([its LICENSE](components/OpenSuperficies/LICENSE), [third-party notices](components/OpenSuperficies/THIRD_PARTY_NOTICES.md)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `tools/vis-skill/` | `AGPL-3.0-only` ([its LICENSE](tools/vis-skill/LICENSE)) | The maintainer's vision CLI, vendored; see [PROVENANCE.md](PROVENANCE.md) |
| `hackathon/` | `AGPL-3.0-only` | Hackathon layer, see [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md) |
| `site/` | `AGPL-3.0-only` | Project site, see [site/PROVENANCE.md](site/PROVENANCE.md); web fonts are loaded from Google Fonts at view time and are not redistributed here; the GemDroid piece is an unofficial fan concept |
| `capsule-ui-kit` | `AGPL-3.0-only` | Separate public repository, pinned in `ui/kit.lock.json` |

Third-party code and assets inside these trees keep their own licences, listed in the
notice files above and in [docs/ARM64_CAPSULE_ASSET_NOTICES.md](docs/ARM64_CAPSULE_ASSET_NOTICES.md).

## Earlier licences (superseded)

- MetaViewer releases up to and including v0.2.4 remain under the licence they were
  published with, PolyForm Strict 1.0.0; MetaViewer moved to `AGPL-3.0-only` in its
  CHANGELOG entry for 0.2.6.
- Earlier MetaViewer documentation described `wender-agent`, `vision-engine` and
  `OpenSuperficies` as separate private programs with an evaluation-only permission. That
  no longer applies: their source is published here under `AGPL-3.0-only`.
- In this repository, commits before `79a69b5` (release `v0.3.0-hackathon`) were published
  under PolyForm Strict 1.0.0. Some hackathon records written at that time still say
  "PolyForm Strict 1.0.0": `hackathon/swarm/docs/ONE_PAGER_IDEA.md`,
  `hackathon/swarm/docs/ONE_PAGER_TEAM.md`, `hackathon/PROVENANCE.md` (Licence line),
  `hackathon/docs/PUBLIC_AUDIT.md` and the first licence paragraph of
  `hackathon/README.md`. They are kept as published in release `v0.3.0-hackathon`: the two
  ONE_PAGER files are Gemini output of Track H unit H6 (`hackathon/PROVENANCE.md`) and
  `hackathon/swarm/docs/test_docs.py` checks their text. [LICENSE](LICENSE) and this notice
  supersede them.

This notice is a statement by the licensor, not legal advice.
