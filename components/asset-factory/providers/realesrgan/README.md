# Offline RealESRGAN

See `../birefnet/README.md` for the shared CLI, receipt and resource boundary.
This provider remains `runnable:false`; licence texts and runtime are AUSENTE.
`build/recipe.json` pins the local checkpoint without copying it into the repo.

The CPU worker uses the installed BasicSR RRDBNet with the x4plus architecture
and restricted tensor-only checkpoint loading. RGB is processed by the model;
source alpha is expanded with nearest-neighbour sampling and authenticated by
the output gate. There is no resize fallback for RGB and no alpha hallucination.
The RealESRGAN convenience loader is bypassed; BasicSR supplies the architecture.

Upscale output is inspected by the input gate but is never presented as a
BiRefNet preparation receipt. Recommended chaining is upscale then matting;
each receipt binds its own immediate input. New masks need their own calibrated
input-policy evaluation. Independent SR quality and edge calibration are AUSENTE,
so `quality_verdict` remains CANNOT_SEE even for an OK execution stage.
