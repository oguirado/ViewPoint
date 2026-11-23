# Notice: licensing of this repository and its components

Everything in this repository is licensed under the
[GNU Affero General Public License, version 3 only](LICENSE) (SPDX: `AGPL-3.0-only`),
unless a file or directory carries its own licence notice. The [superseded historical notices](#earlier-licences-superseded) describe earlier publication terms.
Copyright (c) 2026 oguirado (https://github.com/oguirado).

## Components

| Path | Licence | Notes |
|---|---|---|
| MetaViewer core (`core/`, `daemon-adapter/`, `contracts/`, `agents/`, `ui/`, `tools/`, ...) | `AGPL-3.0-only` | Pre-existing project, see [docs/PREEXISTING.md](docs/PREEXISTING.md) |
| `components/vision-engine/` | `AGPL-3.0-only` ([its LICENSE](components/vision-engine/LICENSE), [NOTICE](components/vision-engine/NOTICE)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `components/wender-agent/` | `AGPL-3.0-only` ([its LICENSE](components/wender-agent/LICENSE), [NOTICE](components/wender-agent/NOTICE)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `components/OpenSuperficies/` | `AGPL-3.0-only` ([its LICENSE](components/OpenSuperficies/LICENSE), [third-party notices](components/OpenSuperficies/THIRD_PARTY_NOTICES.md)) | Vendored source, pinned in [deps.lock](deps.lock) |
| `components/blender-visual-qa/` | `AGPL-3.0-only` ([its LICENSE](components/blender-visual-qa/LICENSE)) | See [provenance](PROVENANCE.md#blender-visual-qa-import-15-nov-2026) for the maintainer-owned imports and later contributions. |
| `tools/vis-skill/` | `AGPL-3.0-only` ([its LICENSE](tools/vis-skill/LICENSE)) | The maintainer's vision CLI, vendored; see [PROVENANCE.md](PROVENANCE.md) |
| `hackathon/` | `AGPL-3.0-only` | Hackathon layer, see [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md) |
| `site/` | `AGPL-3.0-only` for site code. GemDroid incorporates Android robot artwork under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). | Project site, see [site/PROVENANCE.md](site/PROVENANCE.md); web fonts are loaded from Google Fonts at view time and are not redistributed here; the GemDroid piece is an unofficial fan concept |
| `capsule-ui-kit` | `AGPL-3.0-only` | Separate public repository, pinned in `ui/kit.lock.json` |

Third-party code and assets inside these trees keep their own licences, listed in the
notice files above and in [docs/ARM64_CAPSULE_ASSET_NOTICES.md](docs/ARM64_CAPSULE_ASSET_NOTICES.md).

GemDroid is an unofficial fan concept. The Android robot is reproduced or modified from work created and shared by Google and used according to terms described in the [Creative Commons 3.0 Attribution License](https://creativecommons.org/licenses/by/3.0/). The attribution applies to the site and `docs/media/gemdroid-intro.gif`. See the [site credit and non-endorsement statement](site/PROVENANCE.md#third-party) and [Google's Android robot guidelines](https://developer.android.com/distribute/marketing-tools/brand-guidelines#android-robot).

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

## Blender Visual QA and its pinned Vision Engine wheel

The [Blender Visual QA component](components/blender-visual-qa/README.md) is
licensed under [AGPL-3.0-only](components/blender-visual-qa/LICENSE). The maintainer's licensing decision covers operator-owned imports from `godot-visual-qa@8575ca2` and `blender-visual-qa@b5c6d76`. Third-party material retains its own terms.
The [Visual QA guide](docs/VISUAL_QA_3D.md) and
[repository provenance](PROVENANCE.md) identify its imported code and source pins.

The shipped procedural Blender film and replay fixtures use original test geometry, integer motion and generated pixels under `AGPL-3.0-only`. The take 0448 film samples are from the maintainer-owned AstroScout scene.

The separate [Vision Engine wheel](components/blender-visual-qa/tools/wheels/vision_engine-1.1.1-py3-none-any.whl) uses `AGPL-3.0-only`. It includes the following embedded licence and notice.

| Wheel member | SHA-256 |
|---|---|
| `vision_engine-1.1.1.dist-info/LICENSE` | `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0` |
| `vision_engine-1.1.1.dist-info/NOTICE` | `caa4d0a424bea36ff4b9e33b007f2a6fc92af867582571f76622ff061a9cc81f` |

The [corresponding source](https://github.com/oguirado/ViewPoint/tree/c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7/components/vision-engine) is `components/vision-engine` in public ViewPoint commit `c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7`. The wheel SHA-256 is `4efce651bab7d04f04c70fdf3b40e360f74aa2e296d28bdb36aaf7d72bb05480`. The distribution version is `1.1.1` and the Vision API version is `1.1.0`.
The [build recipe](components/blender-visual-qa/tools/build_engine_wheel.py)
and [wheel pin](components/blender-visual-qa/config/vision.json) record the build
inputs and artifact digest. Preserve both embedded notices with the wheel.
