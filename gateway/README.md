# ViewPoint Cloud Run gateway (H06)

A small, self-contained Python 3.12 service for the hackathon demo. It serves
one page with two things side by side:

1. a **REPLAY** of a recorded ViewPoint capsule session — frames plus an
   ordered engine timeline, loaded from a versioned JSON document in
   `replay/`; and
2. a **Gemini plan** panel — the server calls the Gemini REST API with the
   recorded task and step list, validates the response, and shows the new plan
   and its stated rationale next to the recorded actions.

The shipped replay is a **SAMPLE**: synthetic frames and actions created for
gateway development, not a recorded product run. The page says so in the
header, in every frame, in the footer and in the API payload.

This service is replay-only. It does **not** actuate a browser, open a host
tunnel, accept credentials, or expose a shell. The live host remains local.

## Files

| Path | Purpose |
|---|---|
| `app.py` | stdlib HTTP server, routes, page bootstrap, static assets, security headers |
| `gemini_client.py` | Gemini REST client, prompt, response parsing, missing-key client |
| `replay_store.py` | strict loader/validator for `viewpoint.replay/v1`, frame SHA-256 checks |
| `plan_schema.py` | closed ingress schema for the model-authored plan; marks refs as matched/partial/unmatched |
| `templates/index.html` | the single page shell |
| `static/app.css`, `static/app.js` | replay viewport, timeline, plan panel, key-error handling |
| `static/frames/*.svg` | synthetic SAMPLE frames with no private data |
| `replay/h06-sample-v1.json` | versioned SAMPLE replay document |
| `tests/test_app.py` | unittest suite: page, replay endpoint, missing key, no key leakage, fakes |
| `Dockerfile` | Cloud Run image, listens on `$PORT` (default 8080), runs as non-root |
| `requirements.txt` | deliberately empty of third-party packages: stdlib only |

## Run locally

```bash
cd gateway
python3 -m unittest discover -s tests -v
python3 app.py --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>.

Without `GEMINI_API_KEY`, the replay still works and the plan panel shows the
clear missing-key error. To exercise a real Gemini call locally:

```bash
# Do not paste the key into a file in this directory or into source.
read -r -s -p 'Gemini API key: ' GEMINI_API_KEY; echo
export GEMINI_API_KEY
export GEMINI_MODEL=gemini-2.5-flash
python3 app.py --host 127.0.0.1 --port 8080
```

The page never calls `/api/plan` on load: the replay renders first and Gemini is called only when the visitor clicks "Generate plan" (no retries).
The model identifier is configurable and must be recorded from the qualified
deployment; `gemini-2.5-flash` is only the default placeholder.

### Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests make **no external network calls**. They bind a local loopback
server, use a fake Gemini client for the happy path, and use injected fake
openers for the REST and secret-boundary tests. They check:

- the page renders `REPLAY` / `SAMPLE` labels and the replay bootstrap;
- `/api/replay/<id>` returns the versioned document and its SHA-256 header;
- frame bytes match the hashes recorded in the replay JSON;
- the missing-key path returns `GEMINI_KEY_MISSING` with a clear message;
- a fake Gemini plan is schema-validated and matched against recorded steps;
- unknown references are marked `unmatched` / “no matching execution”;
- provider and invalid-plan failures are visibly errors, never canned plans;
- a sentinel key value never appears in responses or logs, and the REST client
  sends it only in the `x-goog-api-key` header (not URL or body);
- tampered frame bytes and path traversal are rejected.

## API

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/` | page with replay + Gemini panel |
| `GET` | `/api/replays` | indices, versions, step/frame counts, replay SHA-256 |
| `GET` | `/api/replay/<replay_id>` | validated replay JSON; `X-Replay-SHA256` header |
| `POST` | `/api/plan` | body `{"replay_id":"h06-sample-v1"}`; server-side Gemini call |
| `GET` | `/healthz` | liveness, replay count, whether Gemini is configured, model label |

Plan failures are explicit:

- `503 GEMINI_KEY_MISSING` — `GEMINI_API_KEY` absent;
- `502 GEMINI_PROVIDER_ERROR` — provider unreachable, HTTP error, blocked or truncated;
- `502 GEMINI_INVALID_RESPONSE` — response failed the closed plan schema.

The page never receives an API key, and the server never logs request headers,
bodies, or the key.

## Deploy to Cloud Run

These commands use placeholders. Replace `YOUR_PROJECT_ID` and `YOUR_REGION`
(for example `us-central1`). Run from this directory.

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=YOUR_REGION
export SERVICE=viewpoint-gateway
export SECRET=gemini-api-key
export MODEL=gemini-2.5-flash

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  generativelanguage.googleapis.com

# Create the secret once. The value is read with no echo and never stored here.
read -r -s -p 'Gemini API key: ' GEMINI_API_KEY; echo
if gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add "$SECRET" --data-file=-
else
  printf '%s' "$GEMINI_API_KEY" | gcloud secrets create "$SECRET" \
    --data-file=- --replication-policy=automatic
fi
unset GEMINI_API_KEY

# Allow the Cloud Run runtime service account to read the secret.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:${PROJECT_NUMBER}<redacted-email>" \
  --role="roles/secretmanager.secretAccessor"

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 1 \
  --concurrency 5 \
  --timeout 60 \
  --set-env-vars "GEMINI_MODEL=${MODEL},PLAN_DAILY_CAP=200,TRUST_PROXY=1" \
  --set-secrets "GEMINI_API_KEY=${SECRET}:latest"
```

`--source .` builds the included Dockerfile with Cloud Build and deploys it.
If your organization disables the default compute service account, create a
runtime service account and pass `--service-account`. Grant that account
`roles/secretmanager.secretAccessor` on the secret instead.

Get the URL and verify the deployed service:

```bash
export URL=$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')

curl -fsS "$URL/healthz" | python3 -m json.tool
curl -fsS "$URL/api/replay/h06-sample-v1" | python3 -m json.tool | head -n 20
curl -fsS -X POST "$URL/api/plan" \
  -H 'Content-Type: application/json' \
  -d '{"replay_id":"h06-sample-v1"}' | python3 -m json.tool
```

The last command performs a real, billed Gemini call. It should return a
validated plan. If the key is missing, it must return the visible
`GEMINI_KEY_MISSING` error instead of a fake success.

### Optional explicit image build

```bash
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/viewpoint/${SERVICE}:$(date +%Y%m%d-%H%M%S)"
gcloud artifacts repositories create viewpoint \
  --repository-format=docker --location="$REGION" --project "$PROJECT_ID" || true
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --min-instances 0 --max-instances 1 --concurrency 5 --timeout 60 \
  --set-env-vars "GEMINI_MODEL=${MODEL},REPLAY_REQUIRE_PIN=1,REPLAY_PINS=${REPLAY_PINS}" \
  --set-secrets "GEMINI_API_KEY=${SECRET}:latest"
```
Every deploy path must keep `--max-instances 1` (the rate bucket and daily cap live in process memory) and pin the served replays (`REPLAY_PINS="replay_id=sha256,..."`, `REPLAY_REQUIRE_PIN=1`).

## AI Studio app and share link

AI Studio's Build interface changes over time; the labels below are the
current path, and the required artifacts are stated so the step can be
completed if a label moves.

1. Open <https://aistudio.google.com/> and sign in with the operator account.
2. Create or select the Google Cloud project that will own the Gemini API key.
   In AI Studio, use **Get API key** / **API keys** for that project
   (<https://aistudio.google.com/app/apikey>). Do not paste the key into any
   file in this repository.
3. Open **Build** (<https://aistudio.google.com/apps>, or the Build entry on
   the AI Studio home) and choose **New app**.
4. Import this directory with **Import code** / **Upload** (or connect the
   repository subfolder if GitHub is offered). If the importer only accepts a
   single file, upload the Python files, `templates/`, `static/`, `replay/`,
   `requirements.txt` and `Dockerfile`; the app is self-contained.
5. Set the runtime to Python 3.12, the start command to `python app.py`, and
   the port to `8080`. Add `GEMINI_MODEL` as a normal environment value and
   `GEMINI_API_KEY` as a **secret**, not plaintext.
6. Choose **Deploy to Cloud Run** (or deploy with the gcloud commands above
   and point the AI Studio app at the resulting `*.run.app` URL). Record the
   deployed URL.
7. Use **Share** in AI Studio, set access for the demo (for example “anyone
   with the link”), and copy the sharing link. Verify the page and the
   `*.run.app` URL while logged out.
8. If Build cannot import a multi-file server app, use the gcloud deploy path
   for the running service and use AI Studio **Share** for the app/code
   surface. Record which path was used in the submission receipts; do not
   claim an AI Studio link that does not open logged out.

The share link must contain only this demo surface: no `.env`, no key, no
private repository history and no recorded user data.

## Replacing the SAMPLE with a real capture

Add a new file under `replay/` with a unique `replay_id`; the service loads
all `*.json` files at startup and shows a session picker when there is more
than one. Set `DEFAULT_REPLAY_ID` to choose the initial session.

The `viewpoint.replay/v1` document is validated strictly:

- `schema` is exactly `viewpoint.replay/v1`;
- `mode` is `REPLAY`, `labels.live` is `false`, and `sample` is truthful;
- frames are ordered by `t_ms`, each `src` stays under `/static/frames/`, and
  each `sha256` must match the frame bytes on disk;
- steps are ordered by `t_ms`; each step references an existing frame;
- actors are `operator`, `engine`, `planner` or `system`; statuses are
  `verified`, `refused`, `pending` or `observed`;
- the top-level and per-step field sets are closed. Add a field only with a
  schema version bump and a matching validator change.

For an H08 capture:

```bash
cp /path/to/scrubbed-frames/*.svg static/frames/
sha256sum static/frames/*.svg
# Insert the hashes into replay/<id>.json, set "sample": false and a real
# sample_notice / provenance, then restart the service.
```

Do not label a synthetic sample as `sample: false`, and do not remove the
`SAMPLE` badge from the page while the document says `sample: true`.

## Security and operating notes

- The Gemini key is read only from `GEMINI_API_KEY` (Cloud Run maps it from
  Secret Manager). It is sent only in the `x-goog-api-key` request header.
  It is never in the page, browser JavaScript, URL, request body or logs.
- The prompt asks Gemini for a short **stated rationale** (two to four
  sentences), not hidden chain-of-thought. The page labels it that way.
- Provider errors are sanitized; no exception text is returned to the client.
- CSP, `X-Frame-Options: DENY`, `nosniff`, no-referrer and same-origin resource
  policy are set by the server. There is no CORS endpoint.
- Request bodies are limited to 8 KiB; Gemini responses are size-limited and
  schema-checked. Unknown plan fields are rejected.
- The page is public by default with `--allow-unauthenticated` for the
  hackathon demo. Use `--no-allow-unauthenticated` for a private rehearsal.
- Billing state is per process: `PLAN_DAILY_CAP` and the per-IP rate buckets
  are in-memory in one instance. With `--max-instances 1` and bounded
  `--concurrency 5`, `PLAN_DAILY_CAP` (set above to 200) is the single global
  daily cap for the deployment. Do not raise `--max-instances` without also
  moving quota to shared storage (for example Redis or Firestore), or the cap
  silently multiplies per instance.
- Cloud Billing budget alert (required for a public deployment): create a
  budget on the demo project so Gemini and Cloud Run charges page you before
  they burn a full cap of billed calls.

```bash
# Alert at 50% and 90% of a 25 USD budget on the project.
gcloud billing budgets create \
  --project "$PROJECT_ID" \
  --billing-account="$(gcloud billing accounts list --format='value(accounts/[0].accountId)')" \
  --display-name="ViewPoint gateway demo guardrail" \
  --budget-amount=25USD \
  --budget-alert-thresholds=0.5,0.9 \
  --budget-alert-email-contacts=operator@example.com
```

- Every Generate plan click triggers one Gemini call; nothing is billed until
  a user clicks. Set a Cloud Billing budget/alert as above.
- The replay contains no credentials, real emails, private paths or live
  session data. Review every frame and JSON field before publishing.

## H06 acceptance status

- [x] local page with REPLAY + SAMPLE labels;
- [x] versioned replay endpoint with frame-hash verification;
- [x] server-side Gemini client with a fake-client test path;
- [x] visible missing-key error and provider/invalid-plan errors;
- [x] tests prove no key value appears in responses or logs;
- [x] Dockerfile listens on `$PORT` and runs as non-root;
- [ ] real deployed Gemini call — needs the operator's GCP project, billing,
  Gemini API key and deployment receipt;
- [ ] AI Studio share link — needs the operator's AI Studio account;
- [ ] replacement with an H08 scrubbed capture — H08 dependency.

The missing items stay HOLD; this README does not claim them as done.

## Client identity, rate limits and capacity (H06b)

- **Default (`TRUST_PROXY` unset):** the rate bucket key is the TCP peer. On Cloud Run the peer is Google's front end, so **every client shares one global bucket** (`PLAN_RATE_CAPACITY`, default 10; `PLAN_RATE_REFILL`, default 1/6 s), alongside the per-process `PLAN_DAILY_CAP`. Size these as service-wide limits, and set `DEMO_TOKEN` so that one anonymous script cannot drain the shared bucket. `/healthz` reports `rate_scope: "global_peer"`.
- **`TRUST_PROXY=1`:** the key is the **last** `X-Forwarded-For` entry, the one appended by Cloud Run's front end. Earlier entries and `X-Real-IP` are ignored. Enable it only when the service is reachable solely through Cloud Run's front end. If an external HTTP(S) load balancer sits in front, the last entry is the balancer, and you are back to one global bucket.
- **Concurrency:** worker slots (`MAX_WORKERS`, default 80) are held per TCP connection. Deploy with `--concurrency` ≤ `MAX_WORKERS`. Idle keep-alive connections release their slot after `KEEPALIVE_IDLE` (default 2 s). Excess connections get `503` + `Retry-After: 1`.
- **Replay attestation:** frame hashes, step `status` (including `verified`) and `sample` are checked only against the same replay file, so they are **self-attested** and the UI says so. To attest a replay, pin its file SHA-256 outside `replay/`: `REPLAY_PINS=<replay_id>=<sha256>[,...]`. Set `REPLAY_REQUIRE_PIN=1` to refuse unpinned replays. A pin mismatch fails startup.
