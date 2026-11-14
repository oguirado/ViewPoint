# Operator Submission & Release Runbook (`swarm/docs/OPERATOR_CHECKLIST.md`)

> **CRITICAL OPERATOR BOUNDARY**: Autonomous AI agents NEVER push to remote repositories, create remote tags, deploy services, or publish submissions. All steps in this runbook must be performed manually and explicitly by the human operator (`oguirado`).

---

## Step 1: Preflight Verification & Integrity Audit

1. **Working Tree Cleanliness**:
   Inspect git status to ensure all Track H deliverables are accounted for and no unintended files are staged:
   ```bash
   git status --short
   ```
2. **Review Provenance & Receipts**:
   Verify that `PROVENANCE.md` matches the final commit hashes and all generation proof logs in `swarm/proof/` match the hashes listed in [swarm/docs/PROOF.md](PROOF.md).
3. **Secrets & Credentials Audit**:
   Confirm that zero credentials, API keys, private tokens, or session artifacts under `~/.gemini` are staged or committed. Run the public hygiene checker:
   ```bash
   bash tools/check_public.sh
   ```

---

## Step 2: Branch Push & Release Tagging

1. **Push Active Hackathon Branch**:
   Push the `swarm-1114` branch to the remote repository:
   ```bash
   git push origin swarm-1114
   ```
2. **Tag Hackathon Release**:
   Create and push the immutable release tag `v0.3.0-hackathon`:
   ```bash
   git tag -a v0.3.0-hackathon -m "ViewPoint Swarm Hackathon Release (Track H + Track P)"
   git push origin v0.3.0-hackathon
   ```
   > **SAFETY NOTE**: Keep the base tag `pre-hackathon-1114` local only. NEVER push `pre-hackathon-1114` to remote repositories.

---

## Step 3: Google Cloud Run Deployment (`[TARGET]`)

Keep `[TARGET]` status on all deployment claims until `~/vp-deploy/deploy_receipt.txt` exists.

1. **Pre-Deploy Hygiene Scan (`swarm/viewer/`)**:
   Run from inside `swarm/viewer/` before deploying:
   ```bash
   grep -rIl -e 'AIza' -e 'BEGIN .*PRIVATE KEY' -e '/home/' . || echo CLEAN
   ```
2. **Deploy Service A (`viewpoint-gateway` from `gateway/`)**:
   Deploy the hardened gateway service (`REPLAY_REQUIRE_PIN=1`, `REPLAY_PINS`, `DEMO_TOKEN`, and Secret Manager `gemini-api-key` — never inject raw keys in source or env literals):
   ```bash
   gcloud run deploy viewpoint-gateway \
     --source gateway/ \
     --region europe-west1 \
     --platform managed \
     --allow-unauthenticated \
     --port 8080 \
     --cpu 1 \
     --memory 512Mi \
     --min-instances 0 \
     --max-instances 1 \
     --concurrency 5 \
     --timeout 60 \
     --set-secrets="GEMINI_API_KEY=gemini-api-key:latest" \
     --set-env-vars="REPLAY_REQUIRE_PIN=1,REPLAY_PINS=<GATEWAY_PIN_SHA256>,DEMO_TOKEN=<OPERATOR_DEMO_TOKEN>"
   ```
3. **Deploy Service B (`viewpoint-swarm-viewer` from `swarm/viewer/`)**:
   Deploy the read-only standalone swarm replay viewer using the dedicated `viewpoint-viewer-run` service account, `REPLAY_REQUIRE_PIN=1`, `REPLAY_PINS`, and `GATEWAY_URL`:
   ```bash
   gcloud run deploy viewpoint-swarm-viewer \
     --source swarm/viewer/ \
     --region europe-west1 \
     --platform managed \
     --allow-unauthenticated \
     --port 8080 \
     --cpu 1 \
     --memory 256Mi \
     --min-instances 0 \
     --max-instances 2 \
     --concurrency 40 \
     --service-account="viewpoint-viewer-run@${PROJECT_ID}.iam.gserviceaccount.com" \
     --set-env-vars="^@^REPLAY_REQUIRE_PIN=1@REPLAY_PINS=take2-w2-4agents=6c83360080dd4d82d76f4135034c6f9427953e471f4e36da7c1ac167580c1e8c,take3-w2-guards=1c376a25fd99e2349717156bc3bf5e0570e8e65620fb1970dc096ecad4166b6e,take1-w1-2agents=2023da5127580be7c77522e312f455733c2fbbff56a7992512e112b5b3d6f335@GATEWAY_URL=<VIEWPOINT_GATEWAY_URL>"
   ```
4. **Save Deployment Receipt**:
   Query both live deployed Cloud Run services via `gcloud run services describe` and persist their URLs, revision/commit metadata, and live smoke-check outputs into `~/vp-deploy/deploy_receipt.txt`:
   > **WARNING**: NEVER write placeholder URLs (e.g. `xxxx.a.run.app`) into `~/vp-deploy/deploy_receipt.txt`. Only write real live service URLs, revision IDs, and smoke-check responses returned after deployment.
   ```bash
   mkdir -p ~/vp-deploy
   : > ~/vp-deploy/deploy_receipt.txt
   date -Is | tee -a ~/vp-deploy/deploy_receipt.txt

   LINK=$(gcloud run services describe viewpoint-gateway \
     --region europe-west1 --format='value(status.url)')
   REV=$(gcloud run services describe viewpoint-gateway \
     --region europe-west1 --format='value(status.latestReadyRevisionName)')
   echo "A $LINK $REV" | tee -a ~/vp-deploy/deploy_receipt.txt

   VURL=$(gcloud run services describe viewpoint-swarm-viewer \
     --region europe-west1 --format='value(status.url)')
   echo "B $VURL $(git log --oneline -1)" | tee -a ~/vp-deploy/deploy_receipt.txt

   # Live smoke checks appended to receipt
   curl -fsS "$LINK/healthz" | tee -a ~/vp-deploy/deploy_receipt.txt
   echo "" | tee -a ~/vp-deploy/deploy_receipt.txt
   curl -fsS "$VURL/healthz" | tee -a ~/vp-deploy/deploy_receipt.txt
   echo "" | tee -a ~/vp-deploy/deploy_receipt.txt
   ```

---

## Step 4: Video Upload & Submission Form

1. **Upload Demo Video**:
   Upload the final composited showcase cut (`~/Vídeos/ViewPoint_hackathon/swarm_final_demo_60s.mp4`, `<= 60s` showcase cut) to YouTube as Unlisted/Public.
2. **Submit Official Hackathon Entry**:
   Complete the submission form before the hard stop of 19:30 CET [CITED] on Saturday 14 Nov 2026, providing links to:
   - [README.md](../../README.md) (`## Hackathon build (swarm-1114)`)
   - [PROVENANCE.md](../../PROVENANCE.md)
   - [swarm/docs/ONE_PAGER_IDEA.md](ONE_PAGER_IDEA.md)
   - [swarm/docs/ONE_PAGER_TEAM.md](ONE_PAGER_TEAM.md)
