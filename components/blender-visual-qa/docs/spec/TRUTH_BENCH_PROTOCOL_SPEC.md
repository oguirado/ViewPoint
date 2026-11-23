# Blender truth-bench protocol specification

## Source boundary and notation

This specification describes the four bound files read through the GitHub connector at explicit ref `bridge/integration`. That ref resolved in this session to commit `95db85e51c3c33e468e9bc7fc5d9454828e3e481`. Every `path:line` citation below refers to that source snapshot of `bridge/integration`; the task report records the source-read evidence separately.

| Bound source | Scope |
|---|---|
| `bpy_side/bench_scenes.py` | Procedural scene builders, scene jitter, render defaults and scene manifest. `bpy_side/bench_scenes.py:33-82`, `bpy_side/bench_scenes.py:202-544` |
| `blender_visual_qa/truth/run_bench.py` | Runner arguments, seed expression, bounded captures, clean residuals and analysis orchestration. `blender_visual_qa/truth/run_bench.py:211-255`, `blender_visual_qa/truth/run_bench.py:311-381`, `blender_visual_qa/truth/run_bench.py:599-624`, `blender_visual_qa/truth/run_bench.py:787-1124` |
| `blender_visual_qa/truth/labels.py` | Truth inputs, pixel/tile rules, temporal annotations, flags and serialization. `blender_visual_qa/truth/labels.py:22-481` |
| `config/render_protocol_v1.json` | Literal GLB turntable protocol declaration. `config/render_protocol_v1.json:1-8` |

**ABSENT** means the specified behavior or value is not defined in these bound files, or is delegated to a dependency whose implementation is outside this source boundary. Source comments are identified as comments where they promise an outcome not established by the implementation. Numerical examples below are arithmetic substitutions into cited expressions; they are not render measurements.

In scene tables, `w,h` are requested resolution components and `n=c.nframes`. Locations and XY sizes are the builder's nominal pixel-domain arguments before optional scene jitter; `PX=1/32` converts them to Blender units. These construction units do not establish uniform projected pixel sizes for perspective B5. [`bpy_side/bench_scenes.py:25`, `bpy_side/bench_scenes.py:145-160`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:403-422`, `bpy_side/bench_scenes.py:476-481`]

## Shared scene construction

| Parameter | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Supported identifiers and one-based seed numbers | `B1=1, B1x=2, B2=3, B3=4, B4=5, B4f=6, B4n=7, B5=8, B5o=9, B6=10, B7=11, B8=12, BS=13`. Numbers follow the list order. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-52` |
| Python entry point | `build(bench,out,res=(640,360),nframes=8,samples=4,motion_blur=False,seed=None)`. Its direct-call frame default is eight for every bench. | `bpy_side/bench_scenes.py:476` |
| Scene-builder CLI | Resolution `640x360`, samples 4, motion blur false, no scene seed. Frame selection is `a.frames or DEFAULT_FRAMES.get(a.bench,8)`: B4n defaults to 32, all others to 8, and an explicit zero frame argument also selects that default. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:528-543` |
| Builder CLI validation | The seed is restricted to `0<=seed<2^31`; resolution is split on lowercased `x` and parsed as two integers. Positive-range validation of dimensions, frame count and samples is **ABSENT** here; runner validation is specified separately. The direct `build` function does not repeat the CLI seed-range check. | `bpy_side/bench_scenes.py:28`, `bpy_side/bench_scenes.py:50-54`, `bpy_side/bench_scenes.py:476-481`, `bpy_side/bench_scenes.py:528-543` |
| Reset | Factory settings with `use_empty=True`; use the resulting context scene. | `bpy_side/bench_scenes.py:119-121`, `bpy_side/bench_scenes.py:479-480` |
| Engine and saved sampling state | `CYCLES`, CPU, supplied samples, seed 0; `use_animated_seed`, `use_denoising` and `use_adaptive_sampling` false. | `bpy_side/bench_scenes.py:482-485` |
| Filter | `FILTER_WIDTH=0.01`, assigned to `cycles.filter_width` and included in the manifest. The source calls this box-like; an explicit pixel-filter-type assignment is **ABSENT**. | `bpy_side/bench_scenes.py:26`, `bpy_side/bench_scenes.py:482-486`, `bpy_side/bench_scenes.py:508` |
| Dimensions and frames | Render dimensions `w,h`, 100% resolution, frames `1..n`, fps 24. | `bpy_side/bench_scenes.py:481`, `bpy_side/bench_scenes.py:487-496` |
| Image settings | Supplied motion-blur value; fixed four threads; PNG, color depth `"8"`, dither 0.0, Standard view transform. Other view settings and a film-transparency assignment are **ABSENT** from this builder. | `bpy_side/bench_scenes.py:490-495` |
| World | New `World`, nodes disabled, color `(0,0,0)`. | `bpy_side/bench_scenes.py:497-500` |
| Initial camera | Location `(camera_dx*PX,camera_dy*PX,10)`, rotation `(0,0,0)`. Orthographic with `ortho_scale=w*PX` except B5, which starts perspective with lens 35. Camera clipping and sensor dimensions are **ABSENT** from the assignments. | `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:502-504` |
| Geometry helper | `add_box` creates a size-1 cube, or a size-1 plane when `plane=True`. Location components and specified size components are multiplied by PX; omitted Z location is 0 and omitted Z scale is 1.0. Object, mesh and material names are `name`, `name+"_mesh"`, and `"m_"+name`. | `bpy_side/bench_scenes.py:145-160` |
| Default material | Clear the node tree, create Emission named `Emission`, assign strength (default 1.0), and connect to Material Output. Without a checker, assign supplied RGB plus alpha 1 to Emission Color. | `bpy_side/bench_scenes.py:124-145` |
| Checker material | A truthy checker argument becomes Checker Texture Scale; Color1 is supplied RGB plus alpha 1 and Color2 is `(0.05,0.05,0.05,1)`; checker Color feeds Emission Color. Explicit checker Vector input wiring is **ABSENT**. | `bpy_side/bench_scenes.py:133-141` |
| Location keys | `key_loc` reapplies the recorded object offset, converts XY to Blender units, inserts both location component keys, and defaults to LINEAR interpolation. | `bpy_side/bench_scenes.py:85-87`, `bpy_side/bench_scenes.py:171-176` |
| Strength keys | `key_strength` keys the Emission Strength socket and defaults to CONSTANT interpolation. | `bpy_side/bench_scenes.py:179-183` |
| Interpolation scope | `set_interp` changes every keyframe of every existing F-curve in the supplied datablock action. Explicit extrapolation beyond endpoint keys is **ABSENT**; this matters for B1x and the freeze actors, and its broad scope changes B4f mover location interpolation. | `bpy_side/bench_scenes.py:163-183`, `bpy_side/bench_scenes.py:224-229`, `bpy_side/bench_scenes.py:298-303`, `bpy_side/bench_scenes.py:334-350` |
| Annotation state | `animated,temporal,coverage,cuts,lit` start empty and `seed_mode="fixed"`. Annotation helpers append integer frames and metadata; they do not insert Blender keys. | `bpy_side/bench_scenes.py:94-116` |
| Pass indices | Sort objects satisfying `o.type in RANK_TYPES` by name and assign indices from 1. Separately sort materials by name and assign indices from 1. The selection does not test `hide_render`. Membership of imported `RANK_TYPES` is **ABSENT** from these files. | `bpy_side/bench_scenes.py:20-21`, `bpy_side/bench_scenes.py:465-473` |
| Manifest | `bvqa.scene/v1`; bench, res, frame range, fps `[24,1]`, engine, seed mode, filter width, object/material name-index lists; animation sorted by `(frame,name,kind)`, temporal by `(start_frame,name)`, and sorted lit names. Nonempty coverage is sorted by `(start_frame,name)`; nonempty cuts by frame. A supplied scene seed adds `scene_seed` and `jitter`. | `bpy_side/bench_scenes.py:506-520` |
| Builder outputs | Save the supplied blend path, then write `<stem>.scene.json` with sorted keys and compact JSON separators. The builder itself contains no frame-render call. | `bpy_side/bench_scenes.py:476-525` |

## Scene seeding: SplitMix64 and scene_slot

A scene seed enables jitter exactly when `seed is not None`, including seed zero. Without a seed, camera/object offsets are zero and automatic placement/color jitter is skipped; the source calls this the golden scene. [`bpy_side/bench_scenes.py:8-13`, `bpy_side/bench_scenes.py:85-91`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:479`]

| Parameter | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Initial state | `M64=(1<<64)-1`; `bench_no=BENCHES.index(bench)+1`; `state=((bench_no<<32)^int(seed)) & M64`. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:27`, `bpy_side/bench_scenes.py:50-52` |
| SplitMix64 step | `x=(x+0x9E3779B97F4A7C15)&M64`; `z=((x^(x>>30))*0xBF58476D1CE4E5B9)&M64`; `z=((z^(z>>27))*0x94D049BB133111EB)&M64`; return state `x` and output `z^(z>>31)`. | `bpy_side/bench_scenes.py:33-38` |
| Integer draw | Advance once, then `randint(lo,hi)=lo+z%(hi-lo+1)`, inclusive endpoints. | `bpy_side/bench_scenes.py:56-58` |
| First two draws | Camera XY offset, each integer in `[-2,2]`. | `bpy_side/bench_scenes.py:50-54` |
| Scene slot | `i=(37*k+11*bench_no)%169`; slot `(i%13-6,i//13-6)`. The constants are side 13, multiplier 37, bench multiplier 11. | `bpy_side/bench_scenes.py:29`, `bpy_side/bench_scenes.py:41-44` |
| First jittered object | If the object-offset dictionary is empty, use `slot+camera_offset`, giving the slot as its added displacement relative to the camera offset. This branch consumes no random draw. | `bpy_side/bench_scenes.py:60-67` |
| Later jittered objects | Draw each XY offset in `[-4,4]` and record it by name. | `bpy_side/bench_scenes.py:60-67` |
| Color | Per channel `v`, compute `min(0.95,max(0.05,round(v+0.02*randint(-4,4),6)))` and record the resulting RGB. | `bpy_side/bench_scenes.py:69-72` |
| Light | Draw energy permille in `[850,1150]` and XY integer offsets each in `[-16,16]`; record by light name. | `bpy_side/bench_scenes.py:74-78` |
| Application | `add_box` applies placement/color jitter only if JIT exists and `jit=True`. `key_loc` reuses the saved object offset; these helpers do not randomize sizes, velocities or keyframe times. B6's lit plane and B8's half plane have the explicit exceptions described in their sections. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:387`, `bpy_side/bench_scenes.py:430-437` |
| Jitter record | Keys `prng:"splitmix64"`, `seed`, `camera_px`, `objects`, `colours`, `lights`; manifest also records `scene_seed`. | `bpy_side/bench_scenes.py:80-82`, `bpy_side/bench_scenes.py:519-520` |
| Uniqueness boundary | The slot is injective in `k mod 169`, not over all integer seeds; seeds separated by 169 repeat that slot. A guarantee of distinct rendered pixels for every seed, frame and projection is **ABSENT** from this placement calculation. Runner preflight checks planned scene-slot collisions separately. | `bpy_side/bench_scenes.py:29`, `bpy_side/bench_scenes.py:41-44`, `bpy_side/bench_scenes.py:60-67`; `blender_visual_qa/truth/run_bench.py:228-242` |

## B1

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Objects and order | Six cubes: `b1_box16,b1_box8,b1_box5,b1_box3,b1_box2,b1_box1`; columns `((16,8,5),(3,2,1))`. For column `ci` and row `ri`, `i=3*ci+ri`. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:202-212` |
| Geometry | XY size `(48,48)`; `y=h/2-(ri+0.5)*h/3`; `x0=-w/2+32` in column 0 and 24 in column 1. | `bpy_side/bench_scenes.py:205-212` |
| Material | Base RGB `(0.9,0.4+0.08*i,0.2)`, emission strength 1; checker scale 8 for odd `i` (boxes 8, 3, 1), none otherwise. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:205-212` |
| Motion | LINEAR XY keys at frame 1 `(x0,y)` and frame `n` `(x0+v*(n-1),y)`, where `v` is the name suffix. Positive-X slope `v` in nominal units between distinct endpoint frames. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:205-214` |
| Annotation | `transform` at frame 1 for each cube; temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:97-109`, `bpy_side/bench_scenes.py:202-215` |
| Camera, seed and jitter | Shared orthographic camera; fixed manifest seed mode; bench number 1; first jittered object `b1_box16`. All cubes receive optional shared object/color jitter. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-215`, `bpy_side/bench_scenes.py:502-504` |
| Classification limit | The docstring describes RIGID-tile existence at a particular tile size. A guaranteed output classification from this builder alone is **ABSENT**; the implemented construction is the geometry and keys above. | `bpy_side/bench_scenes.py:203-215` |

## B1x

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Objects | Three cubes `b1x_box48,b1x_box64,b1x_box80`, with `ri=0,1,2` and corresponding `v=48,64,80`; XY size `(48,48)`. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:218-226` |
| Placement and material | `x0=-w/2+48`; `y=h/2-(ri+0.5)*h/3`. RGB `(0.9,0.5-0.1*ri,0.2+0.2*ri)`, strength 1; checker scale 8 for even `ri` (48 and 80), none for 64. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:221-226` |
| Keyed travel | `k=max(1,min(n-1,int((w-96)//v)))`; LINEAR XY keys at frame 1 `(x0,y)` and frame `1+k` `(x0+v*k,y)`. No extra key at `n` unless it coincides with `1+k`. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:224-228` |
| Default arithmetic | Substitution of `w=640,n=8` gives `k=7,7,6`, hence final keyed frames `8,8,7` for velocities `48,64,80`. | `bpy_side/bench_scenes.py:221-228`, `bpy_side/bench_scenes.py:533-542` |
| Annotation | `transform` at frame 1 for each cube; temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:97-109`, `bpy_side/bench_scenes.py:218-229` |
| Camera, seed and jitter | Shared orthographic camera; fixed manifest seed mode; bench number 2; first jittered object `b1x_box48`; shared box/color jitter. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:218-229`, `bpy_side/bench_scenes.py:502-504` |
| Limits | The docstring mentions `MOTION_D_MAX 64`, MOVED behavior and stopping at the edge. Classification execution and explicit extrapolation after the last key are **ABSENT** from this builder; use the exact `k` formula, including its outer lower bound of 1. | `bpy_side/bench_scenes.py:163-176`, `bpy_side/bench_scenes.py:219-229` |

## B2

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Sliding planes | `b2_slideA`: XY `(-40,20)`, size `(60,40)`, RGB `(0.2,0.7,0.3)`. `b2_slideB`: XY `(40,10)`, size `(60,40)`, RGB `(0.8,0.2,0.6)`; B's Z is explicitly 0.1 Blender units. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:234-236` |
| Sliding motion | LINEAR keys: A at frame 1 `(-40,20)`, frame `n` `(-40+4*(n-1),20)`; B at frame 1 `(40,10)`, frame `n` `(40-4*(n-1),10)`. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:237-240` |
| Visibility planes | `b2_appear`: XY `(0,-50)`, size `(30,30)`, RGB `(0.9,0.9,0.1)`. `b2_vanish`: XY `(-60,-50)`, size `(30,30)`, RGB `(0.1,0.8,0.9)`. | `bpy_side/bench_scenes.py:243-244` |
| Materials | All four planes retain strength-1 emission; checker arguments **ABSENT**. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:234-244` |
| Visibility motion | `mid=max(2,n//2)` (4 at default n). Appear `hide_render`: true at 1, false at mid. Vanish: false at 1, true at mid. CONSTANT interpolation on these visibility objects. | `bpy_side/bench_scenes.py:245-251` |
| Annotation | Slide objects: `transform` at 1. Appear/vanish: `visibility` at mid. Temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:97-109`, `bpy_side/bench_scenes.py:241-252` |
| Camera, seed and jitter | Shared orthographic camera, fixed mode, bench number 3; first jittered object `b2_slideA`; shared jitter on all four. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:232-252`, `bpy_side/bench_scenes.py:502-504` |

## B3

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Emission objects | Seven planes `b3_emit00..06`; index `i=0..6`; XY `(-w/2+(i+0.5)*w/7,h/4)`, size `(w/7-6,40)`, RGB `(0.2,0.2,0.2)`, no checker. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:257-262` |
| Emission animation | Ordered steps `[0.25,0.5,1,2,4,8,16]`. Strength 1.0 keyed at frame 1; each plane's step keyed at every frame `2..n`, CONSTANT. The step-1 plane still gets a material event despite unchanged strength. | `bpy_side/bench_scenes.py:179-183`, `bpy_side/bench_scenes.py:257-266` |
| Lit geometry | Plane `b3_lit`, XY `(0,-h/4)`, size `(w-20,40)`, initially passed RGB `(0.5,0.5,0.5)`. | `bpy_side/bench_scenes.py:268` |
| Replacement shader | Clear that plane's material nodes and connect a new Principled BSDF to Material Output. Explicit Base Color and other Principled inputs are **ABSENT**; the initial emission RGB is not explicitly assigned to the replacement node. | `bpy_side/bench_scenes.py:269-274` |
| Point light | `b3_light`, POINT, location `(dx*PX,dy*PX,3)`. Golden `(e,dx,dy)=(1000,0,0)`; seeded draws give `e=850..1150`, XY `-16..16`. | `bpy_side/bench_scenes.py:25`, `bpy_side/bench_scenes.py:74-78`, `bpy_side/bench_scenes.py:275-278` |
| Light animation | At every frame `1..n`, energy `(50.0 if f<3 else 400.0)*e/1000`, CONSTANT light-data interpolation. | `bpy_side/bench_scenes.py:279-282` |
| Annotation | Material event at 2 for every emission plane; light event at 3; `lit=["b3_lit"]`. These annotation calls do not guard the event frame against n. Temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:97-109`, `bpy_side/bench_scenes.py:263-266`, `bpy_side/bench_scenes.py:279-284` |
| Camera, geometry and jitter | Shared orthographic camera; geometry/camera location keys **ABSENT**. Bench number 4; fixed mode; first jittered object `b3_emit00`; shared geometry/color jitter, followed by light draws. The initially jittered color for `b3_lit` remains recorded even though its emission nodes are replaced. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-78`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:255-284`, `bpy_side/bench_scenes.py:502-504` |

## B4

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. Source mentions `--frames 60` as an explicit override, not the default. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Flicker geometry | Three planes `b4_flick_p2,b4_flick_p3,b4_flick_p4`; `i=0,1,2`, periods `p=2,3,4`; XY `(-w/2+(i+0.5)*w/6,h/4)`, size `(w/6-8,40)`, RGB `(0.7,0.7,0.7)`, no checker. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:289-292` |
| Flicker keys and annotation | For every `f=1..n`, strength `1.0 if (f-1)%p<p//2 else 0.2`, CONSTANT. Add material events at every `2..n`, even when adjacent strengths match; temporal `flicker` interval `[2,n]`. Period 3 uses integer division and has one high frame per three-frame cycle. | `bpy_side/bench_scenes.py:179-183`, `bpy_side/bench_scenes.py:293-297` |
| Freeze cube | `b4_freeze`: XY `(-w/4,-h/4)`, size `(20,20)`, RGB `(0.2,0.6,0.9)`. `stop=max(2,n//2)`; LINEAR keys at 1 and stop, ending at `(-w/4+4*(stop-1),-h/4)`. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:298-301` |
| Freeze annotation and limit | Transform at 1; `frozen` interval `[stop+1,n]`; default n=8 gives stop 4 and interval `[5,8]`. Explicit post-final-key extrapolation is **ABSENT**. | `bpy_side/bench_scenes.py:163-176`, `bpy_side/bench_scenes.py:299-303` |
| Creep cube | `b4_creep`: XY `(0,-h/4)`, size `(20,20)`, RGB `(0.9,0.6,0.2)`; LINEAR keys at 1 and n ending at `((n-1)/8,-h/4)`, hence nominal X slope 1/8 between distinct endpoints. Transform at 1; `drift` interval `[2,n]`. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:304-308` |
| Ramp planes | `b4_ramp0,b4_ramp1`; indices 0,1 correspond to `v=0.5,0.25`; XY `(w/4+i*40,-h/4)`, size `(30,30)`, RGB `(0.3,0.3,0.3)`, no checker. | `bpy_side/bench_scenes.py:309-311` |
| Ramp keys and annotation | At every frame, strength `0.2+v*((f-1)%8)` with LINEAR interpolation. Material events at all frames `2..n`, including resets; `drift` interval `[2,n]`. The source implements a resetting eight-frame sawtooth, not an unbounded ramp. | `bpy_side/bench_scenes.py:312-316` |
| Camera, seed and jitter | Shared orthographic camera, fixed mode, bench number 5; first jittered object `b4_flick_p2`; shared object/color jitter on all seven objects. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:287-316`, `bpy_side/bench_scenes.py:502-504` |
| Saturation limit | The 60-frame no-saturation statement is a source comment; rendered validation of it is **ABSENT** from the builder. | `bpy_side/bench_scenes.py:312-313` |

## B4f

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` also defaults to 8. Source mentions explicit `--frames 60`. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542` |
| Freeze cube | `b4f_freeze`: XY `(-w/4,h/4)`, size `(20,20)`, RGB `(0.2,0.6,0.9)`; `stop=max(2,n//4)`. | `bpy_side/bench_scenes.py:330-335` |
| Freeze keys and annotation | LINEAR keys at frame 1 and stop, from `(-w/4,h/4)` to `(-w/4+4*(stop-1),h/4)`; transform event at 1, `frozen` interval `[stop+1,n]`. Explicit post-final-key extrapolation **ABSENT**. | `bpy_side/bench_scenes.py:163-176`, `bpy_side/bench_scenes.py:336-339` |
| Segment formula | `span=max(1,n-stop)`; for `k=0,1,2`, compute `a=stop+1+(k*span)//3`, `b=stop+((k+1)*span)//3`; return `(min(a,n),min(max(a,b),n))`. The clamps apply even for short frame ranges. | `bpy_side/bench_scenes.py:319-327` |
| Movers and nominal ppm | Pair segments with `30000,80000,150000`; names `b4f_mover03,b4f_mover08,b4f_mover15`. Coverage metadata records those ppm values and segment bounds. | `bpy_side/bench_scenes.py:340-342`, `bpy_side/bench_scenes.py:355` |
| Mover geometry | Each is a cube centered at `(w/4,-h/8)`, with square side `int(max(8,min(min(w,h)-8,round((ppm*w*h/1e6)**0.5))))`. These are rounded/clamped nominal coverage inputs; measured visible or moving coverage is **ABSENT** here. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:340-343`, `bpy_side/bench_scenes.py:355` |
| Mover material | RGB `(0.9,0.6-0.1*k,0.2)`, strength 1, checker scale 6. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:340-343` |
| Mover location keys | Initially `key_loc` at frame 1 `(w/4,-h/8)` and frame n `(w/4+2*(n-1),-h/8)`; transform event at 1. | `bpy_side/bench_scenes.py:344-346` |
| Mover visibility keys | `hide_render=(a>1)` at 1; false at a; true at `b+1` when `b<n`. | `bpy_side/bench_scenes.py:347-349` |
| **Final mover interpolation** | After visibility insertion, `set_interp(o,"CONSTANT")` changes every object F-curve, including both location curves. A continuously interpolated two-pixel-per-frame mover trajectory is **ABSENT from the final configured keys**. | `bpy_side/bench_scenes.py:163-168`, `bpy_side/bench_scenes.py:344-350` |
| Visibility/coverage annotation | Visibility at a when `a>1`, and at `b+1` when `b<n`; coverage record `{name,ppm,frames:[a,b]}`. | `bpy_side/bench_scenes.py:112-113`, `bpy_side/bench_scenes.py:351-355` |
| Default arithmetic | `w=640,h=360,n=8` gives stop 2, segments `[3,4],[5,6],[7,8]`, and sides `83,136,186`. | `bpy_side/bench_scenes.py:319-327`, `bpy_side/bench_scenes.py:334-341`, `bpy_side/bench_scenes.py:533-542` |
| Camera, seed and jitter | Shared orthographic camera, fixed mode, bench number 6; first jittered object `b4f_freeze`; shared jitter on freeze and mover objects. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:330-355`, `bpy_side/bench_scenes.py:502-504` |

## B4n

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder dimensions and frames | Resolution default `640x360`; **CLI frame default 32** through `DEFAULT_FRAMES["B4n"]`; **direct build default 8**. Range `1..n`. Runner explicitly supplies its own frame count, default 8, and resolution, default `320x180`. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:496`, `bpy_side/bench_scenes.py:533-542`; `blender_visual_qa/truth/run_bench.py:324-326`, `blender_visual_qa/truth/run_bench.py:790-793` |
| Objects | Four cubes `b4n_box0..3`, size `(36,36)`, in increasing i order. | `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:358-363` |
| Placement | `(-w/3+i*w/4.5,(-1)**i*h/6)`, `i=0..3`. | `bpy_side/bench_scenes.py:361-362` |
| Materials | RGB `(0.3+0.15*i,0.5,0.7-0.1*i)`, strength 1; checker scale 5 on odd indices 1 and 3, none on even indices. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:362-363` |
| Motion and annotation | Geometry/material/light/camera keys and animated/temporal records are **ABSENT** in this function; it constructs static geometry. | `bpy_side/bench_scenes.py:94-109`, `bpy_side/bench_scenes.py:358-364` |
| Seed mode | Manifest `per_frame`; build still initializes Cycles seed 0 and animated seed false. No per-frame Cycles keys are inserted by this builder; the runner supplies the per-frame handler. | `bpy_side/bench_scenes.py:364`, `bpy_side/bench_scenes.py:484-485`, `bpy_side/bench_scenes.py:508`; `blender_visual_qa/truth/run_bench.py:217-225` |
| Camera and scene jitter | Shared orthographic camera; bench number 7; first jittered object `b4n_box0`; shared scene-level geometry/color jitter. Added lights and per-frame geometry jitter loops are **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:358-364`, `bpy_side/bench_scenes.py:479`, `bpy_side/bench_scenes.py:502-504` |
| Control claim | The docstring describes a FLICKER/DRIFT false-positive control. A quantitative noise level or demonstrated false-positive result is **ABSENT** from the builder. | `bpy_side/bench_scenes.py:359-364` |

## B5

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| Objects | Three cubes `b5_obj0..2`; XY `(-w/4,0),(0,0),(w/4,0)`; size `(30,50)`, omitted Z location 0 and Z scale 1. | `bpy_side/bench_scenes.py:145-157`, `bpy_side/bench_scenes.py:403-407` |
| Materials | Strength-1 emission; RGB `(0.3+0.2*i,0.5,0.8)`; checker scale 6 only on `b5_obj1`. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:403-407` |
| Perspective camera | Lens 35, rotation `(0,0,0)`. Let `(cx,cy)=camera_offset*PX`; location at frame 1 `(cx,cy,12)`, frame n `(0.8+cx,0.3+cy,11)`, in Blender units, LINEAR location curves. | `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:408-421`, `bpy_side/bench_scenes.py:502-504` |
| Object motion and lights | Object location keys and added lights are **ABSENT**; the bench keys the camera. | `bpy_side/bench_scenes.py:403-422` |
| Jitter and seed mode | Shared cube placement/color jitter and camera micro-offset; first jittered object `b5_obj0`; bench number 8; fixed manifest seed mode. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:403-419` |
| Annotation | One camera event at frame 1; temporal and coverage records **ABSENT** from this function. | `bpy_side/bench_scenes.py:94-105`, `bpy_side/bench_scenes.py:403-422` |
| Projection limit | Exact projected object sizes or uniform screen-pixel displacement are **ABSENT**. The code supplies nominal geometry converted to world units and the perspective camera assignments above. | `bpy_side/bench_scenes.py:145-157`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:415-421` |

## B5o

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| Dispatch and objects | Calls `b5(c,w,h,ortho=True)`. Three cubes named `b5_obj0..2`, same XY centers `(-w/4,0),(0,0),(w/4,0)` and size `(30,50)` as B5. | `bpy_side/bench_scenes.py:145-157`, `bpy_side/bench_scenes.py:403-407`, `bpy_side/bench_scenes.py:503-504` |
| Materials | Strength-1 emission; RGB `(0.3+0.2*i,0.5,0.8)`; checker scale 6 only on the middle cube. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:403-407` |
| Orthographic camera | Scale `w*PX`, zero rotation, Y/Z `(cy,10)`. At frames `f=1,n`, key X to `cx+3*(f-1)*PX`, LINEAR. This defines positive world-X camera translation of three nominal builder units per frame between distinct endpoints. | `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:408-414`, `bpy_side/bench_scenes.py:502-504` |
| Object motion and lights | Object location keys and added lights **ABSENT**; only camera X is keyed. | `bpy_side/bench_scenes.py:403-414` |
| Jitter and seed mode | Shared object/color/camera jitter; first jittered object `b5_obj0`; bench number 9, distinct from B5 despite the shared builder; fixed mode. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:403-414` |
| Annotation | Camera event at 1; temporal and coverage records **ABSENT**. | `bpy_side/bench_scenes.py:94-105`, `bpy_side/bench_scenes.py:403-422` |

## B6

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| Base objects | Three cubes `b6_box0..2`; XY `(-w/4+i*w/4,0)`, size `(40,40)`, omitted Z location 0 and Z scale 1. | `bpy_side/bench_scenes.py:145-157`, `bpy_side/bench_scenes.py:425-429` |
| Base materials | Strength-1 emission; RGB `(0.8,0.3+0.2*i,0.3)`; checker scale 5 only on index 1. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:425-429` |
| Camera and jitter | Static shared orthographic camera; shared jitter on the base cubes; first jittered object `b6_box0`; bench number 10. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:425-444`, `bpy_side/bench_scenes.py:502` |
| Golden geometry | With no scene seed, the lit-plane/light branch is skipped; this bench adds only the three base cubes. | `bpy_side/bench_scenes.py:425-444`, `bpy_side/bench_scenes.py:479` |
| Seeded lit plane | Add `b6_lit` at nominal `(0,0,-32)`, hence Z=-1 Blender unit, size `(w-16,h-16)`. `jit=False` prevents automatic placement/color jitter, while a direct `JIT.colour` call separately jitters RGB from `(0.5,0.5,0.5)`. | `bpy_side/bench_scenes.py:25`, `bpy_side/bench_scenes.py:69-72`, `bpy_side/bench_scenes.py:145-159`, `bpy_side/bench_scenes.py:430-432` |
| Seeded plane shader | Clear nodes; Principled BSDF with Base Color explicitly set to recorded jittered RGB plus alpha 1; connect to Material Output. Other Principled input assignments **ABSENT**. | `bpy_side/bench_scenes.py:433-437` |
| Seeded light | `b6_light`, AREA, location `(dx*PX,dy*PX,3)`, size 2.0, energy `300.0*e/1000`; shared light draws `e=850..1150`, XY `-16..16`. Other area-light assignments **ABSENT**. | `bpy_side/bench_scenes.py:74-78`, `bpy_side/bench_scenes.py:438-442` |
| Motion and annotation | Transform, visibility, material and energy keyframes **ABSENT**. Add `b6_lit` to `lit` only in the seeded case; animated/temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:94-109`, `bpy_side/bench_scenes.py:425-444` |
| Seed schedule | Set manifest `per_frame` in both seeded and golden cases. Saved Cycles seed remains 0 with animated seed false; per-frame sampling is applied by the runner expression. | `bpy_side/bench_scenes.py:444`, `bpy_side/bench_scenes.py:484-485`, `bpy_side/bench_scenes.py:507-508`; `blender_visual_qa/truth/run_bench.py:217-225` |

## B7

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| Object | One plane `b7_alias`, XY `(-w/4,0)`, size `(256,80)`. | `bpy_side/bench_scenes.py:447-450` |
| Material | Strength-1 emission, RGB `(0.9,0.9,0.9)`, checker scale 32, secondary color `(0.05,0.05,0.05,1)`. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:450` |
| Motion | LINEAR XY keys at 1 `(-w/4,0)`, and n `(-w/4+16*(n-1),0)`; recorded object jitter is reused at both endpoints. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:451-452` |
| Checker-period limit | The source comment describes eight-pixel squares and a 16-pixel period, translated by a full period per frame. The node code assigns Scale=32; explicit Vector mapping and runtime verification of the claimed screen period are **ABSENT**. | `bpy_side/bench_scenes.py:133-138`, `bpy_side/bench_scenes.py:449-452` |
| Camera and seeding | Shared orthographic camera and object/color jitter; first jittered object `b7_alias`; bench number 11; fixed mode. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:447-453`, `bpy_side/bench_scenes.py:502-504` |
| Annotation | Transform at frame 1 for `b7_alias`; temporal entries **ABSENT**. | `bpy_side/bench_scenes.py:447-453` |

## B8

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| First camera | Rename initial shared orthographic camera to `cam_a`. | `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:371-372`, `bpy_side/bench_scenes.py:502` |
| Box groups | `off=2*w`; for `i=0,1`, `x=(-w/4,w/4)`, add `b8_a<i>` at `(x,0)`, then `b8_b<i>` at `(off+x,0)`; all cubes size `(50,50)`. Creation order `a0,b0,a1,b1`. | `bpy_side/bench_scenes.py:145-157`, `bpy_side/bench_scenes.py:373-376` |
| Box materials | A RGB `(0.9,0.3+0.3*i,0.2)`, checker 6 on a1 only. B RGB `(0.2,0.3+0.3*i,0.9)`, checker 6 on b0 only. Shared strength-1 emission. | `bpy_side/bench_scenes.py:124-145`, `bpy_side/bench_scenes.py:375-376` |
| Second camera | Create shared orthographic camera, rename `cam_b`, set X to `(off+camera_dx)*PX`; Y/Z remain `(camera_dy*PX,10)`. Restore `scene.camera=cam_a`. | `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:377-380` |
| Full-cut markers | `cut=min(4,n)`; bind `cut_a` at 1 to cam_a; if `cut>1`, bind `cut_b` at cut to cam_b. | `bpy_side/bench_scenes.py:381-385` |
| Half-plane timing | `half=min(n,max(cut+1,(n*3)//4))`; default n=8 gives cut 4 and half 6. | `bpy_side/bench_scenes.py:381-386` |
| Half-plane construction | `b8_half`: plane XY `(w/4,0)`, size `(w/2,h)`, RGB `(0.8,0.8,0.2)`, checker 4; `jit=False` exempts both automatic position and color jitter; Z is explicitly 0.2 Blender units. | `bpy_side/bench_scenes.py:124-159`, `bpy_side/bench_scenes.py:387-388` |
| Half-plane visibility | `hide_render=True` keyed at 1; if `half>1`, false at half; CONSTANT interpolation. | `bpy_side/bench_scenes.py:389-394` |
| Annotation | If cut>1: camera event for cam_b and `{frame:cut,kind:"full"}`. If half>1: visibility event for b8_half and `{frame:half,kind:"half"}`. | `bpy_side/bench_scenes.py:395-400` |
| Other animation and lights | Box/camera transform keyframes, a later switch-back marker to cam_a, and added lights are **ABSENT**. | `bpy_side/bench_scenes.py:367-400` |
| Jitter and seed mode | Shared jitter on the four boxes; first jittered object a0; both cameras use the same micro-offset; half plane exempt. Bench number 12; fixed mode. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:374-387` |
| Half-cut limitation | The half plane remains centered at `w/4` while cam_b is placed around `2*w`; no relocation to the B group or return to camera A is implemented. A guaranteed visible half-screen change from camera B is **ABSENT**, despite the `"half"` annotation. | `bpy_side/bench_scenes.py:373-400` |

## BS

| Parameter | Definition | Source at `bridge/integration` |
|---|---|---|
| Builder defaults | `640x360`, 8 frames, range `1..n`; direct `build` defaults match. | `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:476`, `bpy_side/bench_scenes.py:487-496`, `bpy_side/bench_scenes.py:533-543` |
| Object and material | One cube `bs_box`, XY `(-w/4,h/4)`, size `(24,24)`, omitted Z location 0 and Z scale 1. Solid strength-1 emission, RGB `(0.9,0.5,0.1)`; checker **ABSENT**. | `bpy_side/bench_scenes.py:124-159`, `bpy_side/bench_scenes.py:456-459` |
| Motion | LINEAR keys at 1 `(-w/4,h/4)`, and n `(-w/4+3*(n-1),h/4+2*(n-1))`; shared jitter is added to both. Positive world-X/Y slopes of 3 and 2 nominal units per frame between distinct endpoints. | `bpy_side/bench_scenes.py:171-176`, `bpy_side/bench_scenes.py:460-461` |
| Sign-probe limit | The docstring names `T-TR-SIGN/T-TR-YFLIP`, calls the box top-left and positive world Y up. A decoded image-row or motion-vector sign conversion is **ABSENT** from this function. | `bpy_side/bench_scenes.py:456-462` |
| Camera and seed | Shared orthographic camera and object/color jitter; first jittered object `bs_box`; bench number 13; fixed mode. Added lights **ABSENT**. | `bpy_side/bench_scenes.py:23`, `bpy_side/bench_scenes.py:50-72`, `bpy_side/bench_scenes.py:102`, `bpy_side/bench_scenes.py:145-149`, `bpy_side/bench_scenes.py:186-199`, `bpy_side/bench_scenes.py:456-462`, `bpy_side/bench_scenes.py:502-504` |
| Annotation | Transform event at frame 1; temporal/coverage entries **ABSENT**. | `bpy_side/bench_scenes.py:456-462` |

## Runner protocol

### Bounds, defaults, tiers and task population

| Parameter | Defined value or behavior | Source at `bridge/integration` |
|---|---|---|
| Bench order and number | `B1, B1x, B2, B3, B4, B4f, B4n, B5, B5o, B6, B7, B8, BS`; `BENCH_NO` assigns one-based numbers in that order. | `blender_visual_qa/truth/run_bench.py:49-50` |
| Default invocation settings | All listed benches; `--frames 8`; `--res 320x180`; `--spp "4"`; `--seeds None`; `--jobs 1`; `--decode inblender`; determinism checking disabled unless requested. | `blender_visual_qa/truth/run_bench.py:51-53`, `blender_visual_qa/truth/run_bench.py:787-803` |
| Accepted image and frame bounds | Width `1..320`, height `1..180`, frames `2..60`. The runner explicitly passes its resolution and frame count to the builder, including for B4n; the builder CLI's B4n-specific 32-frame default therefore does not apply to a normal runner invocation. | `blender_visual_qa/truth/run_bench.py:324-326`, `blender_visual_qa/truth/run_bench.py:827-839`; `bpy_side/bench_scenes.py:24`, `bpy_side/bench_scenes.py:542-543` |
| Noisy sample tiers | Only distinct integer values from `(4,16,64)`; a comma-separated list preserves the supplied order. Invalid integer syntax raises `E_USAGE`; duplicate or unsupported tiers raise `E_BUDGET`. | `blender_visual_qa/truth/run_bench.py:53`, `blender_visual_qa/truth/run_bench.py:817-824` |
| Tier execution | One `run_tier` per supplied spp value, sequentially in the list comprehension. A single tier uses `--out`; multiple tiers use `--out/spp_<spp>`, write `tiers.json`, and return the maximum tier return code. | `blender_visual_qa/truth/run_bench.py:1100-1124` |
| Seed partition syntax | Exactly one `fit:<lo>[-<hi>]` and one `gate:<lo>[-<hi>]`, comma-separated; inclusive nonnegative ranges, each at most 40 entries and every value below `2^19`. Overlap raises `E_GATE` with `rc=5`; malformed, missing or repeated groups raise `E_TRUTH_ARGS`. | `blender_visual_qa/truth/run_bench.py:59-60`, `blender_visual_qa/truth/run_bench.py:193-208` |
| Legacy versus seeded geometry | With no partition, tasks use a legacy arm at seed 0 and `scene_seed=None` (golden geometry). FIT/GATE tasks pass logical arm `k` as both the beauty seed coordinate and scene-jitter seed. | `blender_visual_qa/truth/run_bench.py:900-911`, `blender_visual_qa/truth/run_bench.py:933-935` |
| Worker bounds | At most 4 Blender threads. CPU plan `w9`: `15-19`, `0-4`, `10-13` (up to 3 jobs); `w10`: `5-9`, `15-19`, `0-4`, `10-14` (up to 4 jobs). Default plan is w9 for at most 3 requested jobs and w10 otherwise, with job count validated against the chosen plan. | `blender_visual_qa/truth/run_bench.py:51`, `blender_visual_qa/truth/run_bench.py:61-65`, `blender_visual_qa/truth/run_bench.py:841-843` |
| Process setup | Blender path is `BVQA_BLENDER` or `/usr/bin/blender`; launches use background/factory-startup/no-audio mode, Python error exit code 9, thread limit, and `taskset`. `BVQA_CPU_CAP` can override the selected CPU mask. Missing taskset raises `E_AFFINITY`; a missing Blender path raises `E_NO_BLENDER`. | `blender_visual_qa/truth/run_bench.py:48`, `blender_visual_qa/truth/run_bench.py:258-262`, `blender_visual_qa/truth/run_bench.py:857-858` |
| Process timeout | The source default is `RENDER_TIMEOUT=300` seconds. Timeout kills and reaps the process group and raises `E_TIMEOUT`. This is a configured limit, not an observed rendering duration. | `blender_visual_qa/truth/run_bench.py:67`, `blender_visual_qa/truth/run_bench.py:265-292` |
| FIT/GATE order | FIT/legacy renders and analyses occur first. `T0_Q4` is fitted from FIT results and the B6 still samples, clamped to an imported engine bound, then frozen before GATE rendering/analysis. GATE is processed in batches of at most `jobs`, scored at the frozen table and released. FIT rows remain the first-pass rows. | `blender_visual_qa/truth/run_bench.py:862-864`, `blender_visual_qa/truth/run_bench.py:970-1004`, `blender_visual_qa/truth/run_bench.py:1024-1054` |
| Calibration limits of this scope | Missing B6 still samples cause `still_probe_absent`; legacy single-arm runs suppress the profile with `seed_partition_absent`. A fitted profile whose reported threshold differs from the frozen threshold is refused as `t0_not_frozen`. The fitting algorithm, imported engine bounds and full acceptance criteria are **ABSENT** from the bound files. | `blender_visual_qa/truth/run_bench.py:862-864`, `blender_visual_qa/truth/run_bench.py:1030-1032`, `blender_visual_qa/truth/run_bench.py:1053-1068` |

### Cycles seed modes

Scene jitter and Cycles sample seeding are separate operations: `render_bench` supplies optional `--seed scene_seed` to the scene builder, reads the returned manifest, then passes the beauty arm and manifest seed mode to `seed_expression`. The builder itself saves `cycles.seed=0` and disables animated seeds; the runner expression applies the sampling schedule at render time. [`blender_visual_qa/truth/run_bench.py:316-336`; `bpy_side/bench_scenes.py:479-485`, `bpy_side/bench_scenes.py:507-520`]

| Mode or operation | Exact runner behavior | Source at `bridge/integration` |
|---|---|---|
| Shared expression | Import `cycles_seed` from `bpy_side.canon`; force CPU, disable `use_animated_seed`, set initial seed to `cycles_seed(BENCH_NO[bench],k,0)`, and assign requested spp. | `blender_visual_qa/truth/run_bench.py:217-225` |
| `fixed` | `beauty_seed` uses render-index coordinate 0 regardless of its `ri` argument. `seed_expression` installs no per-frame handler. | `blender_visual_qa/truth/run_bench.py:211-225` |
| `per_frame` | `beauty_seed` uses its `ri`. The expression appends a `render_pre` handler setting `cycles_seed(BENCH_NO[bench],k,sc.frame_current)` before each render. | `blender_visual_qa/truth/run_bench.py:211-225` |
| Allowed schedule | Only B4n and B6 are registered as per-frame benches. Manifest `seed_mode` defaults to `fixed` if missing; a disagreement with this registration raises `E_TRUTH_ARGS`. Invalid modes also raise `E_TRUTH_ARGS` in `beauty_seed`. | `blender_visual_qa/truth/run_bench.py:55-56`, `blender_visual_qa/truth/run_bench.py:211-214`, `blender_visual_qa/truth/run_bench.py:328-332` |
| Collision preflight | Across the planned benches and distinct arm values, check `ri=0..nframes` for per-frame benches and only `ri=0` for fixed benches. Equal generated seeds for different coordinates raise `E_TRUTH_ARGS`. The zero coordinate is checked although noisy capture chunks begin at render index 1. | `blender_visual_qa/truth/run_bench.py:245-255`, `blender_visual_qa/truth/run_bench.py:311-313`, `blender_visual_qa/truth/run_bench.py:853-856` |
| Scene-slot collision preflight | For seeded partitions, `i=(37*k+11*BENCH_NO[bench])%169`, slot `(i%13-6,i//13-6)`; duplicate slots among distinct planned arm values of a bench raise `E_TRUTH_ARGS`. | `blender_visual_qa/truth/run_bench.py:228-242`, `blender_visual_qa/truth/run_bench.py:853-856` |
| Collision scope limits | Seed preflight does not include an spp coordinate or reserved clean coordinates; the scene-slot check has a separate seen-map per bench. It does not establish universal collision-freedom for unplanned captures. | `blender_visual_qa/truth/run_bench.py:234-255`, `blender_visual_qa/truth/run_bench.py:853-856` |
| Imported seed mixer | The implementation and numeric outputs of `bpy_side.canon.cycles_seed` are **ABSENT** from the four bound files. They must not be equated with the scene-jitter SplitMix64 implementation. Only the call coordinates, mode validation and collision checks above are established here. | `blender_visual_qa/truth/run_bench.py:42`, `blender_visual_qa/truth/run_bench.py:211-255`; `bpy_side/bench_scenes.py:33-58` |

### Chunked capture and storage lifecycle

| Stage | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Chunk partition | `CHUNK_FRAMES=8`; `chunk_ranges(n,size)` yields inclusive `(lo,min(lo+size-1,n))` for `lo=1,1+size,...`. Default chunks cover render indices `1..n` with at most 8 frames each. | `blender_visual_qa/truth/run_bench.py:52`, `blender_visual_qa/truth/run_bench.py:311-313` |
| Scene build | Invoke `bench_scenes.py` once with bench, output blend path, explicit resolution, frames, samples and optional scene seed; then read `<bench>.scene.json`. | `blender_visual_qa/truth/run_bench.py:316-329` |
| Noisy beauty per chunk | Load the blend, execute the seed expression, and render PNG output at `render/frame_####` with inclusive `-s lo -e hi -a`. | `blender_visual_qa/truth/run_bench.py:333-340` |
| Truth per chunk | Invoke `passes.py` for `lo:hi`, with `--truth-seed 0`, `--truth-spp 1`, and the scene JSON. Use the unit's `_exr` directory when a RAM workspace is active. The actual pass renderer, quantization and frame-merge implementation are **ABSENT** from the bound files. | `blender_visual_qa/truth/run_bench.py:317-318`, `blender_visual_qa/truth/run_bench.py:341-344` |
| Decode selection | Default decode is in Blender. External mode adds `--exr-codec NONE --decode external` to the pass command, then runs `blender_visual_qa.truth.decode_worker` over `truth/pending.jsonl`. Its implementation is **ABSENT** here. | `blender_visual_qa/truth/run_bench.py:345-355`, `blender_visual_qa/truth/run_bench.py:801` |
| Per-chunk release | After capture/decode, account workspace usage and release the EXR directory. A requested pass determinism check is attached to the first chunk only. | `blender_visual_qa/truth/run_bench.py:347-357` |
| Metadata handed to analysis | Write render metadata with app `blender`, lowercase bench scene, build sequence 0, fps 24/1 and adapter version `1+cycles-cpu-det`; copy the scene manifest to both `journal.json` and `scene.manifest.json`. | `blender_visual_qa/truth/run_bench.py:358-363` |
| Normal CLI workspace | `_main` opens a `vadapt.ramws` run for the campaign/run ID and requested quota before tiers execute, installs it in `_Store`, and closes it in `finally`. Tier roots are allocated through `ws.path`. RAM quota defaults to 2048 MB and is validated in `1..8192` MB. | `blender_visual_qa/truth/run_bench.py:73`, `blender_visual_qa/truth/run_bench.py:849-850`, `blender_visual_qa/truth/run_bench.py:905-911`, `blender_visual_qa/truth/run_bench.py:1101-1119` |
| Admission | Before rendering a unit, account usage and reserve a planned peak computed from all truth/PNG frames plus one EXR chunk, scaled by image area. Reservation is then removed from the accounting values. The constants are source estimates, not measurements performed for this specification. | `blender_visual_qa/truth/run_bench.py:76-77`, `blender_visual_qa/truth/run_bench.py:105-116`, `blender_visual_qa/truth/run_bench.py:933` |
| Evidence persistence | `persist_unit` skips named scratch subtrees, imported frame extensions/names, hidden and temporary files, symlinks and non-files; logs are capped to their last 256 KiB. A unit is persisted, optionally kept, then released with its determinism-repeat directory. The imported frame-extension/name sets and RAM-workspace internals are **ABSENT** from this scope. | `blender_visual_qa/truth/run_bench.py:74-75`, `blender_visual_qa/truth/run_bench.py:126-148`, `blender_visual_qa/truth/run_bench.py:168-184` |
| Optional frame retention | `--keep-frames` defaults to 0; its help describes a 256 MB cap and a 24-hour TTL. Validation uses imported `ramws.KEEP_CAP_MB`; `keep_unit` delegates copies to `ramws.keep_frames` and decrements the remaining cap. Exact retention enforcement is **ABSENT** from these bound files. | `blender_visual_qa/truth/run_bench.py:151-165`, `blender_visual_qa/truth/run_bench.py:808-809`, `blender_visual_qa/truth/run_bench.py:851-852` |
| Direct-call caveat | `_Store.ws` starts as `None`; release/account operations become no-ops without a workspace, and tier roots remain the supplied output paths. An unconditional no-disk guarantee for direct helper calls is **ABSENT**. | `blender_visual_qa/truth/run_bench.py:80-102`, `blender_visual_qa/truth/run_bench.py:905-911` |
| Sampled determinism | If requested, repeat the first task and compare canonical score-row bytes and capture fingerprints. The fingerprint hashes sorted truth-array filenames/bytes and sorted beauty filenames/decoded PNG pixels. Empty captures and mismatches raise `E_DETERMINISM`; this is not an exhaustive check of every scene. | `blender_visual_qa/truth/run_bench.py:716-730`, `blender_visual_qa/truth/run_bench.py:928-944` |

### Clean captures and temporal residuals

| Parameter or step | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Eligible benches | Only B3 and B5 are in `CLEAN_BENCHES`; B5o is not included. | `blender_visual_qa/truth/run_bench.py:57`, `blender_visual_qa/truth/run_bench.py:739-743` |
| Default and bounds | Start default 1024 spp, cap 4096 spp. If `--clean-spp` is unspecified, enable 1024 only when seed partitions are present and selected benches include B3 or B5; otherwise use 0. Explicit values from 0 through 4096 are accepted; 0 disables the clean call. This setting is separate from noisy tiers `4/16/64`. | `blender_visual_qa/truth/run_bench.py:53-54`, `blender_visual_qa/truth/run_bench.py:739-743`, `blender_visual_qa/truth/run_bench.py:803-805`, `blender_visual_qa/truth/run_bench.py:845-848` |
| Two captures | For each trial spp, render the same saved noisy-scene blend into `clean_0/render` and `clean_1/render`. Each has a fixed seed `cycles_seed(BENCH_NO[bench],(1<<19) \| which,0)`, with `which=0,1`; force CPU and disable animated seeds. Clean beauty is chunked through the same `chunk_ranges`; it does not rebuild jitter or rerender truth passes. | `blender_visual_qa/truth/run_bench.py:59`, `blender_visual_qa/truth/run_bench.py:366-381`, `blender_visual_qa/truth/run_bench.py:605-609` |
| Analysis | Adapt each clean render and call vision analysis with `persist=False`. The runner describes this as consuming residuals without publishing the clean analysis output files; the adapter still prepares `vision_in`. | `blender_visual_qa/truth/run_bench.py:451-460`, `blender_visual_qa/truth/run_bench.py:477-485`, `blender_visual_qa/truth/run_bench.py:607-609` |
| Pair identity | Exactly two sources; `0<nframes<=60`; each source has the requested frame count and grid dimensions; paired frames have identical `(i,render_index,t_us)`. Disagreement raises `E_CLEAN_PAIR`. | `blender_visual_qa/truth/run_bench.py:566-575` |
| General agreement residual | Stack `change.residual(a,b,H_DEAD)` between the two clean runs' same-frame tile descriptors. Obtain `T_exit` through `labels.thresholds({"T0_Q4":table["T0_Q4"]})`. | `blender_visual_qa/truth/run_bench.py:609-611` |
| B5-specific agreement metric | For each real, unpadded tile of each paired decoded RGB image, compute channelwise absolute differences, take each pixel's maximum channel difference, and return `ceil(16*sum(pixel_max)/pixel_count)`. The implementation is `(16*tile.sum()+tile.size-1)//tile.size`. B5 uses this image-space value for convergence while retaining the same-frame S1 difference for diagnostics. | `blender_visual_qa/truth/run_bench.py:578-596`, `blender_visual_qa/truth/run_bench.py:612-619` |
| B5 agreement receipt | `clean_agreement.json` records schema `bvqa.clean-agreement/v1`, metric `rgb-max-mean-q4/v1`, spp, `threshold_q4=T_exit//4`, match permille, population, bad tiles, maximum q4 and S1 bad tiles. | `blender_visual_qa/truth/run_bench.py:615-619` |
| Acceptance of a clean pair | Count all frame/tile entries with agreement difference `<=T_exit//4`. Pass exactly when `good_count*1000 >= 999*population` (at least 99.9%). This is the clean self-check predicate, not acceptance of the overall task or PR. | `blender_visual_qa/truth/run_bench.py:58`, `blender_visual_qa/truth/run_bench.py:620-621` |
| Retry and failure | On failure, double spp. If the next doubling would exceed 4096, raise `E_BUDGET` with `rc=6`. A user-specified starting value need not be a power of two or reach exactly 4096 before this check stops. | `blender_visual_qa/truth/run_bench.py:605-624`, `blender_visual_qa/truth/run_bench.py:847-848` |
| Returned temporal residual | On success use `raw_resid` of clean run 0: an `int64 [F,N]` array in Morton order, first row zero, then `change.residual(clean[t],clean[t-1],H_DEAD)`. It is raw q4 S1 residual, not the `r>>2` display field. The imported S1 residual algorithm is **ABSENT** from these files. | `blender_visual_qa/truth/run_bench.py:488-492`, `blender_visual_qa/truth/run_bench.py:620-621` |
| Residual handoff | `run_one` saves the successful residual as `clean_resid.npy` and records used spp in `clean_spp.json` inside the unit; analysis also accepts an explicitly supplied clean array or loads that saved array. | `blender_visual_qa/truth/run_bench.py:657-671`, `blender_visual_qa/truth/run_bench.py:733-746` |
| GATE threshold caveat | GATE units render after T0 is frozen, but `flight` still calls `run_one` with the initial `table`; clean analysis and its self-check use that initial table before the `analyze=False` return. The later GATE `analyze_bench` uses frozen `table2`. A frozen-threshold guarantee for GATE clean self-checks is **ABSENT**. | `blender_visual_qa/truth/run_bench.py:607-621`, `blender_visual_qa/truth/run_bench.py:733-746`, `blender_visual_qa/truth/run_bench.py:935`, `blender_visual_qa/truth/run_bench.py:982-995`, `blender_visual_qa/truth/run_bench.py:1040-1044` |
| Failure propagation | A failed clean self-check raises unit error `E_BUDGET` with rc 6; a flight preserves that rc in its failure record. An incomplete tier returns 5 if any failure has rc 5, otherwise 3, so unit rc 6 is not necessarily the final CLI code. | `blender_visual_qa/truth/run_bench.py:622-623`, `blender_visual_qa/truth/run_bench.py:876-885`, `blender_visual_qa/truth/run_bench.py:913-919`, `blender_visual_qa/truth/run_bench.py:951-957` |
| Missing clean evidence | Actual `analyze_bench` behavior passes no residual for B3/B5 when clean data is absent, so animated-only tiles remain `SHADE_BAND` and clean presence is recorded false. Other benches use noisy temporal beauty residuals when no clean residual is supplied. The B3/B5 path does not fall back to noisy shading truth despite the more general wording in the `truth_labels` docstring. | `blender_visual_qa/truth/run_bench.py:495-500`, `blender_visual_qa/truth/run_bench.py:668-675`, `blender_visual_qa/truth/run_bench.py:687-693`; `blender_visual_qa/truth/labels.py:222-230` |

## Truth labelling

### Inputs, validation and grid

| Input or check | Defined contract | Source at `bridge/integration` |
|---|---|---|
| Frame arrays | Read existing `f%06d.<key>.npy` for `ob,ma,mv,z,n,uv` using `allow_pickle=False`; `ob,mv,z` are required, missing required files raise `E_IO`, rc 2. | `blender_visual_qa/truth/labels.py:43`, `blender_visual_qa/truth/labels.py:51-61` |
| Object pass | `ob`: uint16, two dimensions. | `blender_visual_qa/truth/labels.py:64-71` |
| Motion pass | `mv`: int16, shape `ob.shape+(2,)`. | `blender_visual_qa/truth/labels.py:72-73` |
| Depth pass | `z`: uint32, same shape as ob. Physical depth-unit definition is **ABSENT** from these checks. | `blender_visual_qa/truth/labels.py:74-75`, `blender_visual_qa/truth/labels.py:125-130` |
| Optional arrays | Missing `ma,n,uv` arrays are omitted. Additional dtype/shape checks for them are **ABSENT** from `check_frame`; the shown classification predicates use ob, mv and z. | `blender_visual_qa/truth/labels.py:54-75`, `blender_visual_qa/truth/labels.py:115-137` |
| Metadata | Require nonempty `truth/frames.jsonl`; reject truthy first-row motion_blur with `E_TRUTH_MB`, rc 2; derive Grid from first-row width/height, require its grid identifier and equality, and require consecutive render indices. Repetition of the first-row blur/grid checks on every row is **ABSENT** in `run`. | `blender_visual_qa/truth/labels.py:423-445` |
| Grid dependency | Output order is declared ascending Morton order over existing cells; arrays are reordered using imported grid properties such as `g.flat`. The Grid implementation, default tile-size selection and region-ID algorithm are **ABSENT** from the bound files. | `blender_visual_qa/truth/labels.py:3-4`, `blender_visual_qa/truth/labels.py:18`, `blender_visual_qa/truth/labels.py:147-148`, `blender_visual_qa/truth/labels.py:377`, `blender_visual_qa/truth/labels.py:438` |
| Optional journals | Missing `journal.json` leaves an empty journal; missing `scene.manifest.json` leaves empty name/index maps. | `blender_visual_qa/truth/labels.py:446-457` |
| Object-index validity | If the manifest name map is nonempty, every ob value must be a listed pass index or zero. Invalid values raise `E_TRUTH_QUANT`, rc 2, reporting at most the first eight sorted invalid IDs. | `blender_visual_qa/truth/labels.py:78-83`, `blender_visual_qa/truth/labels.py:458-461` |
| Residual shape | Optional `run` residual must have shape `[number_of_frames,g.N]`; otherwise `E_SCHEMA`, rc 2. Sequence row 0 is unused. | `blender_visual_qa/truth/labels.py:283-298`, `blender_visual_qa/truth/labels.py:423-425`, `blender_visual_qa/truth/labels.py:462-465` |
| Current-frame dimensions | `label_frame` requires current ob shape `(g.H,g.W)`, otherwise `E_STALE`, rc 4. | `blender_visual_qa/truth/labels.py:166-167` |

### Tile label IDs and flags

The label IDs are the zero-based positions of the declared tuple; `EXCLUDED` contains IDs 1, 2 and 8. [`blender_visual_qa/truth/labels.py:22-26`]

| ID | Label | Declared excluded | Source at `bridge/integration` |
|---:|---|---|---|
| 0 | `STATIC` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 1 | `INVISIBLE` | Yes | `blender_visual_qa/truth/labels.py:22-26` |
| 2 | `SUBPIX` | Yes | `blender_visual_qa/truth/labels.py:22-26` |
| 3 | `RIGID` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 4 | `OCCLUDE` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 5 | `DEFORM` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 6 | `SHADE_STRONG` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 7 | `SHADE_WEAK` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 8 | `SHADE_BAND` | Yes | `blender_visual_qa/truth/labels.py:22-26` |
| 9 | `FLICKER_T` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 10 | `FROZEN_T` | No | `blender_visual_qa/truth/labels.py:22-26` |
| 11 | `DRIFT_T` | No | `blender_visual_qa/truth/labels.py:22-26` |

| Flag | Value | Assignment | Source at `bridge/integration` |
|---|---:|---|---|
| `FLAG_EDGE_ONLY` | 1 | Some geo pixels and zero geo_core pixels. This does not necessarily mean every object-ID-only change lies on an edge, because geo_core excludes obchg-only changes. | `blender_visual_qa/truth/labels.py:27`, `blender_visual_qa/truth/labels.py:170-171`, `blender_visual_qa/truth/labels.py:198-201` |
| `FLAG_BG` | 2 | Lowest modal current ob ID is zero. | `blender_visual_qa/truth/labels.py:27`, `blender_visual_qa/truth/labels.py:151-155`, `blender_visual_qa/truth/labels.py:192-195` |
| `FLAG_CAM_MOTION` | 4 | cam_motion argument true, assigned to every tile. | `blender_visual_qa/truth/labels.py:27`, `blender_visual_qa/truth/labels.py:196-197` |
| `FLAG_UNOBSERVABLE` | 8 | Caller mask selects records whose flag bytes are ORed with 8 by `mark_unobservable`. | `blender_visual_qa/truth/labels.py:28`, `blender_visual_qa/truth/labels.py:341-351` |

### Pixel predicates and edge ring

| Parameter or predicate | Exact definition | Source at `bridge/integration` |
|---|---|---|
| Depth sentinel and motion threshold | `BG_Z=0xFFFFFFFF`; `MOV_Q4=8`, annotated as 0.5 pixel; rigid-share threshold 90%; default `EDGE_RING=1` pixel in Chebyshev distance. | `blender_visual_qa/truth/labels.py:29-32` |
| Q4 rounding | Cast to int32, then `sign(q)*((abs(q)+8)>>4)`: nearest integer pixel with half values rounded away from zero. | `blender_visual_qa/truth/labels.py:87-90` |
| mov | Current motion's maximum absolute component, cast to int32, is at least 8. | `blender_visual_qa/truth/labels.py:119-120` |
| Reprojection | `qx=x-rounded_mv_x`, `qy=y-rounded_mv_y`; test inclusion, then clip coordinates for previous-array lookup. | `blender_visual_qa/truth/labels.py:121-126` |
| occ | Outside reprojection, OR previous sampled ob differs from current ob, OR both depths differ from BG_Z and absolute depth difference exceeds `max(10,current_z//100)`. | `blender_visual_qa/truth/labels.py:125-130` |
| anim | All pixels if anim_all; otherwise membership of current ob in a nonempty anim_ob collection; otherwise false. | `blender_visual_qa/truth/labels.py:131-135` |
| obchg | Previous and current ob differ at the same unwarped coordinate. | `blender_visual_qa/truth/labels.py:136-137` |
| geo | `mov OR occ OR obchg`. | `blender_visual_qa/truth/labels.py:170` |
| geo_core | `(mov OR occ) AND NOT edge`; obchg-only pixels do not contribute. | `blender_visual_qa/truth/labels.py:171` |
| Base edge mask | Replicate-pad ob by one pixel; mark each pixel whose ID differs from any of eight neighbors. Direct `ring<=1` returns this mask, so ring=0 does not disable edge marking. | `blender_visual_qa/truth/labels.py:93-103` |
| Wider rings | For `ring>1`, apply `ring-1` repetitions of eight-neighbor dilation with constant padding. | `blender_visual_qa/truth/labels.py:104-112` |
| Filter-derived ring | `None` gives EDGE_RING; otherwise `max(1,int(ceil(float(filter_width)-1e-9)))`. The file-based run uses the first metadata row's filter_width. For the builder's 0.01 value, this expression gives 1. | `blender_visual_qa/truth/labels.py:405-407`, `blender_visual_qa/truth/labels.py:466-468`; `bpy_side/bench_scenes.py:26`, `bpy_side/bench_scenes.py:486` |

### Tile reduction and label precedence

Tile sums zero-pad masks to the grid rectangle, but count real pixels using an all-true image mask, so padding does not inflate the denominator. The tile's object-major ID is its lowest most-frequent current ob value. Outputs are reordered through `g.flat`. [`blender_visual_qa/truth/labels.py:141-155`, `blender_visual_qa/truth/labels.py:169`, `blender_visual_qa/truth/labels.py:190-193`, `blender_visual_qa/truth/labels.py:232-233`]

Let `nn` be the real-pixel count; `ng,ngc,no,nm,na` the counts of geo, geo_core, occ, mov and anim; and `r` the optional residual. The following branch order is normative for this implementation. [`blender_visual_qa/truth/labels.py:169-177`, `blender_visual_qa/truth/labels.py:191-202`]

| Order | Condition | Label behavior | Source at `bridge/integration` |
|---:|---|---|---|
| 1 | `ng==0 and na==0` | STATIC. | `blender_visual_qa/truth/labels.py:203-204` |
| 2 | `ng!=0`, residual present, `r<=t_exit//2`, `na==0` | INVISIBLE. | `blender_visual_qa/truth/labels.py:205-206` |
| 3 | `ng!=0` and either `ngc==0` or `nm==0 and no==0` | SUBPIX. | `blender_visual_qa/truth/labels.py:207-208` |
| 4 | Remaining `ng!=0` | Begin with DEFORM, then apply the rigid/occlusion rules below. | `blender_visual_qa/truth/labels.py:209-221` |
| 5a | Animation-only, no residual | SHADE_BAND. | `blender_visual_qa/truth/labels.py:222-224` |
| 5b | Animation-only, `r>=2*t_enter` | SHADE_STRONG. | `blender_visual_qa/truth/labels.py:225-226` |
| 5c | Animation-only, `r<=t_exit//2` | SHADE_WEAK. | `blender_visual_qa/truth/labels.py:227-228` |
| 5d | Remaining animation-only residual | SHADE_BAND. | `blender_visual_qa/truth/labels.py:229-230` |

Within branch 4, rigid voting requires `no<=nn//100` and at least one non-edge pixel. Form encoded rounded-motion keys from **all non-edge pixels**, not only moving pixels or pixels belonging to the modal object: `key=(dx+512)*1024+(dy+512)`. Select the lowest modal key and decode `(key//1024-512,key%1024-512)`. A nonzero decoded vector with `modal_count*100>=90*core_count` yields RIGID and stores each component clamped to `[-127,127]`. If `no>nn//100`, label OCCLUDE; otherwise keep DEFORM if rigid conditions fail. Validation of a collision-free input range for this motion-key encoding is **ABSENT**. [`blender_visual_qa/truth/labels.py:31`, `blender_visual_qa/truth/labels.py:151-155`, `blender_visual_qa/truth/labels.py:209-221`]

The returned fields are `tl:uint8[N]`, `flags:uint8[N]`, `mv:int8[N,2]`, `ob_major:uint16[N]` and `occ_permille:uint16[N]`. Motion begins zero and is filled only for RIGID; occlusion permille is `n_occ*1000//max(n,1)`, reordered to Morton order. [`blender_visual_qa/truth/labels.py:164`, `blender_visual_qa/truth/labels.py:179-182`, `blender_visual_qa/truth/labels.py:218-219`, `blender_visual_qa/truth/labels.py:232-233`]

### Threshold band, sequence state and temporal overrides

| Parameter | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Direct-call thresholds | `label_frame` and `label_sequence` default to `t_enter=64,t_exit=48`. The file-based `run` path instead calls `thresholds(profile)`. | `blender_visual_qa/truth/labels.py:158-159`, `blender_visual_qa/truth/labels.py:283-284`, `blender_visual_qa/truth/labels.py:466-468` |
| Imported defaults | Normally load `T0_Q4` and `HYST_BAND_PERMILLE` from `visioncore.params.PARAMS`; on any exception in that import/access block, use fallback `32,100`. Actual imported default values are **ABSENT** from this scope. | `blender_visual_qa/truth/labels.py:388-393` |
| Threshold formula | `t0=int(profile.get("T0_Q4",T0_DEFAULT))` with an empty profile if absent; `band=HYST_DEFAULT_PERMILLE*1000`; `T_enter=t0*(1000000+band)//1000000`, `T_exit=t0*(1000000-band)//1000000`. | `blender_visual_qa/truth/labels.py:396-402` |
| Profile keys not used | Free `T_enter,T_exit,HYST_BAND_PERMILLE` keys are not read by thresholds. Under the fallback 32/100 combination, arithmetic gives enter 35, exit 28; these are not guaranteed active engine defaults. | `blender_visual_qa/truth/labels.py:388-402` |
| Stateful hysteresis | A previous-label hysteresis machine is **ABSENT**: every later frame is classified from adjacent truth frames, its residual and thresholds, then journal overrides are applied. The preceding tile label is not an input to `label_frame`. | `blender_visual_qa/truth/labels.py:283-298` |
| First captured frame | Always `static_frame(g,frames[0])`, even when `f0` is nonzero: STATIC labels, zero motion/occlusion, modal object IDs and BG flags. Neither animated events nor temporal overrides are applied to this first frame. | `blender_visual_qa/truth/labels.py:236-248`, `blender_visual_qa/truth/labels.py:283-298` |
| Event lookup | At later sequence index i, render index is `f0+i`. Process only journal events whose frame equals that index: light sets a Boolean, camera sets a Boolean, recognized material/visibility names add their object indices. Transform events are not handled here. | `blender_visual_qa/truth/labels.py:252-265`, `blender_visual_qa/truth/labels.py:288-296` |
| Light scope | If a light event is active and at least one journal lit-name is recognized, union those objects into the animation set and clear the all-light Boolean. Otherwise an active light event applies anim_all to every pixel. | `blender_visual_qa/truth/labels.py:131-135`, `blender_visual_qa/truth/labels.py:291-296` |
| Camera flag scope | Exact camera events set the camera flag; they do not set anim_all or persist a camera state. B5/B5o record only a frame-1 camera event, and the first captured frame is initialized STATIC without that event; the journal does not define continuing camera flags for their later motion. | `blender_visual_qa/truth/labels.py:252-265`, `blender_visual_qa/truth/labels.py:283-298`; `bpy_side/bench_scenes.py:403-422` |
| Temporal selection | For each temporal record in journal order, require inclusive interval `a<=frame<=b`, a recognized name and kind, then select tiles with matching ob_major. Records mutate only labels; later records see earlier changes. | `blender_visual_qa/truth/labels.py:268-280` |
| Flicker override | Kind `flicker` sets FLICKER_T on all selected tiles. | `blender_visual_qa/truth/labels.py:271-279` |
| Frozen override | Kind `frozen` sets FROZEN_T only on selected tiles whose current label is STATIC. | `blender_visual_qa/truth/labels.py:271-279` |
| Drift override | Kind `drift` sets DRIFT_T on all selected tiles. | `blender_visual_qa/truth/labels.py:271-279` |
| Coverage/cut metadata limit | These label helpers read animated, lit and temporal metadata, not the builder's nominal coverage or cut blocks. Truth coverage is computed from final label IDs as specified below; a direct conversion of a nominal B4f coverage record to truth cov_ppm is **ABSENT**. | `blender_visual_qa/truth/labels.py:252-298`, `blender_visual_qa/truth/labels.py:410-420`; `bpy_side/bench_scenes.py:355`, `bpy_side/bench_scenes.py:515-518` |

### Unobservable marking and runner integration

| Operation | Defined behavior | Source at `bridge/integration` |
|---|---|---|
| Binary marking | Parse the blob, convert caller mask to Boolean, require shape `[n_frames,N]`, copy records, OR flag 8 into selected records, and return the original header plus changed records. Shape mismatch raises E_SCHEMA, rc 2; other fields remain unchanged. | `blender_visual_qa/truth/labels.py:28`, `blender_visual_qa/truth/labels.py:341-351` |
| Mask responsibility | The constant's comment names RIGID tiles and diag_moved causes a/b, but `mark_unobservable` does not enforce a label predicate or calculate a cause. Automatic mask derivation in labels.py is **ABSENT**. | `blender_visual_qa/truth/labels.py:28`, `blender_visual_qa/truth/labels.py:341-351` |
| Runner activation | After labels.run and reading the truth tiles, set `unobservable_flagged` according to whether noisy `vision_in/frames.manifest.json` exists. Only then invoke flagging. Without that decoded beauty manifest the labels remain unflagged by this path. | `blender_visual_qa/truth/run_bench.py:495-509` |
| Beauty source | Prefer `clean_0/vision_in/frames.manifest.json` if present, otherwise noisy `vision_in/frames.manifest.json`; load truth arrays using the label result's frame IDs and require equal beauty/truth/label frame counts, otherwise E_STALE, rc 4. | `blender_visual_qa/truth/run_bench.py:524-535` |
| Diagnostic call | Pass RGB frames, truth arrays, zero engine verdict arrays of shapes `[F,N]` and `[F,N,2]`, grid and label list to `diag_moved.diagnose_sequence`. Start an all-false `[F,N]` mask; for sequence indices 1 through F-1, complement the helper's observable mask for diagnostic tiles of that index. | `blender_visual_qa/truth/run_bench.py:512-521` |
| Write-back in runner | Repack via `labels.mark_unobservable`, replace the truth-tile bytes, update the label result's SHA-256 and reread truth tiles. | `blender_visual_qa/truth/run_bench.py:535-540` |
| Unbound diagnostic semantics | The actual definitions of diag_moved causes a/b and its observable-mask algorithm are **ABSENT**. These files define the call boundary and bit update, not the delegated criterion. | `blender_visual_qa/truth/run_bench.py:44`, `blender_visual_qa/truth/run_bench.py:512-537`; `blender_visual_qa/truth/labels.py:28` |

### Serialized truth and summary outputs

| Output | Defined format or reduction | Source at `bridge/integration` |
|---|---|---|
| Truth header | Magic `BVQTRUT1`, version 1, 64 bytes, little-endian format `<8sHHHHHHHHII16s16s`: magic, version, tile size, W, H, grid W/H, N, record size, frame count, first render index, 16 hash bytes, 16 zero bytes. | `blender_visual_qa/truth/labels.py:35-41`, `blender_visual_qa/truth/labels.py:306-308` |
| Truth records | Eight-byte `<BBbbHH`: label, flags, signed-byte motion X/Y, uint16 object-major ID, uint16 occlusion permille; frame-major sequence with g.N records per frame. | `blender_visual_qa/truth/labels.py:38-41`, `blender_visual_qa/truth/labels.py:306-317` |
| Metadata hash binding | First 16 digest bytes of SHA-256 of the exact truth `frames.jsonl` bytes. | `blender_visual_qa/truth/labels.py:302-303`, `blender_visual_qa/truth/labels.py:430-431`, `blender_visual_qa/truth/labels.py:470` |
| Parser validation | Minimum header size, magic/version/record size, optional complete grid match, exact total byte length, max label <=11 and occlusion <=1000. Grid mismatch E_STALE/4; listed schema errors E_SCHEMA/2. Validation of reserved bytes, unknown flag bits or stored hash against an external metadata file is **ABSENT** from the parser. | `blender_visual_qa/truth/labels.py:25`, `blender_visual_qa/truth/labels.py:320-338` |
| Regions | Select non-background modal objects whose label is RIGID, OCCLUDE, DEFORM, SHADE_STRONG, FLICKER_T, FROZEN_T or DRIFT_T. Emit objects sorted by ID; inclusive consecutive-Morton spans; lowest modal label; modal motion over RIGID tiles if available, otherwise all selected object tiles. | `blender_visual_qa/truth/labels.py:151-155`, `blender_visual_qa/truth/labels.py:355-379` |
| Frame coverage | `cov_ppm=count(tl in {3,4,5,6,9,11})*1000000//max(1,total_tiles)`. FROZEN_T is not in this set. `cam_motion` is integer 1 iff any tile flag includes bit 4, otherwise 0. | `blender_visual_qa/truth/labels.py:410-420` |
| Labeller products | Write `tiles_truth.bin`, `frames_meta.jsonl`, `regions_truth.jsonl`; return code, rc, frames, grid, frame_ids and SHA-256 of the packed truth-tile bytes. JSON lines use sorted keys and compact separators. | `blender_visual_qa/truth/labels.py:383-384`, `blender_visual_qa/truth/labels.py:469-481` |

## Render protocol configuration

The JSON declares status `uncalibrated` and an on-disk-GLB turntable protocol. Its literal fields are listed below. These declarations are distinct from the procedural builder and runner defaults: the runner independently defaults to eight frames at 320x180 and caps frames at 60, while the JSON declares 72 frames and scalar resolution 512. The bound Python files define no loading of this JSON into the bench builder/runner; automatic application of the JSON settings is **ABSENT** from this scope. [`config/render_protocol_v1.json:1-8`; `bpy_side/bench_scenes.py:476-504`, `bpy_side/bench_scenes.py:528-543`; `blender_visual_qa/truth/run_bench.py:787-859`, `blender_visual_qa/truth/run_bench.py:1083-1124`]

| Field | Literal value | Source at `bridge/integration` |
|---|---|---|
| `schema` | `"bvqa.render-protocol/v1"` | `config/render_protocol_v1.json:1` |
| `protocol_version` | `"render_protocol_v1"` | `config/render_protocol_v1.json:1` |
| `status` | `"uncalibrated"` | `config/render_protocol_v1.json:1` |
| `input_rule` | `"Renders come only from the on-disk GLB; receipt input_sha256 is the GLB sha256."` | `config/render_protocol_v1.json:2` |
| `blender.engine` | `"CYCLES"` | `config/render_protocol_v1.json:3` |
| `blender.device` | `"CPU"` | `config/render_protocol_v1.json:3` |
| `blender.seed` | `0` | `config/render_protocol_v1.json:3` |
| `blender.use_denoising` | `false` | `config/render_protocol_v1.json:3` |
| `blender.use_adaptive_sampling` | `false` | `config/render_protocol_v1.json:3` |
| `blender.film_transparent` | `false` | `config/render_protocol_v1.json:3` |
| `blender.view_transform` | `"Standard"` | `config/render_protocol_v1.json:3` |
| `turntable.frames` | `72` | `config/render_protocol_v1.json:4` |
| `turntable.step_mdeg` | `5000` | `config/render_protocol_v1.json:4` |
| `turntable.res` | `512` | `config/render_protocol_v1.json:4` |
| `turntable.spp` | `64` | `config/render_protocol_v1.json:4` |
| `turntable.pitch_mdeg` | `15000` | `config/render_protocol_v1.json:4` |
| `turntable.fov_mdeg` | `40000` | `config/render_protocol_v1.json:4` |
| `turntable.fit_num` | `6` | `config/render_protocol_v1.json:4` |
| `turntable.fit_den` | `5` | `config/render_protocol_v1.json:4` |
| `turntable.fps` | `24` | `config/render_protocol_v1.json:4` |
| `turntable.full_turn_mdeg` | `360000` | `config/render_protocol_v1.json:4` |
| `turntable.timeout_s` | `1800` | `config/render_protocol_v1.json:4` |
| `turntable.azimuth` | `"glTF Y-up; azimuth 0 = +Z, increasing toward +X seen clockwise from above; Blender (x,y,z)=(x,-z,y)"` | `config/render_protocol_v1.json:5` |
| `turntable.distance` | `"D = 1.2*R/tan(FOV/2), R = half diagonal of the visible-mesh AABB, target = AABB centre"` | `config/render_protocol_v1.json:6` |
| `turntable.file_pattern` | `"render/%06d.png"` | `config/render_protocol_v1.json:7` |
| `turntable.first_frame` | `1` | `config/render_protocol_v1.json:7` |
| `checks.rerender_seed` | `"frame index = first 8 hex of input_sha256 mod frames + 1"` | `config/render_protocol_v1.json:8` |
| `checks.scale_x_permille` | `1100` | `config/render_protocol_v1.json:8` |
| `checks.truth_frames` | `[1,19,37,55]` | `config/render_protocol_v1.json:8` |

`turntable.timeout_s=1800` is a declared timeout, not an observed timing. This JSON contains values and descriptive rules; enforcement of GLB receipt identity, the turntable camera formulas, rerender selection, scale perturbation and truth-frame checks is **ABSENT** from these four bound files. The `turntable.res` value is recorded here exactly as the scalar 512; a consuming renderer's detailed interpretation is **ABSENT**. [`config/render_protocol_v1.json:1-8`; `bpy_side/bench_scenes.py:476-543`; `blender_visual_qa/truth/run_bench.py:316-363`, `blender_visual_qa/truth/run_bench.py:787-859`]

## ABSENT: scope limits

| Behavior not established | Boundary evidence at `bridge/integration` |
|---|---|
| Cycles seed-mixer arithmetic, engine S1 residual algorithm, effective engine parameter values, grid construction/tile-size defaults, full calibrator criteria and diag_moved cause definitions. | Imports/calls only: `blender_visual_qa/truth/run_bench.py:35-44`, `blender_visual_qa/truth/run_bench.py:211-225`, `blender_visual_qa/truth/run_bench.py:488-492`, `blender_visual_qa/truth/run_bench.py:512-521`, `blender_visual_qa/truth/run_bench.py:862-864`, `blender_visual_qa/truth/run_bench.py:1053-1058`; `blender_visual_qa/truth/labels.py:18`, `blender_visual_qa/truth/labels.py:388-402` |
| Truth-pass rendering/quantization/decoding internals, exact RANK_TYPES membership and complete RAM-workspace retention internals. | `bpy_side/bench_scenes.py:20-21`, `bpy_side/bench_scenes.py:465-473`; `blender_visual_qa/truth/run_bench.py:39-40`, `blender_visual_qa/truth/run_bench.py:126-165`, `blender_visual_qa/truth/run_bench.py:341-357` |
| Explicit curve extrapolation, explicit checker Vector mapping, unassigned camera/Principled/render defaults, universal pixel-level seed uniqueness or guaranteed bench classifications. | `bpy_side/bench_scenes.py:41-44`, `bpy_side/bench_scenes.py:124-199`, `bpy_side/bench_scenes.py:269-274`, `bpy_side/bench_scenes.py:433-437`, `bpy_side/bench_scenes.py:476-504`; `blender_visual_qa/truth/labels.py:158-233` |
| A stateful temporal label detector, persistent camera-event state, direct nominal-coverage-to-truth conversion or an enforced RIGID-only predicate inside mark_unobservable. | `blender_visual_qa/truth/labels.py:252-298`, `blender_visual_qa/truth/labels.py:341-351`, `blender_visual_qa/truth/labels.py:410-420` |
| Automatic adoption or runtime enforcement of the independent GLB turntable JSON by the truth-bench pipeline. | `config/render_protocol_v1.json:1-8`; `bpy_side/bench_scenes.py:476-543`; `blender_visual_qa/truth/run_bench.py:316-363`, `blender_visual_qa/truth/run_bench.py:787-859`, `blender_visual_qa/truth/run_bench.py:1083-1124` |
