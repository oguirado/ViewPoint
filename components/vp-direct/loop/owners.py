"""Provision isolated, pinned ReplayOwners without installing a default workflow."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import mkdtemp

from metaviewer_agents.mw_owner import Corpus, ReplayOwner
from recorder.store.memoryweb_store import canonical_bytes
from rewind.machine.transitions import require


class ProjectedOwners:
    """The service supplies the admitted owner and its reviewed provenance bytes.

    Each private run retains its input, selected receipt and owner journal for
    audit. Only the caller's explicit public source mapping enters the run
    corpus. The original corpus is read-only.
    """

    def __init__(self, parent, evidence_dir, public_sources):
        self.parent = parent
        self.directory = Path(evidence_dir).resolve(strict=True)
        self.sources = {}
        for ref, raw in public_sources.items():
            relative = Path(ref)
            require(not relative.is_absolute() and ".." not in relative.parts
                    and relative.suffix.lower() == ".md" and isinstance(raw, bytes), "MW_CONTAINMENT")
            self.sources[relative] = raw

    @contextmanager
    def __call__(self, document):
        parent = self.parent
        require(parent.corpus.intact() and parent.corpus.site["id"] == document["site_id"] and
                parent.corpus.workflow["id"] == document["id"] and
                not document["text_slots"], "SEAL_MISMATCH")
        root = Path(mkdtemp(dir=self.directory, prefix="mw-loop-"))
        site = root / "sites" / parent.corpus.site["id"]
        (site / "workflows").mkdir(parents=True)
        for relative, raw in self.sources.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (site / "site.json").write_bytes(canonical_bytes(parent.corpus.site))
        (site / "workflows" / (document["id"] + ".json")).write_bytes(canonical_bytes(document))
        corpus = Corpus(root, document["site_id"] + "/" + document["id"], parent.corpus.api)
        yield ReplayOwner(parent.backend, corpus, root / "run.json", mode=parent.mode,
                          notice=deepcopy(parent.notice), readback_ci=parent.readback_ci,
                          readback_runner=parent.readback_runner)
