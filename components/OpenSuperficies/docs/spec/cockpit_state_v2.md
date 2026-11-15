# Cockpit state v2: main-window capsule wall

The producer publishes `os.cu.cockpit-state/v2` through `EstadoCanonico`, HTTP `/state`, Copy slot and the literal DOM `cockpit-state` mirror. The schema identity is `urn:os-cu:os.cu.cockpit-state/v2`. The v1 schema file remains byte-identical history.

## Differences from v1

| Area | v1 | v2 |
|---|---|---|
| Identity | `os.cu.cockpit-state/v1` | `os.cu.cockpit-state/v2` |
| Main-window layouts | Runtime accepted single or dual layouts | Also accepts `2x2`; four capsules [TARGET] share the main window |
| Leaf JSON | `type`, `slot_id` | Adds required integer `scale_q16` |
| Minimum leaf | Width 1264, height 512 CSS pixels [CITED: v1 producer] | Width 600, height 420 CSS pixels [TARGET], independent of framebuffer dimensions |
| Framebuffer | Legacy canvas dimensions drove iframe dimensions | Fixed 1280×900 pixels [TARGET] inside canonical v2 operator views |
| Scale | Implicit presentation | `min(65536, floor(w*65536/1280), floor(h*65536/900))` [TARGET]; scale is Q16 aspect fit, never a new AI input coordinate system |
| Wall geometry | Unavailable | Canvas 1264×1024; row-major leaves 632×512, origins (0,0), (632,0), (0,512), (632,512) CSS pixels [TARGET] |
| Workspace capacity | Runtime packed at most two [CITED: v1 builder] | Complete groups of four [TARGET] form walls when capacity permits; remainders retain existing single/dual layouts |
| DOM origin | Card index adds a card-bar offset | Wall leaves share the workspace origin; no per-leaf card-bar offset. Existing single/dual mapping remains |
| Layout menu | Wall unavailable | Wall enabled when the cockpit contains enough capsules; global capacity and authority semantics remain |

The historical v1 JSON schema already listed `2x2` in its enum, although the producer and validators rejected it. The v2 schema retains that enum and makes the runtime behavior real. It also updates rectangle constraints and descriptions; the new leaf field is mandatory on every v2 leaf, including single/dual layouts.

The split tree uses half-ratio Q16 splits: a vertical row split, then horizontal column splits. A maximum of four capsules per workspace and the existing bounded tree depth remain [TARGET]. The wall works in every window mode, including the main single-tile window. Existing single and dual workspace rectangles remain compatible.

The iframe keeps the same loopback URL and transport protections. The parent card applies presentation scaling without changing the capsule framebuffer. Existing per-slot input custody remains; this introduces no agent input endpoint. AI observation continues through exact framebuffer capture separately. Recording and its RAM/operator-save policy are outside this document's changes.

Layout capacity remains cockpit-wide. Changing it reflows affected workspaces. Operator requests retain the operator gate; agent requests authenticate their credential and validate every affected held capsule, including capsules in other workspaces. A wall request with insufficient capsules fails before capacity changes. The menu observes the resulting global wall even when reflow renumbers workspaces.

## Consumer integration and verification

Visual QA must read the version before validating, require `scale_q16` on v2 leaves, validate rectangle/tree/capsule consistency, and treat scale as presentation data. HTTP authorization, canonical ordering, sequence rules, null telemetry, recording metadata, ports, leases, and byte-for-byte DOM mirroring retain their contracts.

`aiops/state.ts` is outside WALL's authorized write surface. The required consumer update is supplied as `build/room_WALL/state-v2.patch`; without it, the live shell rejects v2 at `readState`. The patch preserves historical v1 validation and adds strict v2 canvas/leaf/scale checks. The new shell-reader test detects this missing integration; the patched scratch copy passes.

Failing-first logs: `build/room_WALL/failing-go.log`, `failing-vitest.log`, and `v2-unpatched.log`. Go tests verify wall packing, remainder overflow, scale mismatch refusal, and fourth-slot lease protection. Node SSR tests verify wall geometry, framebuffer dimensions, literal v2 mirroring, and menu legality. Browser/display and live rendering measurements remain [H] for the conductor.

This mandate is scoped to the supplied cockpit corpus and operator wall ruling.

The layout guard also disambiguates equal per-slot epochs by authenticating the supplied capability against candidate slots. Selecting the last matching holder/epoch would reject a valid credential when several slots share that epoch. Unit cases cover a holder owning the complete wall and a peer holding the last slot.

The main-window layout menu forwards the selected credential's lease epoch for agent requests; operator requests retain the canonical document sequence. These are distinct authorities and may have different numeric values.
