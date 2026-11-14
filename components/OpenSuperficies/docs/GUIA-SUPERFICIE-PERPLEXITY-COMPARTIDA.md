# Shared Perplexity surface · handover for another metaorchestrator

Audience: a metaorchestrator of a **different** campaign that needs web research through Perplexity.
Emitter: `opus5-estratega` (campaign `opensuperficies-nextgen`) · 2026-09-18 · every value below is [MEASURED] on this host.
Language: work in English, like every agent in these campaigns.

## 1. What this is

A Docker + Xpra browser capsule, owned by the operator, with a **logged-in Perplexity session** in a persistent profile.
You drive it by CDP through a domain script. You never touch credentials and you never see the session.

| fact | value |
|---|---|
| instance name | `perplexity` |
| container | `newton-srf-perplexity` (image `opensuperficies-browser-arm64:1.0.0`) |
| CDP | `127.0.0.1:9240` |
| Xpra viewer | `127.0.0.1:10020` (operator's window; not yours) |
| state | `READONLY=yes` (blindada: automation drives, the human does not) |
| session | `autenticado: true`, profile persisted at `staging/superficies/perplexity/perfil` |
| owner | `operador-humano`. It is declared in `OpenSuperficies/_estado/residuos/declarados.json` and is **not** a residue |

## 2. The one command you need

```bash
/home/user/OpenSuperficies/scripts/perplex-box.sh consultar perplexity '<question in English>' [destino.md]
```

- Default destination: `staging/superficies/perplexity/cosecha/<UTC timestamp>-<pid>.md`
- `BUDGET_S=900` by default (seconds). Raise it for a deep question: `BUDGET_S=1800 ... consultar ...`
- To attach a file to the query: `ADJUNTO=/absolute/path ... consultar ...` (it is staged into `/uploads` and the attachment gate runs)
- List what you have harvested: `scripts/perplex-box.sh cosechar perplexity`

Ask for the documented semantics of the exact version you are pinning, and for primary sources. Quote what you use.

## 3. Mutual exclusion — mandatory when two campaigns share this surface

The script already takes an admission token (`flock` over `$XDG_RUNTIME_DIR/newton-surface/paint.N.lock`,
`SLOTS_PINTANDO_MAX=4` by default), but that token allows **four concurrent painters on the host**. Two metaorchestrators
querying the same browser at the same time would interleave in one DOM. Do not rely on the default.

**Wrap every query in this lock. No exceptions:**

```bash
flock -w 1200 /run/user/1000/newton-surface/perplexity.campana.lock \
  /home/user/OpenSuperficies/scripts/perplex-box.sh consultar perplexity '<question>'
```

- `-w 1200` waits up to 20 minutes and then gives up with a non-zero exit. A timeout is **not** a failure of your unit:
  treat it as a busy resource, record it, and either retry later or fall back to your own web search.
- Never use `flock -n` in a loop that retries immediately: a query can legitimately take minutes.
- Never hold the lock across anything but the single `consultar` call.

If you want belt and braces, also force the surface to a single painter for your call: `SLOTS_PINTANDO_MAX=1`.

## 4. Exit codes — read them literally

| rc | meaning | what you do |
|---|---|---|
| `0` | OK, answer written to the destination | read the destination file |
| `4` | PARTIAL: the end-of-stream gate never saw the end | the file holds what arrived; re-ask only if it is unusable |
| `6` | AMBIGUOUS: the query may have been sent **and charged**. Sealed `needs_human` | **never retry.** Report it to the operator and move on |
| `7` | REQUIRES HUMAN (a challenge, a login wall) | stop. The operator takes the wheel with `docker-xpra-surface.sh intervenir perplexity`. Do not attempt to solve a challenge |

## 5. Before your first query

```bash
/home/user/OpenSuperficies/scripts/docker-xpra-surface.sh estado perplexity
```

Expect `SALUD_CONTENEDOR=running`, `READONLY=yes` and a final JSON line with `"autenticado": true`.
If `autenticado` is `false`, the session expired: **stop and ask the operator**. Never try to log in.

## 6. Hard rules

You may: run `consultar`, run `cosechar`, read files under `staging/superficies/perplexity/cosecha/`.

You may **not**:

- restart, stop, recreate or `purgar` the capsule, or change its profile or its ports;
- run `intervenir` or `blindar` (that is the operator's switch between human and automation);
- read, copy or move `staging/superficies/perplexity/perfil` — it holds the operator's session;
- touch `_estado/residuos/declarados.json` in the OpenSuperficies repository;
- drive the Xpra viewer on `:10020`, which is the operator's window;
- open a second capsule against Perplexity. One surface, one session, one lock.

## 7. If your campaign has a residue census

This container, its network `newton-srf-perplexity` and the ports 10020 and 9240 belong to a foreign campaign.
If your census counts containers by image, by name pattern `newton-srf-*`, or listeners in 10020–10099 / 9220–9299,
declare them as an external surface before your first wave barrier, or your barrier will go red on a resource that is
not yours to clean. In OpenSuperficies that declaration lives in `_estado/residuos/declarados.json`; copy the shape.

## 8. When it looks broken

| symptom | cause measured here | action |
|---|---|---|
| viewer shows a fixed blue screen | the Xpra layer offers only the `lz4` compressor and the HTML5 client cannot decode | operator-side only; your CDP path is unaffected |
| `superficie 'perplexity' no activa` | the capsule is down | ask the operator to start it: `docker-xpra-surface.sh iniciar perplexity https://www.perplexity.ai/` |
| rc 7 on every query | a login wall or a challenge | operator takes the wheel; never bypass a challenge |

## 9. What to bring back

Cite what you used. If research shows your plan or your product could be more robust, more secure or more
maintainable, record it as an improvement proposal in your own room and keep working — that is what this surface is
for, and it is cheaper than a second failed attempt.
