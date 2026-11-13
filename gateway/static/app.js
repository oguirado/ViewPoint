(() => {
  "use strict";

  const state = {
    replay: null,
    frameIndex: 0,
    timer: null,
    planSeq: 0,
    planLoading: false,
    pinned: false,
  };

  let bootstrap = null;
  const params = new URLSearchParams(window.location.search);

  // Demo token arrives out-of-band only: URL fragment (#demo_token=...) or
  // the input field. It is never embedded in served HTML or page source.
  let fragmentDemoToken = "";
  try {
    const hash = window.location.hash ? window.location.hash.slice(1) : "";
    if (hash) {
      const hashParams = new URLSearchParams(hash);
      fragmentDemoToken = hashParams.get("demo_token") || "";
      if (fragmentDemoToken) {
        const clean = window.location.pathname + window.location.search;
        window.history.replaceState(null, "", clean);
      }
    }
  } catch (_) {
    fragmentDemoToken = "";
  }

  function getDemoToken() {
    try {
      const field = document.getElementById("demo-token");
      if (field && field.value) return field.value.trim();
    } catch (_) {}
    return fragmentDemoToken || "";
  }

  const el = (id) => {
    const node = document.getElementById(id);
    if (!node) throw new Error(`Missing element #${id}`);
    return node;
  };

  const setText = (node, value) => {
    node.textContent = value == null ? "" : String(value);
  };

  const formatTime = (ms) => {
    const total = Math.max(0, Number(ms) || 0);
    const minutes = Math.floor(total / 60000);
    const seconds = Math.floor((total % 60000) / 1000);
    const tenths = Math.floor((total % 1000) / 100);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths}`;
  };

  const shortHash = (hash) => (hash ? `${hash.slice(0, 12)}…` : "unknown");

  const formatGeneratedAt = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "unknown time";
    return date.toLocaleString([], {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const makeChip = (text, className = "") => {
    const chip = document.createElement("span");
    chip.className = `meta-chip ${className}`.trim();
    chip.textContent = text;
    return chip;
  };

  const setPlanStatus = (text, kind = "") => {
    const status = el("plan-status");
    status.className = "plan-status" + (kind ? ` is-${kind}` : "");
    setText(status, text);
  };

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : {};
      const error = new Error(detail.message || `HTTP ${response.status}`);
      error.code = detail.code || "HTTP_ERROR";
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function renderReplayMeta(replay) {
    const meta = el("replay-meta");
    meta.replaceChildren();
    const summary = bootstrap.replays.find((item) => item.replay_id === replay.replay_id);
    const pinned = Boolean(summary && summary.attestation === "pinned");
    state.pinned = pinned;
    meta.append(
      makeChip(`schema ${replay.schema.replace("viewpoint.replay/", "")}`),
      makeChip(`v${replay.version}`),
      makeChip(`${replay.steps.length} actions`),
      makeChip(`${replay.frames.length} frames`),
      makeChip(
        pinned
          ? `sha ${shortHash(summary && summary.sha256)} · pinned digest`
          : `file sha ${shortHash(summary && summary.sha256)} · self-attested, not an attestation`,
        pinned ? "" : "chip-self-attested",
      ),
    );

    el("timeline-note").textContent = `${replay.steps.length} actions · ${replay.frames.length} frames`;
    el("topbar-mode").textContent =
      `${replay.sample ? "recorded sample" : "recorded session"}${pinned ? "" : " · labels self-attested"} · no live host link`;
    el("mode-badge").textContent = replay.mode;
    el("sample-badge").hidden = !replay.sample;
    el("footer-notice").textContent = replay.sample
      ? replay.sample_notice
      : "Recorded session. The replay is shown for review; it does not actuate a browser.";
    el("footer-revision").textContent = bootstrap.revision || "local";
  }

  function renderTimeline(replay) {
    const timeline = el("timeline");
    timeline.replaceChildren();

    replay.steps.forEach((step) => {
      const item = document.createElement("li");
      item.className = "timeline-step";
      item.dataset.stepId = step.step_id;
      item.dataset.frameId = step.frame_id;

      const button = document.createElement("button");
      button.type = "button";
      button.className = "step-hit";
      button.setAttribute(
        "aria-label",
        `${step.actor}: ${step.action}. Outcome: ${step.outcome}`,
      );
      button.addEventListener("click", () => {
        stopPlayback();
        showFrameById(step.frame_id, step.step_id);
      });

      const time = document.createElement("span");
      time.className = "step-time";
      setText(time, formatTime(step.t_ms));

      const body = document.createElement("span");
      body.className = "step-body";

      const top = document.createElement("span");
      top.className = "step-top";
      const actor = document.createElement("span");
      actor.className = `actor actor-${step.actor}`;
      setText(actor, step.actor);
      const status = document.createElement("span");
      status.className = `status status-${step.status}${state.pinned ? "" : " is-self-attested"}`;
      setText(status, state.pinned ? step.status : `${step.status} · self-attested`);
      top.append(actor, status);

      const action = document.createElement("span");
      action.className = "step-action";
      setText(action, step.action);

      const meta = document.createElement("span");
      meta.className = "step-meta";
      setText(meta, `target · ${step.target}`);

      const outcome = document.createElement("span");
      outcome.className = "step-outcome";
      setText(outcome, step.outcome);

      body.append(top, action, meta, outcome);
      button.append(time, body);
      item.append(button);
      timeline.append(item);
    });
  }

  function renderReplay(replay) {
    state.replay = replay;
    state.frameIndex = 0;

    document.title = `${replay.title} — ViewPoint replay`;
    setText(el("replay-title"), replay.title);
    setText(el("replay-task"), replay.task);
    renderReplayMeta(replay);
    renderTimeline(replay);

    setText(el("terminal-outcome"), replay.terminal_outcome);
    setText(
      el("replay-limitations"),
      Array.isArray(replay.limitations) && replay.limitations.length
        ? replay.limitations.join(" · ")
        : "",
    );

    showFrame(0);
  }

  function setActiveStep(stepId) {
    document.querySelectorAll(".timeline-step").forEach((item) => {
      const active = item.dataset.stepId === stepId;
      item.classList.toggle("is-active", active);
      const button = item.querySelector("button");
      if (button) {
        if (active) button.setAttribute("aria-current", "step");
        else button.removeAttribute("aria-current");
      }
    });
  }

  function showFrame(index, activeStepId = null) {
    if (!state.replay || !state.replay.frames.length) return;
    const frames = state.replay.frames;
    const next = Math.max(0, Math.min(frames.length - 1, index));
    state.frameIndex = next;
    const frame = frames[next];

    const image = el("frame-image");
    image.src = frame.src;
    image.alt = `Replay frame ${next + 1}: ${frame.caption}`;

    setText(el("frame-url"), frame.url);
    setText(el("frame-counter"), `${next + 1} / ${frames.length}`);
    setText(el("frame-caption"), `${frame.caption} · ${formatTime(frame.t_ms)}`);

    if (activeStepId) {
      setActiveStep(activeStepId);
    } else {
      const first = state.replay.steps.find((step) => step.frame_id === frame.frame_id);
      if (first) setActiveStep(first.step_id);
    }
  }

  function showFrameById(frameId, activeStepId = null) {
    if (!state.replay) return;
    const index = state.replay.frames.findIndex((frame) => frame.frame_id === frameId);
    if (index >= 0) showFrame(index, activeStepId);
  }

  function updatePlayButton() {
    const button = el("frame-play");
    const playing = Boolean(state.timer);
    button.textContent = playing ? "❚❚ pause" : "▶ play";
    button.setAttribute("aria-label", playing ? "Pause frame sequence" : "Play frame sequence");
  }

  function stopPlayback() {
    if (state.timer) {
      window.clearInterval(state.timer);
      state.timer = null;
    }
    updatePlayButton();
  }

  function togglePlayback() {
    if (state.timer) {
      stopPlayback();
      return;
    }
    if (!state.replay) return;
    if (state.frameIndex >= state.replay.frames.length - 1) showFrame(0);
    state.timer = window.setInterval(() => {
      if (!state.replay) return;
      if (state.frameIndex >= state.replay.frames.length - 1) {
        stopPlayback();
        return;
      }
      showFrame(state.frameIndex + 1);
    }, 1600);
    updatePlayButton();
  }

  function renderPlan(payload) {
    const plan = payload.plan;
    setPlanStatus(
      `Gemini ${payload.model} · ${formatGeneratedAt(payload.generated_at)} · replay ${payload.replay_id}`,
      "ok",
    );
    setText(el("plan-model"), `model · ${payload.model}`);

    const content = el("plan-content");
    content.replaceChildren();

    const summary = document.createElement("p");
    summary.className = "plan-summary";
    setText(summary, plan.summary);
    content.append(summary);

    const reasoning = document.createElement("div");
    reasoning.className = "plan-reasoning";
    const reasonLabel = document.createElement("p");
    reasonLabel.className = "label";
    setText(reasonLabel, "Gemini reasoning · stated rationale");
    const reasonText = document.createElement("p");
    setText(reasonText, plan.reasoning);
    reasoning.append(reasonLabel, reasonText);
    content.append(reasoning);

    const list = document.createElement("ol");
    list.className = "plan-steps";
    plan.steps.forEach((step, index) => {
      const item = document.createElement("li");
      item.className = "plan-step";

      const number = document.createElement("span");
      number.className = "plan-step-num";
      setText(number, String(index + 1).padStart(2, "0"));

      const body = document.createElement("div");
      const title = document.createElement("h3");
      setText(title, step.title);
      const detail = document.createElement("p");
      setText(detail, step.detail);
      const refs = document.createElement("div");
      refs.className = "refs";

      if (step.engine_step_refs.length) {
        // Fail closed: without per-ref data, no id is presented as existing.
        const matches = Array.isArray(step.ref_matches)
          ? step.ref_matches
          : step.engine_step_refs.map((id) => ({ id, exists: false }));
        matches.forEach((ref) => {
          const chip = document.createElement("span");
          chip.className = `ref-chip ${ref.exists === true ? "ref-matched" : "ref-unmatched"}`;
          setText(chip, ref.exists === true
            ? `recorded step ${ref.id}`
            : `step ${ref.id} · no matching execution`);
          refs.append(chip);
        });
        const MATCH_TEXT = {
          matched: "ID exists; support unverified",
          partial: "some referenced IDs have no matching execution",
          unmatched: "no matching execution",
        };
        const matchChip = document.createElement("span");
        const matchClass = {
          matched: "ref-matched",
          partial: "ref-partial",
          unmatched: "ref-unmatched",
        }[step.match] || "ref-unmatched";
        matchChip.className = `ref-chip ${matchClass}`;
        setText(matchChip, MATCH_TEXT[step.match] || "no matching execution");
        refs.append(matchChip);
      } else {
        const chip = document.createElement("span");
        chip.className = "ref-chip ref-no_refs";
        setText(chip, "no recorded step referenced");
        refs.append(chip);
      }

      body.append(title, detail, refs);
      item.append(number, body);
      list.append(item);
    });
    content.append(list);

    const policy = document.createElement("p");
    policy.className = "evidence-policy";
    setText(policy, payload.evidence_policy);
    content.append(policy);
  }

  function showPlanError(error) {
    const missingKey = error.code === "GEMINI_KEY_MISSING";
    const status = error.status || 0;
    const rateLimited = error.code === "RATE_LIMITED" || error.code === "DAILY_CAP_EXCEEDED" || error.code === "PLAN_BUSY" || status === 429;
    const shuttingDown = error.code === "SHUTTING_DOWN" || status === 503;
    let statusText = "Gemini call failed";
    if (missingKey) statusText = "Gemini not configured";
    else if (rateLimited) statusText = "Rate limited (429) — plan not generated";
    else if (shuttingDown) statusText = "Service unavailable (503) — plan not generated";
    else if (status === 401) statusText = "Demo token required (401) — plan not generated";
    setPlanStatus(statusText, "error");
    setText(el("plan-model"), "server-side only");

    const content = el("plan-content");
    content.replaceChildren();
    const box = document.createElement("div");
    box.className = "error-box";
    const title = document.createElement("p");
    title.className = "error-title";
    let titleText = "Provider error";
    if (missingKey) titleText = "Gemini key absent";
    else if (rateLimited) titleText = "Rate limited (429)";
    else if (shuttingDown) titleText = "Service unavailable (503)";
    else if (status === 401) titleText = "Demo token required (401)";
    setText(title, titleText);
    const message = document.createElement("p");
    message.className = "error-message";
    let messageText = error.message || "The Gemini plan could not be generated.";
    if (rateLimited && !(error.code === "RATE_LIMITED" || error.code === "DAILY_CAP_EXCEEDED" || error.code === "PLAN_BUSY")) {
      messageText += " The daily or per-client quota is exhausted. No Gemini call was made for this attempt.";
    }
    setText(message, messageText);
    const hint = document.createElement("p");
    hint.className = "error-hint";
    if (missingKey) {
      setText(hint, "Set GEMINI_API_KEY on the Cloud Run service from Secret Manager, then reload. The key is never sent to this page.");
    } else if (rateLimited) {
      setText(hint, "The replay remains visible and labelled REPLAY above. No plan was generated; select Generate plan again later. No automatic retry was made for this attempt.");
    } else if (shuttingDown) {
      setText(hint, "The replay remains visible and labelled REPLAY above. No plan was generated during shutdown. No automatic retry was made for this attempt.");
    } else {
      setText(hint, "No plan is fabricated. The replay remains available and is still labelled REPLAY.");
    }
    box.append(title, message, hint);
    content.append(box);
  }

  async function requestPlan() {
    if (!state.replay) return;
    if (state.planLoading) return;
    const seq = ++state.planSeq;
    state.planLoading = true;
    el("plan-button").disabled = true;
    setPlanStatus("Calling Gemini API on the server…", "busy");
    el("plan-content").replaceChildren();

    try {
      const headers = { "Content-Type": "application/json" };
      const demoToken = getDemoToken();
      if (demoToken) headers["X-Demo-Token"] = demoToken;
      const payload = await fetchJson("/api/plan", {
        method: "POST",
        headers,
        body: JSON.stringify({ replay_id: state.replay.replay_id }),
      });
      if (seq !== state.planSeq) return;
      renderPlan(payload);
    } catch (error) {
      if (seq !== state.planSeq) return;
      showPlanError(error);
    } finally {
      if (seq === state.planSeq) {
        state.planLoading = false;
        el("plan-button").disabled = false;
      }
    }
  }

  function showStageError(error) {
    setText(el("replay-title"), "Replay unavailable");
    setText(el("replay-task"), "");
    setText(el("timeline-note"), "");
    setText(el("terminal-outcome"), "");
    setText(el("replay-limitations"), "");
    el("frame-image").removeAttribute("src");
    setText(el("frame-caption"), "No frame loaded.");
    setText(el("frame-counter"), "0 / 0");
    const timeline = el("timeline");
    timeline.replaceChildren();
    const item = document.createElement("li");
    item.className = "page-error";
    setText(item, error.message || "The replay could not be loaded.");
    timeline.append(item);
  }

  function populateReplayPicker() {
    const select = el("replay-select");
    select.replaceChildren();
    bootstrap.replays.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.replay_id;
      option.textContent = item.sample ? `${item.title} (SAMPLE)` : item.title;
      select.append(option);
    });
    select.hidden = bootstrap.replays.length < 2;
    select.addEventListener("change", () => {
      const next = new URLSearchParams(window.location.search);
      next.set("replay", select.value);
      window.history.replaceState(null, "", `?${next.toString()}`);
      loadReplay(select.value);
    });
  }

  async function loadReplay(replayId) {
    state.planSeq += 1;
    state.planLoading = false;
    stopPlayback();
    setPlanStatus("Waiting for the replay…", "");
    el("plan-content").replaceChildren();
    el("plan-button").disabled = true;
    setText(el("replay-title"), "Loading replay…");
    setText(el("replay-task"), "");
    setText(el("replay-limitations"), "");

    try {
      const replay = await fetchJson(`/api/replay/${encodeURIComponent(replayId)}`);
      renderReplay(replay);
      const select = el("replay-select");
      if (select && !select.hidden) select.value = replay.replay_id;
      el("plan-button").disabled = false;
      setPlanStatus("Replay ready. Select Generate plan to request a new Gemini plan.", "");
    } catch (error) {
      showStageError(error);
      el("plan-content").replaceChildren();
      setPlanStatus("Waiting for a replay.", "");
    }
  }

  function bindControls() {
    el("frame-prev").addEventListener("click", () => {
      stopPlayback();
      showFrame(state.frameIndex - 1);
    });
    el("frame-next").addEventListener("click", () => {
      stopPlayback();
      showFrame(state.frameIndex + 1);
    });
    el("frame-play").addEventListener("click", togglePlayback);
    el("plan-button").addEventListener("click", requestPlan);
    window.addEventListener("beforeunload", stopPlayback);
  }

  function init() {
    try {
      const node = document.getElementById("bootstrap");
      bootstrap = JSON.parse(node.textContent);
    } catch (_) {
      showStageError(new Error("The page bootstrap is missing or invalid."));
      return;
    }
    if (!bootstrap || !Array.isArray(bootstrap.replays) || !bootstrap.replays.length) {
      showStageError(new Error("No replay is configured on this deployment."));
      return;
    }

    bindControls();
    populateReplayPicker();

    const requested = params.get("replay");
    const known = bootstrap.replays.some((item) => item.replay_id === requested);
    loadReplay(known ? requested : bootstrap.replayId);
  }

  init();
})();
