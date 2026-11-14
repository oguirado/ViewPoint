# CONSUME: how Antigravity Track H uses the rewindable capsule without relabelling it

Track P (Claude, claude-fable-5-2) owns `product/ref_rewind*`. Track H (Gemini, `swarm/`) never copies or edits these
files; it calls them as an external tool or dispatches their emitted scripts as data. Contract: `product/ref_rewind/CONTRACT.md` (rw1).

## Option A: call the frozen CLI (broker = ours)

```sh
CTL="python3 product/ref_rewind_demo/rewindctl.py"
RT=/dev/shm/vqa-cu/<your-rt>; N=<nonce shared with the bridge>
# Blender must run product/ref_broker/bridge.py with VP_SWARM_RT=$RT VP_SWARM_NONCE=$N XDG_CONFIG_HOME=$RT/xdg
VP_SWARM_NONCE=$N $CTL run --job <vision job> --rt $RT --turns 12     # rc 0 idle, 3 FROZEN/RECONCILE
$CTL status --rt $RT                                                  # no Blender contact
$CTL audit  --rt $RT                                                  # rc 0 clean, 1 problems
VP_SWARM_NONCE=$N $CTL rewind --job <job> --rt $RT --to last_good --resume   # director only; leaves FROZEN
VP_SWARM_NONCE=$N $CTL export --job <job> --rt $RT --tick <T>         # checkpoint .blend for ticks <= confirmed_tick
VP_SWARM_NONCE=$N $CTL mark   --job <job> --rt $RT                    # extra point of the confirmed state
```

Agents write intents to `RT/intents/<agent_id>/<event_id>.json` (tmp + rename), as in the F1 contract, plus the optional rw1 fields:

```json
{"v":1,"rec":"enqueue","event_id":"e1","agent_id":"props-agent","lane":"script","based_on_frame":1,
 "write_set":["col:PROPS_set"],"script":"<bpy a1>","alternatives":["<bpy a2>"],
 "expect":{"probe":"<read-only bpy script; last stdout line: RW_RESULT {\"value\": ...}>","equals":true}}
```

Rules that bite: create the claimed collections before the first turn (linking one changes `col:Scene Collection`); an agent
intent with `op`, `rec:"control"` or a `ctl.*` id closes `NO_LEASE`; after any rewind every queued gui intent closes
`STALE_FRAME` and is resubmitted under a new event id; an event whose alternatives were all rolled back closes `ALL_REJECTED`.

## Option B: your H1 broker dispatches our scripts

```sh
$CTL emit-script mark    --point-path $RT/ckpt/p7.blend > mark.py
$CTL emit-script restore --point-path $RT/ckpt/p7.blend --sha256 <blend sha256> > restore.py
$CTL emit-script digest  > digest.py
```

Each script needs only `bpy`, runs on Blender's main thread (your bridge), and prints as its last stdout line
`RW_RESULT {"op":"mark|restore|digest","digest":{...},...}`. Your broker must: (1) dispatch them as director control jobs,
never on an agent's behalf; (2) compare the digest of a restore answer with the digest stored at mark time and freeze on
a mismatch; (3) bump its own epoch on every restore and close queued gui intents as stale; (4) append the four record types
below to its own log if it wants `rewindctl audit --events <file>` to judge it.

## Records to render (overlay strip, TaskLight)

| Record | Fields to show | Suggested UI |
|---|---|---|
| `{"rec":"ckpt","point_id","tick","scene_epoch","blend_sha256","bytes","digest","frame"}` | `point_id`, `tick` | checkpoint tick on the timeline |
| `{"rec":"txn","phase":"begin\|commit\|rollback","event_id","attempt","base_point","verdict","reason","point","tick"}` | `attempt`, `phase`, `verdict` | card state: trying / committed / rolled back |
| `{"rec":"judge","attempt","pick","oracles":[{"name","ok","detail"}]}` | failed oracle `name` + first token of `detail` | reason chip (`expect`, `delta`, `witness`) |
| `{"rec":"control","op":"rewind","to","by","scene_epoch_after","digest_match","attempt"}` | `to`, `digest_match` | overlay strip "REWIND → p1"; red if `digest_match:false` |

Real samples: `evidence/nominal/events.jsonl` (seq 18-25 is the reject → rewind → commit sequence); failure samples:
`evidence/mutant_no_restore/events.jsonl` (`digest_match:false`, `control stop`), `evidence/mutant_judge_accept_all/events.jsonl`.
A renderer written against these is Gemini work with `CONTRACT.md` as a listed Claude design input.

## Provenance

| Unit | Directory | Branch |
|---|---|---|
| F1 | `product/ref_broker` | `p-fable-f1` |
| R0 | `product/ref_rewind` | `p-fable-r0` |
| R1 | `product/ref_rewind_ckpt` | `p-fable-r1` |
| R2 | `product/ref_rewind_lanes` | `p-fable-r2` |
| R3 | `product/ref_rewind_verify` | `p-fable-r3` |
| R4 | `product/ref_rewind_demo` | `p-fable-r4` (contains all of the above merged) |

All rows: Track P, `claude-fable-5-2`. Recording the demo through Option A and showing it in a Track H video is use of an
external tool; state it as "rewind capsule: Track P reference (Claude)" wherever the video credits components.
