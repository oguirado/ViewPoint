<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# ViewPoint Studio production

Use Python 3.11 or newer. Media commands use FFmpeg and FFprobe. Boards, the asset catalogue, and pixel checks also use Pillow and NumPy where indicated by imports. The CPU Blender profile needs Blender, bubblewrap and the systemd user manager on Linux. The media tools pin `LC_ALL=C.UTF-8`, `LANG=C.UTF-8`, and `TZ=UTC` in child processes. Use fresh work directories for commands that publish a directory. The episode executor and series exporter can reuse matching committed results.

## Discover an installed capability

```sh
./install.sh "$STUDIO_PREFIX"
studio="$STUDIO_PREFIX/bin/viewpoint-studio"
"$studio" --list-capabilities
"$studio" --mcp-manifest
"$studio" --dry-run --expect-registry "$REGISTRY_SHA256" < request.json
"$studio" --run --expect-registry "$REGISTRY_SHA256" --artifact-root "$ASSETS" \
  --profile "$PROFILE" --profile-sha256 "$PROFILE_SHA256" --require-qualified < request.json
```

Discovery reports declared, dry-run, runnable and independently qualified status for each slot. A profile supplies executable and interpreter pins, available slots and independent qualification receipts. The registry pins the schemas under `lib/viewpoint-studio/slots`. For MCP, configure the installed executable with arguments `["--mcp", "--expect-registry", "SHA256"]`; add the profile and artifact arguments for provider execution. The stdio tools are `vp_studio_capabilities`, `vp_studio_dry_run`, and `vp_studio_run`. The last two take `{"request": {…}}`. A refusal includes `reason`, `retry_when` and `next_command`.

## Execute an episode

```sh
"$studio" --episode --list-capabilities
"$studio" --episode --artifact-root "$ASSETS" --episode-state-root "$STATE" \
  --episode-archive-root "$ARCHIVE" --episode-restore-root "$RESTORE" \
  --episode-policy "$POLICY" --episode-policy-sha256 "$POLICY_SHA256" < episode-request.json
```

Use four disjoint existing directories. Put a `viewpoint.episode-recipe/v1` under the asset root. Submit `{"schema":"viewpoint.episode-request/v1","request_id":"episode-1","op":"af.episode","args":{"recipe":"recipe.json","sha256":"<recipe SHA-256>"}}`. The policy binds each shot's scene, clip and camera and authorizes effects and limits. Resubmit identical parameters to validate or continue committed work. Use `af.episode.status` with the same request ID and arguments to read its state. The resulting archive and fresh restore are digest-verified. See `tests/fixtures/episode/make.py` for a complete recipe and policy.

For registered contact shots using a catalogue and SET producer, use the adapter below. The set producer is executable code. Supply it from your trusted catalogue and run it in the local background-Blender scope.

```sh
python3 -B "$STUDIO_PREFIX/adapter/episode.py" resolve --library "$CATALOGUE" --shot-list shots.json --out resolved.json
python3 -B "$STUDIO_PREFIX/adapter/episode.py" bind --library "$CATALOGUE" --resolved resolved.json \
  --intent edit.json --material-name "$ACTOR_MATERIAL" --out bound.json
python3 -B "$STUDIO_PREFIX/adapter/episode.py" render --bound bound.json --sha256 "$BOUND_SHA256" --out renders
python3 -B "$STUDIO_PREFIX/adapter/episode.py" reopen --bound bound.json --sha256 "$BOUND_SHA256" --renders renders --out readback
python3 -B "$STUDIO_PREFIX/adapter/episode.py" conform --bound bound.json --sha256 "$BOUND_SHA256" --renders renders --out cut
```

## Editorial and short assembly

```sh
python3 -B "$STUDIO_PREFIX/editorial/editorial.py" discover
python3 -B "$STUDIO_PREFIX/editorial/editorial.py" build --project "$PROJECT" --out "$DELIVERY"
python3 -B "$STUDIO_PREFIX/editorial/conform.py" --timeline "$DELIVERY/timeline.json" --out "$REVISION"
python3 -B "$STUDIO_PREFIX/short/assemble.py" --timeline "$SHORT_EDIT" --out "$SHORT_DELIVERY"
```

The editable project roster is in `editorial/schemas.json` and `tests/fixtures/editorial`. The build emits boards, blocking, camera and light bindings, lossless source shots, cue sheet, stems, captions, timeline and picture conform. The short timeline uses `short/edit.schema.json` (`viewpoint.short-edit/v1`), integer `fps`, even `pixels`, ordered `shots` with an `id`, zero-based half-open `source_cut` and `media` pin, a `mix` pin and a `captions` list of `{start,end,text}` edit-frame ranges. A media pin is `{path,sha256}` with a path relative to the timeline. The deliverable contains a lossless picture archive, H.264/AAC master with selectable English captions, an SRT and a digest receipt.

## Search, curate and reuse assets

```sh
db="$STUDIO_PREFIX/library/asset_db.py"
python3 -B "$db" --library "$CATALOGUE" index --root "assets=$ASSETS" --jobs 1
python3 -B "$db" --library "$CATALOGUE" find 'press switch'
python3 -B "$db" --library "$CATALOGUE" assemble shots.json --out resolved.json
python3 -B "$db" --library "$CATALOGUE" sync --remote "$RCLONE_REMOTE" --root "$REMOTE_FOLDER" --dry-run
```

Use `show` for metadata and `resolve` for current bytes. The optional sync plan emits immutable rclone argv for a caller-configured remote name ending in `:`. Use `stage --plan PLAN --index INDEX --destination DIR` for local readback before transfer. These verbs perform zero network calls.

Run `library/library.py --snapshot DIR --catalog-sha256 SHA --index FILE build` to build the curation FTS index, then use the same flags with `find QUERY` or `show ID`. Pass `--trust-root`, `--authority` and `--destination` together, and `build --admission FILE --admission-sha256 SHA`, to enable destination-specific approval. The catalogue remains unverified until approval binds the exact asset, origin, rights and verdict evidence. `assemble RECIPE --receipt FILE` also verifies approved clips, rig/rest bindings and the dependency closure.

Use `library/collection.py stage --help` to package distinct recipes with a shared admitted character, and `cold-resolve PACKAGE --sha256 SHA` to verify and resolve the package from its own bytes. `library/reuse_proof.py cold-build --help` accepts a pinned staged build plan with relative input paths. Use `compare --help` to compare two pinned frame outputs with an explicit pixel tolerance.

## Check series continuity and restore a package

```sh
series="$STUDIO_PREFIX/series/series.py"
python3 -B "$series" check --project "$BIBLE"
python3 -B "$series" rights --project "$BIBLE" --policy "$POLICY" \
  --policy-sha256 "$POLICY_SHA256" --evidence "$EVIDENCE" --destination "$DESTINATION"
python3 -B "$series" export --project "$BIBLE" --output "$PACKAGES" --policy "$POLICY" \
  --policy-sha256 "$POLICY_SHA256" --evidence "$EVIDENCE" --destination "$DESTINATION"
```

Supply the export receipt's `snapshot_sha256` to `import --archive FILE --snapshot-sha256 SHA --into NEW_DIR` with the same policy, evidence and destination. Use `check --readback FILE` to match an adapter readback against the bible. For a configured destination with a mounted Drive backend, pass `--drive-root MOUNT`. `tests/fixtures/series/fixture.py` builds a runnable local example with fixture-only rights.

## Review an exact movie

```sh
review="$STUDIO_PREFIX/review/rp_review.py"
python3 -B "$review" declared --movie-sha "$MOVIE_SHA256" --contacts contacts.json --out declared.json
python3 -B "$review" build --movie "$MOVIE" --sha256 "$MOVIE_SHA256" --declared declared.json --pack "$PACK"
python3 -B "$review" verify --pack "$PACK" --movie "$MOVIE" --sha256 "$MOVIE_SHA256"
python3 -B "$review" replay --pack "$PACK"
```

Declare the inclusive zero-based `hand_button` interval and any `foot_plant` intervals with `first_index` and `last_index`. The declared document binds the candidate digest. `plan` reports the blind segments. Configure a protocol with pinned rubric and blind context files, independent primary seats, a reserve and a judge. Pass `--protocol FILE --seat-registry DIRECTORY --ledger FILE` before the verb and a separate `--root WORKSPACE` to panel verbs. Use `register`, `brief`, `technical`, `blind-ctx`, `seal`, `card-ctx`, `accept`, `judge` and `score` to preserve digest bindings across each stage. For explicit observer execution, supply an executable `--observer` adapter. Use only the results that the same pinned movie and complete panel evidence authorize.
