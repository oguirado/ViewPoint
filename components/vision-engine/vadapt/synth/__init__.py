"""vadapt.synth - deterministic synthetic frame sequences and px mutants (SPEC_ARCH §5.2)."""
from .synthetic import SynthAdapter, SynthFrame, SynthSource, SCENARIOS, render_scenario  # noqa: F401
from .px_mutants import apply_op  # noqa: F401
