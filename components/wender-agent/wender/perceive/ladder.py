# @module {"id":"wa.perceive.ladder","layer":0,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.ladder - the read ladder: pick the cheapest rung that the surface offers.

Origin of the ladder: the OpenFresnel companions contract (CONTRATO-PROGRAMA-OPERABLE.md)
section 2 (lines 76-95). A vision model costs ~1 token per 1024 px, ~8100 tokens per 4K screen
[CITED, that contract, R12], so it is rung 4 of 5.
"""

# rung -> (name, served_by)
RUNGS = {
    1: ("structured", "app JSON-L / API state (e.g. Blender bpy, MCP)"),
    2: ("accessibility", "CDP Accessibility.getFullAXTree (web) or AT-SPI (desktop)"),
    3: ("pixel", "vision-engine: screen fingerprint + per-tile change/motion/flicker"),
    4: ("vision_model", "multimodal model on vision-engine-selected crops only"),
    5: ("blind_coordinates", "coordinates without semantic confirmation"),
}
CAPABILITY_FOR_RUNG = {1: "structured", 2: "accessibility", 3: "pixels", 4: "vision_model", 5: "pointer"}


# @anchor:wa.perceive.ladder.choose_rung
def choose_rung(capabilities):
    """Cheapest available rung for a surface, given a set of capability names."""
    caps = set(capabilities)
    for r in sorted(RUNGS):
        if CAPABILITY_FOR_RUNG[r] in caps:
            return r
    return None
