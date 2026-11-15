(function () {
  "use strict";

  const RING_AGENTS = [
    "modeler-agent",
    "props-agent",
    "look-agent",
    "camera-agent"
  ];

  const AGENT_STYLE_KEY = {
    "modeler-agent": "modeler",
    "props-agent": "props",
    "look-agent": "look",
    "camera-agent": "camera"
  };

  // R8: Clean verified turns ONLY; VERIFIED_WITH_SPILL NEVER resolves to verdict-green or verdict-cyan.
  const GREEN_VERDICTS = new Set(["STATE_VERIFIED"]);
  const CYAN_VERDICTS = new Set(["VERIFIED"]);
  const SPILL_VERDICTS = new Set(["VERIFIED_WITH_SPILL"]);
  const AMBER_VERDICTS = new Set([
    "STALE_FRAME",
    "NO_VISIBLE_CHANGE",
    "TURN_TIMEOUT",
    "ACTUATOR_BUSY",
    "QUEUE_FULL",
    "DUPLICATE_EVENT",
    "QUEUED"
  ]);
  const RED_VERDICTS = new Set([
    "CLAIM_REFUSED",
    "NO_LEASE",
    "LEASE_EXPIRED",
    "FRAME_EVICTED",
    "FOOTPRINT_MISSING",
    "JOB_ERROR",
    "INPUT_UNCERTAIN",
    "OUTCOME_UNCERTAIN"
  ]);

  const EVENTS_SHA_RE = /source events\.jsonl sha256=([0-9a-f]{64})/;

  const state = {
    catalog: [],
    bundle: null,
    visibleTurns: [],
    selectedIndex: 0,
    playTimer: null,
    viewMode: "frames"
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(el, value) {
    if (!el) {
      return;
    }
    el.textContent = value === null || value === undefined ? "" : String(value);
  }

  function clearNode(el) {
    if (el) {
      el.replaceChildren();
    }
  }

  function isSafeGatewayUrl(rawUrl) {
    if (typeof rawUrl !== "string") {
      return false;
    }
    const trimmed = rawUrl.trim();
    if (!trimmed || trimmed.length > 512 || /[\u0000-\u0020\u007f]/.test(trimmed)) {
      return false;
    }
    const allowedPrefix =
      trimmed.startsWith("https://") ||
      trimmed.startsWith("http://localhost:") ||
      trimmed.startsWith("http://localhost/") ||
      trimmed === "http://localhost" ||
      trimmed.startsWith("http://127.0.0.1:") ||
      trimmed.startsWith("http://127.0.0.1/") ||
      trimmed === "http://127.0.0.1";
    if (!allowedPrefix) {
      return false;
    }
    try {
      const parsed = new URL(trimmed);
      if (parsed.username || parsed.password) {
        return false;
      }
      if (parsed.protocol === "https:") {
        return Boolean(parsed.hostname);
      }
      if (parsed.protocol === "http:" && (parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1")) {
        return true;
      }
      return false;
    } catch (_err) {
      return false;
    }
  }

  function applyGatewayLink(rawUrl) {
    const gatewayLink = byId("gateway-link");
    if (!gatewayLink) {
      return;
    }
    if (isSafeGatewayUrl(rawUrl)) {
      gatewayLink.setAttribute("href", rawUrl.trim());
      gatewayLink.hidden = false;
    } else {
      gatewayLink.hidden = true;
      gatewayLink.removeAttribute("href");
    }
  }

  function verdictClass(verdict) {
    if (SPILL_VERDICTS.has(verdict)) {
      return "verdict-spill verdict-amber";
    }
    if (GREEN_VERDICTS.has(verdict)) {
      return "verdict-green";
    }
    if (CYAN_VERDICTS.has(verdict)) {
      return "verdict-cyan";
    }
    if (AMBER_VERDICTS.has(verdict)) {
      return "verdict-amber";
    }
    if (RED_VERDICTS.has(verdict)) {
      return "verdict-red";
    }
    return "verdict-observed";
  }

  function stepHasSpill(step) {
    if (!step || step.status !== "observed") {
      return false;
    }
    const outcome = String(step.outcome || "");
    const action = String(step.action || "");
    return (
      outcome.indexOf("VERIFIED_WITH_SPILL") !== -1 ||
      action.indexOf("VERIFIED_WITH_SPILL") !== -1 ||
      outcome.indexOf("spill_regions=") !== -1
    );
  }

  function statusToVerdictClass(status, hasSpill) {
    if (hasSpill) {
      return "verdict-spill verdict-amber";
    }
    if (status === "verified") {
      return "verdict-green";
    }
    if (status === "refused") {
      return "verdict-red";
    }
    if (status === "pending") {
      return "verdict-amber";
    }
    return "verdict-observed";
  }

  function extractEventsShaFromLimitations(limitations) {
    if (!Array.isArray(limitations)) {
      return null;
    }
    for (let i = 0; i < limitations.length; i += 1) {
      const m = EVENTS_SHA_RE.exec(String(limitations[i] || ""));
      if (m && m[1]) {
        return m[1];
      }
    }
    return null;
  }

  function makeAgentBadge(agentId) {
    const key = AGENT_STYLE_KEY[agentId] || "system";
    const span = document.createElement("span");
    span.className = "agent-badge badge-" + key;

    const dot = document.createElement("span");
    dot.className = "agent-dot dot-" + key;
    span.appendChild(dot);

    const txt = document.createElement("span");
    setText(txt, agentId || "system");
    span.appendChild(txt);
    return span;
  }

  function makeVerdictPill(verdictText, cssVariant) {
    const span = document.createElement("span");
    span.className = "verdict-pill " + (cssVariant || verdictClass(verdictText));
    setText(span, verdictText);
    return span;
  }

  function showError(msg) {
    const banner = byId("error-banner");
    if (!banner) {
      return;
    }
    setText(banner, msg);
    banner.hidden = false;
  }

  function hideError() {
    const banner = byId("error-banner");
    if (banner) {
      banner.hidden = true;
      setText(banner, "");
    }
  }

  function stopPlayback() {
    if (state.playTimer !== null) {
      window.clearInterval(state.playTimer);
      state.playTimer = null;
    }
    setText(byId("btn-play"), "Play");
  }

  function togglePlayback() {
    if (state.playTimer !== null) {
      stopPlayback();
      return;
    }
    if (!state.visibleTurns.length) {
      return;
    }
    setText(byId("btn-play"), "Pause");
    state.playTimer = window.setInterval(function () {
      if (!state.visibleTurns.length) {
        stopPlayback();
        return;
      }
      const next = (state.selectedIndex + 1) % state.visibleTurns.length;
      selectTurnIndex(next);
    }, 1100);
  }

  function setViewMode(mode) {
    state.viewMode = mode === "video" ? "video" : "frames";
    const videoSlot = byId("video-slot");
    const frameSlot = byId("frame-slot");
    const btnFrames = byId("btn-mode-frames");
    const btnVideo = byId("btn-mode-video");

    if (state.viewMode === "video") {
      videoSlot.hidden = false;
      frameSlot.hidden = true;
      btnFrames.classList.remove("is-active");
      btnVideo.classList.add("is-active");
    } else {
      videoSlot.hidden = true;
      frameSlot.hidden = false;
      btnFrames.classList.add("is-active");
      btnVideo.classList.remove("is-active");
      const vid = byId("replay-video");
      if (vid && !vid.paused) {
        vid.pause();
      }
    }
  }

  function findFrameById(frames, frameId) {
    for (let i = 0; i < frames.length; i += 1) {
      if (frames[i].frame_id === frameId) {
        return frames[i];
      }
    }
    return frames[0] || null;
  }

  function computeCursorsAt(swarm, targetTms) {
    const palette = swarm.agent_palette || {};
    const cursors = swarm.cursors || [];
    const latestByAgent = {};

    for (let i = 0; i < RING_AGENTS.length; i += 1) {
      const ag = RING_AGENTS[i];
      const spec = palette[ag] || { color: "#cbd5e1", anchor_xy: [960, 540] };
      latestByAgent[ag] = {
        agent_id: ag,
        x: spec.anchor_xy[0],
        y: spec.anchor_xy[1],
        phase: "idle",
        label: ag + " (idle)",
        color: spec.color
      };
    }

    for (let i = 0; i < cursors.length; i += 1) {
      const cur = cursors[i];
      if (cur.t_ms <= targetTms && latestByAgent[cur.agent_id]) {
        const spec = palette[cur.agent_id] || { color: "#38bdf8" };
        latestByAgent[cur.agent_id] = {
          agent_id: cur.agent_id,
          x: cur.x,
          y: cur.y,
          phase: cur.phase,
          label: cur.label,
          color: spec.color
        };
      }
    }
    return latestByAgent;
  }

  function drawCursorOverlay(swarm, activeTurn) {
    const canvas = byId("cursor-canvas");
    if (!canvas || !canvas.getContext) {
      return;
    }
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!swarm || !activeTurn) {
      return;
    }

    const cursorsByAgent = computeCursorsAt(swarm, activeTurn.t_ms);
    for (let i = 0; i < RING_AGENTS.length; i += 1) {
      const ag = RING_AGENTS[i];
      const info = cursorsByAgent[ag];
      if (!info) {
        continue;
      }
      const isHead = activeTurn.agent_id === ag;
      const radius = isHead ? 18 : 11;

      ctx.save();
      ctx.beginPath();
      ctx.arc(info.x, info.y, radius + 5, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(11, 15, 23, 0.88)";
      ctx.lineWidth = 4;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(info.x, info.y, radius, 0, Math.PI * 2);
      ctx.strokeStyle = info.color;
      ctx.lineWidth = isHead ? 3.5 : 2;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(info.x, info.y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = info.color;
      ctx.fill();

      const tagText = String(info.label || ag).slice(0, 56);
      ctx.font = "600 18px ui-monospace, Menlo, Consolas, monospace";
      const metrics = ctx.measureText(tagText);
      const boxW = Math.min(540, metrics.width + 20);
      const boxH = 30;
      const boxX = Math.min(canvas.width - boxW - 12, Math.max(12, info.x + 20));
      const boxY = Math.min(canvas.height - boxH - 12, Math.max(12, info.y - 15));

      ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
      ctx.fillRect(boxX, boxY, boxW, boxH);
      ctx.strokeStyle = info.color;
      ctx.lineWidth = isHead ? 2 : 1;
      ctx.strokeRect(boxX, boxY, boxW, boxH);

      ctx.fillStyle = "#f8fafc";
      ctx.fillText(tagText, boxX + 10, boxY + 21);
      ctx.restore();
    }
  }

  function renderAgentRoster(swarm, activeAgentId) {
    const container = byId("agent-roster");
    clearNode(container);
    if (!swarm || !swarm.agent_palette) {
      return;
    }
    const claimsByAgent = {};
    const claims = swarm.claims || [];
    for (let i = 0; i < claims.length; i += 1) {
      claimsByAgent[claims[i].agent_id] = claims[i];
    }

    for (let i = 0; i < RING_AGENTS.length; i += 1) {
      const ag = RING_AGENTS[i];
      const spec = swarm.agent_palette[ag];
      if (!spec) {
        continue;
      }
      const key = AGENT_STYLE_KEY[ag] || "system";
      const card = document.createElement("div");
      card.className = "agent-card agent-" + key + (activeAgentId === ag ? " is-head" : "");

      card.appendChild(makeAgentBadge(ag));

      const laneLine = document.createElement("p");
      laneLine.className = "agent-meta-line";
      const claim = claimsByAgent[ag];
      const epochTxt = claim ? "lease_epoch=" + claim.lease_epoch : "default spec";
      setText(
        laneLine,
        "lane=" + spec.default_lane + " · allow_gui=" + String(spec.allow_gui) + " · " + epochTxt
      );
      card.appendChild(laneLine);

      const prefixLine = document.createElement("p");
      prefixLine.className = "agent-meta-line";
      setText(
        prefixLine,
        "prefixes: " + (spec.write_prefixes || []).join(", ") +
        " · slot=[" + spec.anchor_xy[0] + "," + spec.anchor_xy[1] + "]"
      );
      card.appendChild(prefixLine);

      container.appendChild(card);
    }
  }

  function renderVerdictSummary(swarm) {
    const row = byId("verdict-summary");
    clearNode(row);
    if (!swarm || !swarm.verdict_counts) {
      return;
    }
    const vc = swarm.verdict_counts;
    const cleanVerified = (vc.STATE_VERIFIED || 0) + (vc.VERIFIED || 0);
    const spillTurns = vc.VERIFIED_WITH_SPILL || 0;

    // R8: Explicitly separate clean verified turns (STATE_VERIFIED / VERIFIED) from spill turns (VERIFIED_WITH_SPILL)
    row.appendChild(makeVerdictPill("Clean Verified: " + cleanVerified, "verdict-green"));
    row.appendChild(makeVerdictPill("Spill Warning: " + spillTurns, "verdict-spill verdict-amber"));

    const keys = Object.keys(vc).sort();
    for (let i = 0; i < keys.length; i += 1) {
      const k = keys[i];
      const count = vc[k];
      row.appendChild(makeVerdictPill(k + " × " + count, verdictClass(k)));
    }
  }

  function renderGatewaySteps(replayDoc, swarm) {
    const container = byId("gateway-step-list");
    clearNode(container);
    const steps = (replayDoc && replayDoc.steps) || [];
    setText(byId("gateway-steps-count"), steps.length + " steps");

    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "step-row";

      const top = document.createElement("div");
      top.className = "turn-row-top";

      const left = document.createElement("span");
      left.className = "mono-meta";
      setText(left, step.step_id + " · " + step.actor + " · t=" + step.t_ms + " ms · " + step.frame_id);
      top.appendChild(left);

      const hasSpill = stepHasSpill(step);
      const pillLabel = hasSpill ? "observed · spill" : step.status;
      top.appendChild(makeVerdictPill(pillLabel, statusToVerdictClass(step.status, hasSpill)));
      btn.appendChild(top);

      const actionLine = document.createElement("div");
      actionLine.className = "turn-row-desc";
      setText(actionLine, step.action);
      btn.appendChild(actionLine);

      const outcomeLine = document.createElement("div");
      outcomeLine.className = "mono-meta";
      setText(outcomeLine, "target=" + step.target + " → " + step.outcome);
      btn.appendChild(outcomeLine);

      btn.addEventListener("click", function () {
        jumpToTimestampOrFrame(step.t_ms, step.frame_id, swarm);
      });

      container.appendChild(btn);
    }
  }

  function jumpToTimestampOrFrame(tMs, frameId, swarm) {
    if (!state.visibleTurns.length || !swarm) {
      return;
    }
    let bestIdx = 0;
    let bestDist = Number.POSITIVE_INFINITY;
    for (let i = 0; i < state.visibleTurns.length; i += 1) {
      const cand = state.visibleTurns[i];
      const dist = Math.abs(cand.t_ms - tMs) + (cand.frame_id === frameId ? 0 : 250);
      if (dist < bestDist) {
        bestDist = dist;
        bestIdx = i;
      }
    }
    selectTurnIndex(bestIdx);
  }

  function rebuildVisibleTurns() {
    if (!state.bundle || !state.bundle.swarm) {
      state.visibleTurns = [];
      return;
    }
    const allTurns = state.bundle.swarm.turns || [];
    const hidePass = Boolean(byId("toggle-hide-pass") && byId("toggle-hide-pass").checked);
    const filtered = hidePass
      ? allTurns.filter(function (t) { return t.verdict !== "PASS"; })
      : allTurns.slice();

    state.visibleTurns = filtered.length > 0 ? filtered : allTurns.slice();

    const slider = byId("timeline-slider");
    if (slider) {
      slider.min = "0";
      slider.max = String(Math.max(0, state.visibleTurns.length - 1));
      slider.value = "0";
    }
    renderTurnList();
    selectTurnIndex(0);
  }

  function renderTurnList() {
    const listEl = byId("turn-list");
    clearNode(listEl);

    for (let i = 0; i < state.visibleTurns.length; i += 1) {
      const turnRec = state.visibleTurns[i];
      const row = document.createElement("button");
      row.type = "button";
      row.className = "turn-row" + (i === state.selectedIndex ? " is-selected" : "");
      row.dataset.turnIdx = String(i);

      const top = document.createElement("div");
      top.className = "turn-row-top";

      const badgeWrap = document.createElement("div");
      badgeWrap.className = "header-row";
      badgeWrap.appendChild(makeAgentBadge(turnRec.agent_id));

      const turnTag = document.createElement("span");
      turnTag.className = "mono-meta";
      const tLabel = turnRec.turn !== null ? "turn " + turnRec.turn : "queued";
      setText(turnTag, tLabel + " · tick " + turnRec.tick + " · " + turnRec.frame_id);
      badgeWrap.appendChild(turnTag);

      top.appendChild(badgeWrap);
      const pillLabel =
        turnRec.verdict === "VERIFIED_WITH_SPILL"
          ? "VERIFIED_WITH_SPILL (spill=" + turnRec.spill_region_count + ")"
          : turnRec.verdict;
      top.appendChild(makeVerdictPill(pillLabel, verdictClass(turnRec.verdict)));
      row.appendChild(top);

      const desc = document.createElement("div");
      desc.className = "turn-row-desc";
      const summaryText = turnRec.expect || turnRec.reason || (turnRec.event_id ? turnRec.event_id : "Empty FIFO ring pass");
      setText(desc, summaryText);
      row.appendChild(desc);

      (function (idx) {
        row.addEventListener("click", function () {
          stopPlayback();
          selectTurnIndex(idx);
        });
      })(i);

      listEl.appendChild(row);
    }
  }

  function selectTurnIndex(index) {
    if (!state.bundle || !state.visibleTurns.length) {
      return;
    }
    const bounded = Math.max(0, Math.min(state.visibleTurns.length - 1, index));
    state.selectedIndex = bounded;

    const slider = byId("timeline-slider");
    if (slider) {
      slider.value = String(bounded);
    }

    const rows = byId("turn-list").children;
    for (let i = 0; i < rows.length; i += 1) {
      if (i === bounded) {
        rows[i].classList.add("is-selected");
      } else {
        rows[i].classList.remove("is-selected");
      }
    }

    const turnRec = state.visibleTurns[bounded];
    const swarm = state.bundle.swarm;
    const activeFrame = findFrameById(swarm.frames || [], turnRec.frame_id);

    setText(
      byId("scrubber-pos-label"),
      "Turn item " + (bounded + 1) + " / " + state.visibleTurns.length +
      " (turn=" + (turnRec.turn !== null ? turnRec.turn : "n/a") + ", tick=" + turnRec.tick + ")"
    );
    setText(
      byId("scrubber-time-label"),
      "t = " + turnRec.t_ms + " ms (ts_ms=" + turnRec.ts_ms + ")"
    );

    if (activeFrame) {
      const img = byId("frame-image");
      if (
        img &&
        typeof activeFrame.src === "string" &&
        activeFrame.src.startsWith("/static/frames/") &&
        !activeFrame.src.includes("..")
      ) {
        img.src = activeFrame.src;
      }
      setText(
        byId("active-frame-meta"),
        activeFrame.frame_id + " (frame_" + activeFrame.source_frame +
        " · scene_epoch=" + activeFrame.scene_epoch +
        " · view_epoch=" + activeFrame.view_epoch + ")"
      );
      setText(byId("frame-caption"), activeFrame.caption);
      setText(
        byId("receipt-frame-sha"),
        activeFrame.frame_id + " (frame_" + activeFrame.source_frame + "): " + activeFrame.sha256
      );
    }

    drawCursorOverlay(swarm, turnRec);
    renderAgentRoster(swarm, turnRec.agent_id);

    const detailVerdict = byId("detail-verdict");
    if (detailVerdict) {
      detailVerdict.className = "verdict-pill " + verdictClass(turnRec.verdict);
      const detailLabel =
        turnRec.verdict === "VERIFIED_WITH_SPILL"
          ? "VERIFIED_WITH_SPILL · spill=" + turnRec.spill_region_count
          : turnRec.verdict;
      setText(detailVerdict, detailLabel);
    }

    setText(
      byId("detail-agent"),
      turnRec.agent_id + " · turn=" + (turnRec.turn !== null ? turnRec.turn : "n/a") + " · tick=" + turnRec.tick
    );
    setText(
      byId("detail-event"),
      (turnRec.event_id || "none (PASS)") + " · lane=" + (turnRec.lane || "ring")
    );
    setText(
      byId("detail-frames"),
      "based_on=" + (turnRec.based_on_frame !== null ? turnRec.based_on_frame : "n/a") +
      " → dispatch=" + (turnRec.dispatch_capture_seq !== null ? turnRec.dispatch_capture_seq : "n/a") +
      " → verify=" + (turnRec.verify_frame_ref !== null ? turnRec.verify_frame_ref : "n/a") +
      " (" + turnRec.frame_id + ")" +
      (turnRec.spill_region_count > 0 ? " · spill_regions=" + turnRec.spill_region_count : "")
    );
    setText(
      byId("detail-writeset"),
      turnRec.write_set && turnRec.write_set.length > 0 ? turnRec.write_set.join(", ") : "[] (read-only / GUI view)"
    );
    setText(byId("detail-expect"), turnRec.expect || "—");
    setText(byId("detail-reason"), turnRec.reason || "No error or refusal reason recorded.");

    if (state.viewMode === "video") {
      const vid = byId("replay-video");
      if (vid && Number.isFinite(vid.duration) && vid.duration > 0) {
        const sec = Math.max(0, Math.min(vid.duration, turnRec.t_ms / 1000.0));
        vid.currentTime = sec;
      }
    }
  }

  function applyBundle(payload) {
    if (
      !payload ||
      payload.mode !== "REPLAY" ||
      payload.live !== false ||
      !payload.replay ||
      !payload.swarm ||
      payload.swarm.mode !== "REPLAY" ||
      payload.swarm.live !== false
    ) {
      throw new Error("Refused bundle: server payload violated REPLAY / live=false invariant.");
    }

    stopPlayback();
    state.bundle = payload;

    const replayDoc = payload.replay;
    const swarm = payload.swarm;
    const eventsSha =
      payload.source_events_sha256 ||
      swarm.source_events_sha256 ||
      extractEventsShaFromLimitations(replayDoc.limitations) ||
      "—";

    setText(byId("mode-badge"), "REPLAY · NOT LIVE");
    setText(byId("replay-title"), replayDoc.title);
    setText(
      byId("replay-subtitle"),
      "Recorded " + replayDoc.recorded_at + " · schema=" + swarm.schema + " · mode=REPLAY · live=false"
    );
    setText(byId("receipt-attestation"), payload.attestation || "pinned");
    setText(byId("receipt-id"), payload.replay_id + " · " + replayDoc.recorded_at);
    setText(byId("receipt-events-sha"), eventsSha);
    setText(byId("receipt-replay-sha"), payload.sha256);
    setText(byId("receipt-swarm-sha"), payload.swarm_sha256);
    setText(byId("replay-task"), replayDoc.task);

    const limList = byId("limitations-list");
    clearNode(limList);
    const lims = replayDoc.limitations || [];
    for (let i = 0; i < lims.length; i += 1) {
      const li = document.createElement("li");
      setText(li, lims[i]);
      limList.appendChild(li);
    }

    const vidEl = byId("replay-video");
    const btnVideo = byId("btn-mode-video");
    const fallbackNote = byId("video-fallback-note");
    if (
      payload.video_available === true &&
      typeof swarm.video_url === "string" &&
      swarm.video_url.startsWith("/static/videos/") &&
      !swarm.video_url.includes("..")
    ) {
      vidEl.src = swarm.video_url;
      btnVideo.hidden = false;
      fallbackNote.hidden = true;
      setViewMode("video");
    } else {
      vidEl.removeAttribute("src");
      btnVideo.hidden = true;
      fallbackNote.hidden = false;
      setViewMode("frames");
    }

    renderVerdictSummary(swarm);
    renderGatewaySteps(replayDoc, swarm);
    rebuildVisibleTurns();
  }

  async function loadReplayById(replayId) {
    hideError();
    try {
      const resp = await window.fetch("/api/replays/" + encodeURIComponent(replayId), {
        method: "GET",
        headers: { "Accept": "application/json" }
      });
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status + " loading replay " + replayId);
      }
      const data = await resp.json();
      applyBundle(data);
    } catch (err) {
      showError("Failed to load replay bundle: " + (err && err.message ? err.message : String(err)));
    }
  }

  async function bootstrap() {
    const btnPrev = byId("btn-prev");
    const btnNext = byId("btn-next");
    const btnPlay = byId("btn-play");
    const slider = byId("timeline-slider");
    const togglePass = byId("toggle-hide-pass");
    const selectEl = byId("replay-select");
    const btnModeFrames = byId("btn-mode-frames");
    const btnModeVideo = byId("btn-mode-video");

    btnPrev.addEventListener("click", function () {
      stopPlayback();
      selectTurnIndex(state.selectedIndex - 1);
    });

    btnNext.addEventListener("click", function () {
      stopPlayback();
      selectTurnIndex(state.selectedIndex + 1);
    });

    btnPlay.addEventListener("click", function () {
      togglePlayback();
    });

    slider.addEventListener("input", function () {
      stopPlayback();
      selectTurnIndex(Number.parseInt(slider.value, 10) || 0);
    });

    togglePass.addEventListener("change", function () {
      stopPlayback();
      rebuildVisibleTurns();
    });

    selectEl.addEventListener("change", function () {
      if (selectEl.value) {
        loadReplayById(selectEl.value);
      }
    });

    btnModeFrames.addEventListener("click", function () {
      setViewMode("frames");
    });

    btnModeVideo.addEventListener("click", function () {
      setViewMode("video");
    });

    window.addEventListener("keydown", function (ev) {
      if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT")) {
        return;
      }
      if (ev.key === "ArrowLeft") {
        stopPlayback();
        selectTurnIndex(state.selectedIndex - 1);
      } else if (ev.key === "ArrowRight") {
        stopPlayback();
        selectTurnIndex(state.selectedIndex + 1);
      }
    });

    try {
      const resp = await window.fetch("/api/replays", {
        method: "GET",
        headers: { "Accept": "application/json" }
      });
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status + " listing replays");
      }
      const catalogPayload = await resp.json();
      if (!catalogPayload || catalogPayload.mode !== "REPLAY" || catalogPayload.live !== false) {
        throw new Error("Catalog violated REPLAY / live=false contract.");
      }

      applyGatewayLink(catalogPayload.gateway_url);

      clearNode(selectEl);
      const list = catalogPayload.replays || [];
      state.catalog = list;
      for (let i = 0; i < list.length; i += 1) {
        const item = list[i];
        const opt = document.createElement("option");
        opt.value = item.replay_id;
        setText(opt, item.replay_id + " — " + item.frames + " frames, " + item.turns + " turns");
        selectEl.appendChild(opt);
      }

      const hasFlagship = list.some(function (x) { return x.replay_id === "take2-w2-4agents"; });
      const initialId =
        catalogPayload.default_replay_id ||
        (hasFlagship ? "take2-w2-4agents" : (list[0] && list[0].replay_id));
      if (initialId) {
        selectEl.value = initialId;
        await loadReplayById(initialId);
      }
    } catch (err) {
      showError("Startup error: " + (err && err.message ? err.message : String(err)));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
