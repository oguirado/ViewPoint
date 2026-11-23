# DRIVE provenance

The PR1 publication reconstruction implemented `blender_visual_qa/drive/png.py` and `blender_visual_qa/drive/launch.py` from the documented PNG RGB8 and owned-process launch contracts. It used clean-room implementations under `AGPL-3.0-only`.

The optional headless CUDA integration added worker and launch behavior, including the worker's own-thread naming permission. The staged repair received independent review. Campaign creation records bind the clean-room origins of `blender_visual_qa/drive/worker.py` (item_42, initial staged SHA-256 `b022955c3eb02eb28d20763a5ff7f50e31e5bb5bada76a41debfc261ee565650` [CITED]) and `blender_visual_qa/drive/containment.py` (item_34, corroborated by the chained process-isolation staging receipts).

The maintainer's licensing decision covers owned code from `blender-visual-qa@b5c6d76` and `godot-visual-qa@8575ca2` under `AGPL-3.0-only`. Third-party material retains its own terms.
