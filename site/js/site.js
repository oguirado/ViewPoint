/* ViewPoint · Gemini in Blender — static site logic (no build step, no trackers). */
(() => {
  "use strict";

  const MV_SRC = "https://cdn.jsdelivr.net/npm/@google/model-viewer@4.1.0/dist/model-viewer.min.js";
  const YT_ID = /^[A-Za-z0-9_-]{11}$/;

  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid);
    return n;
  };

  const getJSON = async (url) => {
    try {
      const r = await fetch(url, { cache: "no-cache" });
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch (e) {
      console.warn("[viewpoint] could not load", url, e);
      return null;
    }
  };

  const videoId = (videos, key) => {
    const v = videos && typeof videos[key] === "string" ? videos[key].trim() : "";
    return YT_ID.test(v) ? v : "";
  };

  const ytEmbed = (id, title) => el("iframe", {
    class: "yt",
    src: `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0&modestbranding=1`,
    title,
    allow: "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture",
    allowfullscreen: true,
    referrerpolicy: "strict-origin-when-cross-origin",
    loading: "lazy",
  });

  // Click-to-load facade: nothing is requested from YouTube until the visitor presses play.
  const ytFacade = (id, title, poster) => {
    const b = el("button", { class: "yt", type: "button", "aria-label": `Play video: ${title}` },
      el("img", { src: poster, alt: "", loading: "lazy" }),
      el("span", { class: "yt__play", "aria-hidden": "true" }));
    b.addEventListener("click", () => b.replaceWith(ytEmbed(id, title)), { once: true });
    return b;
  };

  /* ---------- modal ---------- */
  const modal = $("#modal");
  const modalBody = $("#modal-body");
  const openModal = (title, content, note = "") => {
    $("#modal-title").textContent = title;
    modalBody.replaceChildren(content);
    $("#modal-note").textContent = note;
    $("#modal-note").hidden = !note;
    if (typeof modal.showModal === "function") modal.showModal();
    else modal.setAttribute("open", "");
  };
  modal.addEventListener("close", () => { modalBody.replaceChildren(); $("#modal-status").textContent = ""; });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".fact.is-open").forEach((f) => f.classList.remove("is-open"));
    const a = document.activeElement;
    if (a && a.classList && a.classList.contains("src")) a.blur();
  });
  modal.addEventListener("click", (e) => {
    if (e.target === modal || e.target.closest("[data-close]")) modal.close();
  });

  let mvPromise = null;
  const loadModelViewer = () => {
    if (customElements.get("model-viewer")) return Promise.resolve();
    if (!mvPromise) {
      mvPromise = new Promise((resolve, reject) => {
        const s = el("script", { type: "module", src: MV_SRC });
        s.onload = () => customElements.whenDefined("model-viewer").then(resolve);
        s.onerror = reject;
        document.head.append(s);
      });
    }
    return mvPromise;
  };

  const open3D = async (p) => {
    const holder = el("div", { class: "mv-loading", role: "status", text: "Loading the 3D viewer…" });
    const mb = (p.glb_bytes / 1e6).toFixed(2);
    const who = p.glb_by_director
      ? `The .glb exported after the take by the director (Claude) from the agents' scene (${mb} MB).`
      : `The .glb exported by the agent (${mb} MB).`;
    const fan = p.fan_concept ? " Unofficial fan concept." : "";
    const vw = p.viewer || {};
    openModal(`${p.title} · 3D`, holder,
      `${who}${fan} Drag to turn, scroll or pinch to zoom. ${vw.note || "The animation plays if the file has one."}`);
    try {
      await loadModelViewer();
    } catch {
      holder.textContent = "The 3D viewer could not load. The renders and the video show the same piece.";
      return;
    }
    if (!modal.open) return;
    const mv = el("model-viewer", {
      src: p.glb,
      alt: `3D model of ${p.title}, ${p.subtitle}`,
      "camera-controls": true,
      "touch-action": "pan-y",
      autoplay: true,
      "shadow-intensity": "1",
      exposure: vw.exposure || (p.id === "logo" ? "1.1" : "1"),
      "environment-image": "neutral",
      "auto-rotate": p.id === "logo" ? true : null,
      "interaction-prompt": "auto",
      "camera-target": vw.camera_target || null,
      "camera-orbit": vw.camera_orbit || null,
      "min-camera-orbit": vw.min_camera_orbit || null,
      "max-camera-orbit": vw.max_camera_orbit || null,
      poster: p.poster,
      loading: "eager",
    });
    mv.setAttribute("tabindex", "0");
    mv.addEventListener("load", () => { $("#modal-status").textContent = `${p.title} 3D model loaded. Use the arrow keys to turn it.`; }, { once: true });
    modalBody.replaceChildren(mv);
  };

  const openVideo = (p, id) => openModal(`${p.title} · the take`, ytEmbed(id, `${p.title}: the recorded take`));

  /* ---------- pieces ---------- */
  const factEl = (f, pid, i) => {
    const tipId = `tip-${pid}-${i}`;
    const fact = el("div", { class: "fact" });
    const btn = el("button", { class: "src", type: "button", "aria-describedby": tipId, text: f.value });
    btn.addEventListener("click", () => fact.classList.toggle("is-open"));
    const side = () => {
      const r = fact.getBoundingClientRect();
      fact.classList.toggle("tip-right", r.left + r.width / 2 > window.innerWidth / 2);
      fact.classList.toggle("tip-left", r.left + r.width / 2 <= window.innerWidth / 2);
    };
    fact.addEventListener("pointerenter", side);
    btn.addEventListener("focus", side);
    btn.addEventListener("blur", () => fact.classList.remove("is-open"));
    fact.append(el("dt", { text: f.label }), el("dd", {}, btn),
      el("span", { class: "tip", role: "tooltip", id: tipId, text: `Source: ${f.source}` }));
    return fact;
  };

  const icon3d = () => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true");
    s.innerHTML = '<path d="M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 2.3L18.7 8 12 11.7 5.3 8 12 4.3ZM5 9.7l6 3.3v6.6l-6-3.3V9.7Zm8 9.9V13l6-3.3v6.6l-6 3.3Z" fill="currentColor"/>';
    return s;
  };
  const iconPlay = () => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true");
    s.innerHTML = '<path d="M8 5v14l11-7L8 5Z" fill="currentColor"/>';
    return s;
  };

  const VIEW = { "34": "three-quarter view", front: "front view", hero: "hero shot", blueprint: "side blueprint view",
    exploded: "exploded view", stowed: "stowed view, wings folded", aerial: "aerial view", railside: "railside aerial view",
    street: "street-level view" };
  const viewOf = (src) => {
    const key = src.replace(/^.*\/[a-z0-9]+-|\.webp$/g, "");
    return VIEW[key] || "render";
  };
  const pieceCard = (p, videos) => {
    const imgs = [p.poster, ...(p.gallery || [])];
    const altFor = (src) => `${p.title}, ${p.subtitle}: ${viewOf(src)} (render)`;
    const main = el("img", { src: p.poster, alt: altFor(p.poster), loading: "lazy", width: 1600, height: 900 });
    if (p.id === "astroscout") main.classList.add("contain");

    const thumbs = el("div", { class: "piece__thumbs", role: "group", "aria-label": `${p.title} renders` });
    imgs.forEach((src, i) => {
      const b = el("button", { type: "button", "aria-pressed": i === 0 ? "true" : "false", "aria-label": `Show the ${viewOf(src)} (${i + 1} of ${imgs.length})` },
        el("img", { src, alt: "", loading: "lazy" }));
      b.addEventListener("click", () => {
        main.src = src;
        main.alt = altFor(src);
        thumbs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      });
      thumbs.append(b);
    });

    const badges = el("div", { class: "badges" },
      p.kind === "multi" ? el("span", { class: "badge badge--multi", text: "4 agents" }) : null,
      p.fan_concept ? el("span", { class: "badge badge--fan", text: "Unofficial fan concept" }) : null,
      p.unofficial_study ? el("span", { class: "badge badge--fan", text: "Unofficial study" }) : null,
      p.warmup ? el("span", { class: "badge badge--warm", text: "Warm-up · before 08:59 CET" }) : null);

    const vid = videoId(videos, p.video_key);
    const actions = el("div", { class: "piece__actions" },
      p.glb ? el("button", { class: "btn btn--primary btn--small", type: "button", onclick: () => open3D(p) }, icon3d(), "View in 3D") : null,
      vid ? el("button", { class: "btn btn--ghost btn--small", type: "button", onclick: () => openVideo(p, vid) }, iconPlay(), "Watch the take") : null,
      p.next ? el("a", { class: "btn btn--ghost btn--small", href: p.next.href }, p.next.label) : null);

    const agents = p.per_agent ? el("ul", { class: "piece__agents", "aria-label": "Verified edits per agent role" },
      p.per_agent.map((a) => el("li", {}, `${a.role} `, el("b", { text: a.verified })))) : null;
    const agentsNote = p.per_agent_note ? el("p", { class: "fine piece__note", text: p.per_agent_note }) : null;

    const honest = p.honest && p.honest.length ? el("details", { class: "honest" },
      el("summary", { text: "What went wrong, and what was not Gemini" }),
      el("ul", { class: "piece__honest" }, p.honest.map((h) => el("li", { text: h })))) : null;

    return el("article", { class: "piece", id: `piece-${p.id}`, "aria-labelledby": `h-${p.id}` },
      el("div", { class: `piece__media piece__media--${p.id}` }, main, badges, imgs.length > 1 ? thumbs : null),
      el("div", { class: "piece__body" },
        el("div", { class: "piece__title" }, el("h3", { id: `h-${p.id}`, text: p.title }), el("p", { text: p.subtitle })),
        el("ul", { class: "piece__meta" },
          el("li", {}, el("span", { class: "who who--gemini", text: "Gemini" }), ` ${p.agent}`),
          el("li", { text: `${p.start}–${p.end} CET · ${p.duration}` })),
        el("p", { class: "piece__blurb", text: p.blurb }),
        el("dl", { class: "facts" }, p.facts.map((f, i) => factEl(f, p.id, i))),
        agents, agentsNote, honest, actions));
  };

  /* ---------- multi-agent ---------- */
  const takeCard = (p) => {
    const bars = el("ul", { class: "bars" }, p.per_agent.map((a) => {
      const [v, t] = a.verified.split("/").map(Number);
      const pct = t ? Math.round((v / t) * 100) : 0;
      return el("li", { class: "bar" },
        el("span", { text: a.role }),
        el("span", { class: "bar__track", role: "img", "aria-label": `${a.role}: ${v} of ${t} edits verified` },
          el("span", { class: "bar__fill", style: `width:${pct}%` })),
        el("span", { class: "bar__val", text: a.verified }));
    }));
    const sum = el("ul", { class: "take__sum" }, p.facts.slice(0, 3).map((f) => el("li", {}, `${f.label}: `, el("b", { text: f.value }))));
    return el("article", { class: "take" },
      el("h3", { text: p.title }),
      el("p", { text: `${p.agent} · ${p.start}–${p.end} CET · verified edits per role` }),
      bars, sum, p.per_agent_note ? el("p", { class: "fine piece__note", text: p.per_agent_note }) : null);
  };

  /* ---------- copy ---------- */
  const fillCopy = (copy) => {
    if (!copy) return;
    document.querySelectorAll("[data-copy]").forEach((n) => {
      const v = n.dataset.copy.split(".").reduce((o, k) => (o ? o[k] : undefined), copy);
      if (typeof v === "string" && v) n.textContent = v; else n.hidden = true;
    });
    const steps = (sel, list) => {
      const ol = $(sel);
      if (!ol || !Array.isArray(list)) return;
      ol.replaceChildren(...list.map((s) => el("li", {}, el("h3", { text: s.title }), el("p", { text: s.body }))));
    };
    steps("#engine-steps", copy.engine && copy.engine.steps);
    steps("#multi-steps", copy.multi && copy.multi.steps);
  };

  /* ---------- engineering sections (data/sections.json) ---------- */
  const CODE_RE = /(\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\bvp_[a-z_]+\b|\b[\w-]+\.(?:jsonl|json|py|md|mp4|sh)\b|\b(?:t_video_s|rec_start_epoch|digest_match|action_seq|based_on_frame|bpy\.app\.timers)\b)/g;
  const withCode = (text) => {
    const p = el("p");
    let last = 0;
    String(text).replace(CODE_RE, (m, _g, idx) => {
      if (idx > last) p.append(text.slice(last, idx));
      p.append(el("code", { text: m }));
      last = idx + m.length;
      return m;
    });
    if (last < text.length) p.append(text.slice(last));
    return p;
  };
  const fillSections = (sections) => {
    if (!sections || !Array.isArray(sections.sections)) return;
    for (const s of sections.sections) {
      const root = document.querySelector(`[data-section="${s.id}"]`);
      if (!root) continue;
      const set = (sel, txt) => { const n = root.querySelector(sel); if (n && txt) n.textContent = txt; };
      set(".js-eyebrow", s.eyebrow); set(".js-title", s.title); set(".js-lede", s.summary || s.lede);
      const blocks = root.querySelector(".js-blocks");
      if (blocks && Array.isArray(s.blocks)) {
        const cards = s.blocks.map((b) => el("article", { class: "block" },
          el("h3", { text: b.title }), ...String(b.body).split(/\n\n+/).map((para) => withCode(para))));
        const more = el("details", { class: "more" },
          el("summary", { text: `Read the details (${s.blocks.length} short notes)` }),
          s.summary && s.lede ? el("p", { class: "more__lede", text: s.lede }) : null,
          el("div", { class: "blocks" }, ...cards));
        blocks.replaceWith(more);
      }
      const facts = root.querySelector(".js-facts");
      if (facts) {
        if (Array.isArray(s.facts) && s.facts.length) {
          facts.replaceChildren(...s.facts.map((f, i) => factEl(f, s.id, i)));
        } else facts.hidden = true;
      }
      const lim = root.querySelector(".js-limits");
      if (lim) {
        if (Array.isArray(s.limits) && s.limits.length) {
          lim.replaceChildren(el("span", { class: "nc__h", text: "Not claimed" }), ...s.limits.map((t) => el("span", { class: "nc__chip", text: t })));
        } else lim.hidden = true;
      }
    }
  };

  const init = async () => {
    const [data, videos, copy, sections] = await Promise.all([
      getJSON("data/pieces.json"), getJSON("data/videos.json"), getJSON("data/copy.json"), getJSON("data/sections.json"),
    ]);
    fillCopy(copy);
    fillSections(sections);

    const grid = $("#pieces-grid");
    if (data && Array.isArray(data.pieces)) {
      grid.replaceChildren(...data.pieces.map((p) => pieceCard(p, videos || {})));
      const multi = data.pieces.filter((p) => p.kind === "multi" && p.per_agent);
      $("#takes").replaceChildren(...multi.map(takeCard));
    } else {
      grid.replaceChildren(el("p", { class: "fine", text: "The piece list could not be loaded." }));
    }

    const mainId = videoId(videos, "main");
    if (mainId) {
      const fig = $("#hero-media");
      const big = fig.querySelector(".mosaic__big") || fig.querySelector("img");
      const poster = (big.querySelector("img") || big).getAttribute("src");
      const player = ytFacade(mainId, "ViewPoint: the hackathon demo", poster);
      player.classList.add("mosaic__big");
      big.replaceWith(player);
      fig.querySelector("figcaption").textContent = "The hackathon demo video. Below: what Gemini built.";
      const li = $("#foot-main-video");
      li.querySelector("a").href = `https://www.youtube.com/watch?v=${mainId}`;
      li.hidden = false;
    }
    document.querySelectorAll(".vslot[data-video-key]").forEach((box) => {
      const id = videoId(videos, box.dataset.videoKey);
      if (!id) return;
      box.append(ytFacade(id, "Watch the take", box.dataset.poster));
      box.hidden = false;
    });
    const engId = videoId(videos, "engine");
    if (engId) {
      const box = $("#engine-video");
      box.append(ytFacade(engId, "The Blender engine at work", "assets/img/live-orbital-gui.webp"));
      box.hidden = false;
    }
  };

  init();

  /* ---------- silent looping clips: play only while on screen ---------- */
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const loops = [...document.querySelectorAll("video.js-loop")];
  if (!reduce && "IntersectionObserver" in window) {
    const vio = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const v = e.target;
        if (e.isIntersecting) { if (v.preload === "none") v.preload = "auto"; v.play().catch(() => {}); }
        else v.pause();
      }
    }, { threshold: 0.35 });
    loops.forEach((v) => vio.observe(v));
  } else {
    loops.forEach((v) => { v.controls = true; });
  }

  /* ---------- fight moves: contact sheets + 3D clips ---------- */
  const MOVES = [["idle_guard", "Guard"], ["jab_L", "Jab"], ["cross_R", "Cross"], ["hook_L", "Hook"], ["uppercut_R", "Uppercut"],
    ["front_kick_R", "Front kick"], ["side_kick_L", "Side kick"], ["roundhouse_R", "Roundhouse"], ["block_high", "Block"],
    ["hit_react", "Hit react"], ["victory", "Victory"]];
  const ROBOTS = { astroscout: { name: "AstroScout", prefix: "AS", glb: "assets/models/astroscout-fight.glb", mb: "0.9" },
    gemdroid: { name: "GemDroid", prefix: "GD", glb: "assets/models/gemdroid-fight.glb", mb: "4.6" } };
  const movesRoot = $("#moves");
  if (movesRoot) {
    let robot = "astroscout", move = "roundhouse_R", mv = null;
    const chips = movesRoot.querySelector(".moves__chips");
    const sheet = $("#moves-sheet"), view = $("#moves-view"), play = $("#moves-play");
    const label = (m) => (MOVES.find((x) => x[0] === m) || [m, m])[1];
    const render = () => {
      chips.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.move === move ? "true" : "false"));
      movesRoot.querySelectorAll("[data-robot]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.robot === robot ? "true" : "false"));
      sheet.src = `assets/img/moves/${robot}-${move}.webp`;
      sheet.alt = `Contact sheet of ${ROBOTS[robot].name}'s ${label(move).toLowerCase()}: five frames, the hit frame marked.`;
      play.textContent = `▶ Play it in 3D (${ROBOTS[robot].mb} MB)`;
      if (mv) {
        const src = ROBOTS[robot].glb;
        const clip = `${ROBOTS[robot].prefix}_${move}`;
        if (mv.getAttribute("src") !== src) mv.setAttribute("src", src);
        mv.setAttribute("animation-name", clip);
        mv.setAttribute("alt", `${ROBOTS[robot].name} performing ${label(move).toLowerCase()} in 3D`);
        if (mv.loaded) mv.play({ repetitions: Infinity });
      }
    };
    chips.replaceChildren(...MOVES.map(([id, name]) => {
      const b = el("button", { type: "button", "data-move": id, "aria-pressed": "false", text: name });
      b.addEventListener("click", () => { move = id; render(); });
      return b;
    }));
    movesRoot.querySelectorAll("[data-robot]").forEach((b) => b.addEventListener("click", () => { robot = b.dataset.robot; render(); }));
    play.addEventListener("click", async () => {
      play.disabled = true; play.textContent = "Loading the 3D viewer…";
      try { await loadModelViewer(); } catch { play.textContent = "The 3D viewer could not load"; return; }
      mv = el("model-viewer", { "camera-controls": true, "touch-action": "pan-y", autoplay: true, "shadow-intensity": "1",
        "environment-image": "neutral", exposure: "1", "interaction-prompt": "none", "camera-orbit": "30deg 80deg auto" });
      mv.addEventListener("load", () => mv.play({ repetitions: Infinity }));
      view.replaceChildren(mv);
      view.classList.add("is-3d");
      render();
    });
    render();
  }

  // Highlight the nav link of the section in view. Passive and read-only: it never moves the page.
  const links = [...document.querySelectorAll(".nav a")]
    .map((a) => ({ a, s: document.getElementById(a.getAttribute("href").slice(1)) }))
    .filter((x) => x.s);
  let current = null, ticking = false;
  const mark = () => {
    ticking = false;
    const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    let pick = null;
    if (atEnd) pick = links[links.length - 1];
    else for (const x of links) if (x.s.getBoundingClientRect().top <= window.innerHeight * 0.4) pick = x;
    if (pick === current) return;
    if (current) current.a.removeAttribute("aria-current");
    current = pick;
    if (!pick) return;
    pick.a.setAttribute("aria-current", "true");
    // Scroll only the nav strip sideways.
    const strip = pick.a.closest("nav");
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const l = pick.a.offsetLeft, r = l + pick.a.offsetWidth;
      if (l < strip.scrollLeft) strip.scrollLeft = l - 8;
      else if (r > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = r - strip.clientWidth + 24;
    }
  };
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(mark); } }, { passive: true });
  window.addEventListener("resize", mark, { passive: true });
  mark();
})();
