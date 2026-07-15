/* ============================================================
   ZADE UI v2 — shared enhancement layer
   Drop-in replacement for ui/zade-ui.js.
   Injects the Command-style left sidebar on every utility page,
   plus the same a11y/error normalization the v1 file had.
   No page markup changes required.
   ============================================================ */
(function () {
  "use strict";

  // When served from the kernel, pages live at /ui/…; in a file
  // preview they sit side by side. Detect once, link accordingly.
  const servedFromKernel = window.location.pathname.startsWith("/ui");
  const href = (file) => (servedFromKernel ? (file === "index" ? "/ui/" : "/ui/" + file + ".html") : (file === "index" ? "index.html" : file + ".html"));

  const I = {
    command: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    founder: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6.5 8-6.5s8 2.5 8 6.5"/>',
    approvals: '<path d="M20 6 9 17l-5-5"/>',
    ledger: '<path d="M12 2 3 6l9 4 9-4-9-4z"/><path d="m3 12 9 4 9-4"/><path d="m3 18 9 4 9-4"/>',
    commitments: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    surfacing: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4l2.5 2.5"/>',
    memory: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    trading: '<path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/>',
    voice: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 19v3"/>',
    system: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    browser: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    vault: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="12" cy="12" r="4"/><path d="M12 8v1M12 15v1M8 12h1M15 12h1"/>',
    research: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v3l2 2"/>',
    brief: '<path d="M4 4h11l5 5v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/><path d="M14 4v5h5"/><path d="M8 13h7M8 17h7"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>'
  };

  // Redesign: 10 destinations folded into 6. No group labels at this count.
  //   Home     = index.html   (Command's chat + all of Voice)
  //   Inbox    = inbox.html    (Approvals + Action Plans + Commitments + attention queue)
  //   Strategy = strategy.html (Founder + Ledger)
  //   Memory   = memory.html   (restyled)
  //   Trading  = trading.html  (restyled, advanced tools tucked away)
  //   Settings = settings.html (System + notifications)
  // Icons are reused as-is: Inbox borrows approvals, Strategy borrows ledger,
  // Settings borrows system — no new icon assets introduced.
  const GROUPS = [
    {
      label: null,
      pages: [
        ["Home", "index", I.command],
        ["Inbox", "inbox", I.approvals],
        ["Strategy", "strategy", I.ledger],
        ["Memory", "memory", I.memory],
        ["Trading", "trading", I.trading],
        ["Settings", "settings", I.system]
      ]
    },
    {
      label: "Ops",
      pages: [
        ["Brief", "digest", I.brief],
        ["Browser", "browser", I.browser],
        ["Vault", "vault", I.vault],
        ["Research", "research", I.research]
      ]
    }
  ];

  // Every reachable page, for the command palette. The sidebar shows the
  // folded 6 + Ops; the palette also reaches the standalone consoles.
  const ALL_PAGES = [
    ["Home", "index"], ["The brief", "digest"], ["Inbox", "inbox"], ["Strategy", "strategy"], ["Memory", "memory"],
    ["Trading", "trading"], ["Settings", "settings"], ["Browser", "browser"], ["Vault", "vault"],
    ["Research", "research"], ["Approvals console", "approvals"], ["Founder ops", "founder"],
    ["Operating ledger", "ledger"], ["Commitments", "commitments"], ["Attention & notifications", "surfacing"],
    ["System", "system"], ["Voice", "voice"]
  ];

  const canonicalPath = (link) => {
    try {
      const url = new URL(link, window.location.origin);
      if (url.pathname === "/ui") return "/ui/";
      return url.pathname;
    } catch {
      return "";
    }
  };

  const currentPath = () => {
    if (window.location.pathname === "/ui") return "/ui/";
    return window.location.pathname;
  };

  const isCurrent = (link) => {
    const target = canonicalPath(link);
    const here = currentPath();
    if (target === here) return true;
    // file-preview fallback: compare basenames
    const base = (p) => p.split("/").pop() || "index.html";
    return !servedFromKernel && base(target) === base(here);
  };

  const formatKernelError = (text) => {
    const raw = String(text || "").trim();
    if (!raw || (raw[0] !== "{" && raw[0] !== "[")) return raw;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed === "string") return parsed;
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (Array.isArray(parsed.detail)) {
        return parsed.detail
          .map((item) => item && (item.msg || item.detail || item.message))
          .filter(Boolean)
          .join(" ");
      }
      if (parsed && typeof parsed.message === "string") return parsed.message;
    } catch {
      return raw;
    }
    return raw;
  };

  const normalizeMessages = (root) => {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(".error, .notice.err, [data-error]").forEach((el) => {
      const formatted = formatKernelError(el.textContent);
      if (formatted && formatted !== el.textContent.trim()) el.textContent = formatted;
      if (!el.hasAttribute("aria-live")) el.setAttribute("aria-live", "polite");
    });
  };

  const injectSkipLink = () => {
    if (document.querySelector(".zade-skip")) return;
    const main = document.querySelector("main");
    if (!main) return;
    if (!main.id) main.id = "zade-main";
    const link = document.createElement("a");
    link.className = "zade-skip";
    link.href = "#" + main.id;
    link.textContent = "Skip to content";
    document.body.insertBefore(link, document.body.firstChild);
  };

  const el = (tag, className, html) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (html != null) node.innerHTML = html;
    return node;
  };

  const svgIcon = (paths) =>
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";

  const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ============================================================
  //  Character layer — ambient breathe, presence line, optional
  //  cursor glow. The presence line is Zade's heartbeat: a
  //  serif-italic first-person status rotating under the logo.
  // ============================================================
  const PRESENCE_LINES = [
    "Awake. I don’t sleep.",
    "Watching the doors.",
    "Everything’s where you left it.",
    "I’ve got you, Ellie.",
  ];

  const injectAmbient = () => {
    if (document.querySelector(".zade-ambient")) return;
    document.body.insertBefore(el("div", "zade-ambient"), document.body.firstChild);
  };

  // Cursor-following glow — the founder disabled it in review, so it ships
  // OFF and only wakes when Settings flips localStorage.zadeCursorGlow to "on".
  const injectCursorGlow = () => {
    if (localStorage.getItem("zadeCursorGlow") !== "on") return;
    if (document.querySelector(".zade-cursor-glow")) return;
    const glow = el("div", "zade-cursor-glow");
    document.body.appendChild(glow);
    window.addEventListener("mousemove", (event) => {
      glow.style.transform = "translate(" + event.clientX + "px, " + event.clientY + "px)";
    });
  };

  const injectPresence = (aside) => {
    const wrap = el("div", "zade-presence");
    wrap.innerHTML =
      '<span class="zade-presence-dot" aria-hidden="true"></span>' +
      '<span class="zade-presence-line" data-presence-line aria-live="off"></span>';
    aside.appendChild(wrap);
    const line = wrap.querySelector("[data-presence-line]");
    let idx = Math.floor(Date.now() / 5200) % PRESENCE_LINES.length;
    line.textContent = PRESENCE_LINES[idx];
    window.setInterval(() => {
      idx = (idx + 1) % PRESENCE_LINES.length;
      line.textContent = PRESENCE_LINES[idx];
    }, 5200);
  };

  // ============================================================
  //  Activity beacon — surfaces the work Zade does off-thread.
  //  The chat/voice surface only shows the founder-facing reply;
  //  the contrarian pass, operating loops, cadence reviews,
  //  self-evals, and the work queue never appear there. This
  //  reads the runtime's own trail (all plain GETs, no token,
  //  no backend changes) and renders a live pulse + recent feed
  //  in the shared sidebar, so every page shows what's running.
  // ============================================================
  let activityRefs = null; // sidebar monitor
  let stripRefs = null; // narrow-screen mirror
  let pollTimer = null;
  let feedOpen = false;
  let offlineStreak = 0;

  // A runtime.respond IS the chat/voice reply — everything else is off-thread.
  const SURFACED_OPS = new Set(["runtime.respond"]);
  const OP_LABELS = {
    "runtime.respond": "Answering you",
    "runtime.contrarian": "Contrarian check",
    "evals.case": "Self-evaluation",
    "ops.model_benchmark": "Benchmarking models",
  };
  const EVENT_LABELS = {
    "runtime.operating_loop": "Operating loop",
    "runtime.cadence": "Cadence review",
    "runtime.respond": "Answering you",
  };

  const humanizeOp = (value) => {
    const tail = String(value || "").split(".").pop().replace(/[_-]+/g, " ").trim();
    return tail ? tail.charAt(0).toUpperCase() + tail.slice(1) : "Activity";
  };

  const relTime = (iso) => {
    const t = new Date(iso).getTime();
    if (!t || Number.isNaN(t)) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    const m = Math.round(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.round(m / 60);
    if (h < 24) return h + "h ago";
    return Math.round(h / 24) + "d ago";
  };

  const fetchJSON = async (path) => {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  };

  // Merge model-call telemetry and runtime events into one time-sorted trail.
  // runtime.respond is logged in both places; keep the telemetry copy (it carries
  // latency) and drop the duplicate event so a single reply isn't listed twice.
  const normalizeActivity = (modelCalls, events) => {
    const rows = [];
    for (const call of modelCalls) {
      const silent = !SURFACED_OPS.has(call.operation);
      const latency = Number(call.latency_ms || 0);
      rows.push({
        at: call.created_at,
        label: OP_LABELS[call.operation] || humanizeOp(call.operation),
        status: call.status || "ok",
        silent,
        detail: latency ? Math.round(latency / 100) / 10 + "s" : call.model || "",
      });
    }
    for (const event of events) {
      if (event.event_type === "runtime.respond") continue;
      rows.push({
        at: event.created_at,
        label: EVENT_LABELS[event.event_type] || humanizeOp(event.event_type),
        status: event.status || "ok",
        silent: true,
        detail: String(event.message || ""),
      });
    }
    rows.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
    return rows;
  };

  const injectActivityMonitor = (aside) => {
    const wrap = el("div", "zade-activity");
    wrap.setAttribute("data-state", "idle");
    wrap.innerHTML =
      '<button type="button" class="zade-activity-head" aria-expanded="false" aria-controls="zade-activity-feed" title="What Zade is doing off-thread">' +
        '<span class="zade-beacon" aria-hidden="true"></span>' +
        '<span class="zade-activity-copy">' +
          '<span class="zade-activity-title" data-act-title>Activity</span>' +
          '<span class="zade-activity-detail" data-act-detail aria-live="polite">Checking…</span>' +
        "</span>" +
        '<span class="zade-activity-chip" data-act-chip hidden></span>' +
        '<svg class="zade-activity-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>' +
      "</button>" +
      '<div class="zade-activity-feed" id="zade-activity-feed" role="region" aria-label="Recent Zade activity"></div>';
    aside.appendChild(wrap);
    const head = wrap.querySelector(".zade-activity-head");
    const feed = wrap.querySelector(".zade-activity-feed");
    head.addEventListener("click", () => {
      feedOpen = !feedOpen;
      head.setAttribute("aria-expanded", String(feedOpen));
      feed.classList.toggle("open", feedOpen);
    });
    activityRefs = {
      wrap,
      title: wrap.querySelector("[data-act-title]"),
      detail: wrap.querySelector("[data-act-detail]"),
      chip: wrap.querySelector("[data-act-chip]"),
      feed,
    };
  };

  const injectStripBeacon = (nav) => {
    const beacon = el("span", "zade-strip-beacon");
    beacon.setAttribute("data-state", "idle");
    beacon.innerHTML =
      '<span class="zade-beacon" aria-hidden="true"></span>' +
      '<span class="zade-strip-label" data-strip-label aria-live="polite">Idle</span>';
    nav.insertBefore(beacon, nav.firstChild);
    stripRefs = { wrap: beacon, label: beacon.querySelector("[data-strip-label]") };
  };

  const renderFeed = (rows) => {
    if (!activityRefs) return;
    if (!rows.length) {
      activityRefs.feed.innerHTML = '<div class="zade-activity-empty">No recent runtime activity.</div>';
      return;
    }
    activityRefs.feed.innerHTML = rows
      .slice(0, 8)
      .map((row) => {
        const statusClass = row.status && row.status !== "ok" ? " error" : "";
        const meta = [row.silent ? "off-thread" : "in chat", row.detail].filter(Boolean).join(" · ");
        return (
          '<div class="zade-activity-row">' +
          '<span class="zade-activity-status' + statusClass + '" aria-hidden="true"></span>' +
          '<span class="zade-activity-op">' +
            '<span class="op-name">' + esc(row.label) + "</span>" +
            (meta ? '<span class="op-meta">' + esc(meta) + "</span>" : "") +
          "</span>" +
          '<time datetime="' + esc(row.at) + '">' + esc(relTime(row.at)) + "</time>" +
          "</div>"
        );
      })
      .join("");
  };

  const setActivityState = (state, title, detail, chip) => {
    if (activityRefs) {
      activityRefs.wrap.setAttribute("data-state", state);
      activityRefs.title.textContent = title;
      activityRefs.detail.textContent = detail;
      if (chip) {
        activityRefs.chip.textContent = chip;
        activityRefs.chip.hidden = false;
      } else {
        activityRefs.chip.hidden = true;
      }
    }
    if (stripRefs) {
      stripRefs.wrap.setAttribute("data-state", state);
      stripRefs.label.textContent = state === "working" ? detail || "Working" : title;
    }
  };

  const tickActivity = async () => {
    let queue = null;
    let events = null;
    let calls = null;
    try {
      [queue, events, calls] = await Promise.all([
        fetchJSON("/work/queue?limit=1").catch(() => null),
        fetchJSON("/runtime/events?limit=10").catch(() => null),
        fetchJSON("/models/telemetry/calls?limit=12").catch(() => null),
      ]);
    } catch {
      queue = events = calls = null;
    }
    if (!queue && !events && !calls) {
      offlineStreak += 1;
      setActivityState("offline", "Kernel offline", "No signal from the runtime", "");
      renderFeed([]);
      return;
    }
    offlineStreak = 0;
    const counts = (queue && queue.counts) || {};
    const rows = normalizeActivity((calls && calls.items) || [], (events && events.events) || []);
    renderFeed(rows);

    const running = Number(counts.running || 0);
    const pending = Number(counts.pending || 0);
    const freshestAt = rows.length ? new Date(rows[0].at).getTime() : 0;
    const ageMs = freshestAt ? Date.now() - freshestAt : Infinity;
    const working = running > 0 || ageMs <= 12000;
    const recent = !working && ageMs <= 5 * 60 * 1000;
    const chip = pending > 0 ? pending + " queued" : "";

    if (working) {
      setActivityState("working", "Working", "Sweeping the operating layer for you", chip);
    } else if (rows.length) {
      const lead = rows[0].silent ? rows[0].label + " · " : "Last active ";
      setActivityState(recent ? "recent" : "idle", "Idle", lead + relTime(rows[0].at), chip);
    } else {
      setActivityState("idle", "Idle", "No recent activity", chip);
    }
  };

  // Self-scheduling poll: 5s when the runtime is live, backing off to 20s while
  // it's unreachable (file preview / kernel down), and paused while the tab is
  // hidden so a backgrounded page isn't hammering loopback.
  const startActivityPoller = () => {
    if (pollTimer) return;
    let notifTick = 0;
    const loop = async () => {
      pollTimer = null;
      if (!document.hidden) {
        await tickActivity();
        // Refresh the unread badge on boot and then roughly every 15s, not every
        // activity tick — the count moves far slower than the work queue.
        if (notifTick % 3 === 0) refreshNotifBadge();
        notifTick += 1;
      }
      const delay = document.hidden ? 2000 : offlineStreak > 2 ? 20000 : 5000;
      pollTimer = window.setTimeout(loop, delay);
    };
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { tickActivity(); refreshNotifBadge(); }
    });
    loop();
  };

  // ============================================================
  //  Frameless titlebar — the shell runs decorations:false, so the
  //  universe draws its own chrome: a full-width drag region + Zade
  //  window controls. Injected ONLY under Tauri (window.__TAURI__);
  //  in a browser / kernel-served fallback the native chrome stays.
  //  Controls call the shell's own win_* commands over the same IPC
  //  bridge as the kernel proxy — close = hide-to-tray (resident).
  // ============================================================
  const inTauri = () => !!(window.__TAURI__ && window.__TAURI__.core && typeof window.__TAURI__.core.invoke === "function");
  // Only the production shell is frameless. The init-script bridge (BRIDGE_JS in
  // the shell's main.rs) sets window.__ZADE_FRAMELESS__ by comparing against the
  // fixed kernel origin, so this is correct whatever Tauri's asset host is. In the
  // dev loop (ZADE_DEV_UI) the flag is false → keep OS chrome, no custom bar (which
  // would otherwise double-stack, since withGlobalTauri injects __TAURI__ there).
  // Defaults to showing the bar if the flag is somehow absent (better than a bare
  // window); a plain browser has no __TAURI__ so it's skipped anyway.
  const isFramelessShell = () => inTauri() && window.__ZADE_FRAMELESS__ !== false;

  const TB_ICONS = {
    min: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg>',
    max: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5.5" y="5.5" width="13" height="13" rx="1.5"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>'
  };

  const injectTitlebar = () => {
    if (!isFramelessShell() || document.querySelector(".zade-titlebar")) return;
    const invoke = window.__TAURI__.core.invoke;
    const bar = el("div", "zade-titlebar");
    bar.setAttribute("data-tauri-drag-region", "");
    const title = el("div", "zade-titlebar-title", "Zade");
    title.setAttribute("data-tauri-drag-region", "");
    const controls = el("div", "zade-titlebar-controls");
    const mkBtn = (kind, cmd, label, extra) => {
      const btn = el("button", "zade-tb-btn" + (extra ? " " + extra : ""), TB_ICONS[kind]);
      btn.type = "button";
      btn.title = label;
      btn.setAttribute("aria-label", label);
      btn.addEventListener("click", () => { try { invoke(cmd); } catch (e) { /* IPC gone */ } });
      return btn;
    };
    controls.appendChild(mkBtn("min", "win_minimize", "Minimize"));
    controls.appendChild(mkBtn("max", "win_toggle_maximize", "Maximize / restore"));
    controls.appendChild(mkBtn("close", "win_hide", "Close to tray", "zade-tb-close"));
    bar.appendChild(title);
    bar.appendChild(controls);
    document.body.insertBefore(bar, document.body.firstChild);
    document.body.classList.add("zade-has-titlebar");

    // Rust emits the new fullscreen state on immersive toggle; hide our chrome
    // while immersed so the world fills the screen.
    if (window.__TAURI__.event && window.__TAURI__.event.listen) {
      window.__TAURI__.event.listen("zade://immersive", (evt) => {
        document.body.classList.toggle("zade-immersive", !!evt.payload);
      });
    }
  };

  const injectSidebar = () => {
    if (document.querySelector(".zade-sidebar")) return;

    const aside = el("aside", "zade-sidebar");
    aside.setAttribute("aria-label", "Zade workspace");

    const header = el("div", "zade-sidebar-header");
    const logo = el("a", "zade-sidebar-logo");
    logo.href = href("index");
    logo.innerHTML =
      '<div class="zade-mark">Z</div>' +
      '<div><div class="zade-name">Zade</div><div class="zade-tag">Local AI Co-founder</div></div>';
    header.appendChild(logo);
    header.appendChild(makeBell());
    aside.appendChild(header);

    // Zade's heartbeat: the rotating first-person presence line.
    injectPresence(aside);

    GROUPS.forEach((group) => {
      const wrap = el("div", "zade-nav-group");
      if (group.label) wrap.appendChild(el("div", "zade-nav-kicker", group.label));
      const nav = document.createElement("nav");
      group.pages.forEach(([label, file, icon]) => {
        const a = el("a", "zade-nav-item");
        a.href = href(file);
        a.innerHTML = svgIcon(icon) + "<span>" + label + "</span>";
        if (isCurrent(a.getAttribute("href"))) a.setAttribute("aria-current", "page");
        nav.appendChild(a);
      });
      wrap.appendChild(nav);
      aside.appendChild(wrap);
    });

    // Activity monitor sits just above the footer status rows. Its own
    // margin-top:auto claims the free space, pinning it and the footer together
    // to the bottom of the rail.
    injectActivityMonitor(aside);

    const foot = el("div", "zade-sidebar-foot");
    foot.innerHTML =
      '<div class="zade-status"><div class="zade-dot" data-kernel-dot></div><span data-kernel-label>Kernel · checking…</span></div>' +
      '<div class="zade-status"><div class="zade-dot"></div><span>Loopback only · no cloud</span></div>';
    aside.appendChild(foot);

    document.body.appendChild(aside);
    document.body.classList.add("zade-has-sidebar");

    // narrow screens fall back to a horizontal strip (CSS decides visibility)
    injectTopStrip();

    // live kernel check for the footer dot
    fetch("/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const dot = aside.querySelector("[data-kernel-dot]");
        const label = aside.querySelector("[data-kernel-label]");
        if (data) {
          dot.classList.add("live");
          label.textContent = "Live · " + (window.location.host || "127.0.0.1:8787");
        } else {
          label.textContent = "Kernel offline";
        }
      })
      .catch(() => {
        const label = aside.querySelector("[data-kernel-label]");
        if (label) label.textContent = "Local only · kernel offline";
      });
  };

  const injectTopStrip = () => {
    if (document.querySelector(".zade-workspace-nav")) return;
    const nav = el("nav", "zade-workspace-nav");
    nav.setAttribute("aria-label", "Zade workspace (compact)");
    GROUPS.forEach((group) => {
      group.pages.forEach(([label, file]) => {
        const a = document.createElement("a");
        a.href = href(file);
        a.textContent = label;
        if (isCurrent(a.getAttribute("href"))) a.setAttribute("aria-current", "page");
        nav.appendChild(a);
      });
    });
    injectStripBeacon(nav);
    const bell = makeBell();
    bell.classList.add("zade-notif-bell-strip");
    nav.appendChild(bell);
    document.body.insertBefore(nav, document.body.firstChild);
  };

  // Pages hand-wrote "+ New X" summaries; the stylesheet draws the plus
  // as a ::before glyph, so strip the literal one to avoid "+ +".
  const normalizeSummaries = (root) => {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("details > summary").forEach((summary) => {
      const first = summary.firstChild;
      if (first && first.nodeType === Node.TEXT_NODE && /^\s*\+\s+/.test(first.nodeValue)) {
        first.nodeValue = first.nodeValue.replace(/^\s*\+\s+/, "");
      }
    });
  };

  // The old per-page header link rows duplicate the sidebar; hide them.
  const hideHeaderWorkspaceLinks = () => {
    const header = document.querySelector("header");
    if (!header) return;
    header.querySelectorAll('a.button[href^="/ui"], a.button[href^="index"], a.button[href$=".html"]').forEach((link) => {
      const linkHref = link.getAttribute("href") || "";
      if (linkHref.includes("#")) return;
      link.classList.add("zade-header-link-hidden");
      link.style.display = "none";
    });
    header.querySelectorAll(".actions, div").forEach((node) => {
      if (!node.getAttribute("aria-label") && node.querySelector("button")) {
        node.setAttribute("aria-label", "Page actions");
      }
    });
  };

  const markButtons = () => {
    document.querySelectorAll("button").forEach((button) => {
      if (!button.type) button.type = "button";
      if (button.disabled) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
    });
  };

  const observeUi = () => {
    const observer = new MutationObserver((mutations) => {
      let shouldNormalize = false;
      let shouldMarkButtons = false;
      mutations.forEach((mutation) => {
        if (mutation.type === "childList") shouldNormalize = true;
        if (mutation.type === "attributes" && mutation.target instanceof HTMLButtonElement) shouldMarkButtons = true;
        if (mutation.type === "characterData") shouldNormalize = true;
      });
      if (shouldNormalize) { normalizeMessages(document); normalizeSummaries(document); }
      if (shouldMarkButtons) markButtons();
    });
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ["disabled", "class"],
      childList: true,
      characterData: true,
      subtree: true
    });
  };

  const bootstrapToken = async () => {
    if (localStorage.getItem("zadeKernelToken")) return;
    try {
      const response = await fetch("/session/token");
      if (!response.ok) return;
      const data = await response.json();
      if (data && data.token) localStorage.setItem("zadeKernelToken", data.token);
    } catch {
      // File previews and non-loopback sessions can run without mutation bootstrap.
    }
  };

  // ============================================================
  //  Notification center — notifications as first-class citizens
  //  of the universe. A bell + live unread badge in the sidebar
  //  (and compact strip) on every page; clicking opens a panel of
  //  actionable cards. Badge polls the tray state (cheap); the
  //  panel pulls the full recent list on open. Mark-read is the
  //  same POST /notifications/{id}/read the surfacing page uses.
  // ============================================================
  let notifRefs = null;
  let notifItems = [];

  const notifHeaders = () => {
    const t = localStorage.getItem("zadeKernelToken") || "";
    const h = { "Content-Type": "application/json" };
    if (t) h["X-Zade-Token"] = t;
    return h;
  };

  const makeBell = () => {
    const btn = el("button", "zade-notif-bell");
    btn.type = "button";
    btn.setAttribute("aria-label", "Notifications");
    btn.title = "Notifications";
    btn.innerHTML = svgIcon(I.bell) + '<span class="zade-notif-badge" data-notif-badge hidden></span>';
    btn.addEventListener("click", (event) => { event.preventDefault(); toggleNotifications(); });
    return btn;
  };

  const setNotifBadge = (count) => {
    document.querySelectorAll("[data-notif-badge]").forEach((badge) => {
      if (count > 0) {
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    });
  };

  const refreshNotifBadge = async () => {
    try {
      const state = await fetchJSON("/tray/state");
      setNotifBadge(Number(state.unread_notifications || 0));
    } catch { /* kernel offline — leave the badge as-is */ }
  };

  const renderNotifCards = () => {
    if (!notifRefs) return;
    if (!notifItems.length) {
      notifRefs.list.innerHTML = '<div class="zade-notif-empty">Nothing to hear about. I keep the noise off your desk on purpose.</div>';
      return;
    }
    notifRefs.list.innerHTML = notifItems
      .map((n) => {
        const unread = !n.read_at;
        const sev = ["info", "warning", "critical"].includes(n.severity) ? n.severity : "info";
        const meta = [esc(n.topic || n.source || ""), esc(relTime(n.created_at))].filter(Boolean).join(" · ");
        return (
          '<div class="zade-notif-card sev-' + sev + (unread ? " unread" : "") + '" data-id="' + esc(n.id) + '">' +
          '<span class="zade-notif-stripe"></span>' +
          '<div class="zade-notif-copy">' +
          '<div class="zade-notif-title">' + esc(n.title || "(untitled)") + "</div>" +
          (n.body ? '<div class="zade-notif-text">' + esc(n.body) + "</div>" : "") +
          '<div class="zade-notif-meta">' + meta + "</div>" +
          "</div>" +
          (unread ? '<button type="button" class="zade-notif-mark" data-read="' + esc(n.id) + '" title="Mark read" aria-label="Mark read">✓</button>' : "") +
          "</div>"
        );
      })
      .join("");
  };

  const loadNotifCards = async () => {
    if (!notifRefs) return;
    try {
      const data = await fetchJSON("/notifications?limit=25");
      const items = data.items || data.notifications || [];
      // Unread first, then most recent — the panel leads with what still wants you.
      notifItems = items.sort((a, b) => {
        const au = a.read_at ? 1 : 0;
        const bu = b.read_at ? 1 : 0;
        if (au !== bu) return au - bu;
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      });
      renderNotifCards();
    } catch (err) {
      notifRefs.list.innerHTML = '<div class="zade-notif-empty">Could not load — ' + esc(formatKernelError(err.message)) + "</div>";
    }
  };

  const markNotifRead = async (id) => {
    try {
      await fetch("/notifications/" + encodeURIComponent(id) + "/read", { method: "POST", headers: notifHeaders(), body: "{}" });
      const item = notifItems.find((n) => String(n.id) === String(id));
      if (item) item.read_at = new Date().toISOString();
      renderNotifCards();
      refreshNotifBadge();
    } catch { /* leave it unread; the badge will resync on next poll */ }
  };

  const markAllNotifRead = async () => {
    const unread = notifItems.filter((n) => !n.read_at);
    if (!unread.length) return;
    notifRefs.markAll.disabled = true;
    await Promise.all(
      unread.map((n) =>
        fetch("/notifications/" + encodeURIComponent(n.id) + "/read", { method: "POST", headers: notifHeaders(), body: "{}" })
          .then(() => { n.read_at = new Date().toISOString(); })
          .catch(() => {})
      )
    );
    notifRefs.markAll.disabled = false;
    renderNotifCards();
    refreshNotifBadge();
  };

  const closeNotifications = () => {
    if (notifRefs) notifRefs.overlay.classList.remove("open");
  };

  const openNotifications = () => {
    if (!notifRefs) injectNotifCenter();
    notifRefs.overlay.classList.add("open");
    loadNotifCards();
  };

  const toggleNotifications = () => {
    if (notifRefs && notifRefs.overlay.classList.contains("open")) closeNotifications();
    else openNotifications();
  };

  const injectNotifCenter = () => {
    if (notifRefs) return;
    const overlay = el("div", "zade-notif-overlay");
    overlay.innerHTML =
      '<div class="zade-notif-panel" role="dialog" aria-label="Notifications">' +
      '<div class="zade-notif-head"><span>Notifications</span>' +
      '<button type="button" class="zade-notif-allread" data-mark-all>Mark all read</button></div>' +
      '<div class="zade-notif-list"></div>' +
      '<a class="zade-notif-foot" href="' + href("surfacing") + '">Open the full history →</a>' +
      "</div>";
    document.body.appendChild(overlay);
    notifRefs = {
      overlay,
      list: overlay.querySelector(".zade-notif-list"),
      markAll: overlay.querySelector("[data-mark-all]")
    };
    overlay.addEventListener("mousedown", (event) => { if (event.target === overlay) closeNotifications(); });
    notifRefs.markAll.addEventListener("click", markAllNotifRead);
    notifRefs.list.addEventListener("click", (event) => {
      const markBtn = event.target.closest("button[data-read]");
      if (markBtn) { markNotifRead(markBtn.dataset.read); return; }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && overlay.classList.contains("open")) closeNotifications();
    });
  };

  // ============================================================
  //  Command palette — Ctrl+K (or Cmd+K) from any page.
  //  Navigation across every surface + a capture-to-memory action,
  //  so the universe is one keystroke deep from anywhere.
  // ============================================================
  let paletteRefs = null;
  let paletteIndex = 0;

  const paletteCommands = (query) => {
    const q = query.trim().toLowerCase();
    const rows = [];
    for (const [label, file] of ALL_PAGES) {
      if (!q || label.toLowerCase().includes(q)) {
        rows.push({ kind: "nav", label: "Go to " + label, hint: href(file), run: () => { window.location.href = href(file); } });
      }
    }
    if (q) {
      rows.push({
        kind: "capture",
        label: 'Remember: "' + query.trim() + '"',
        hint: "saves to memory",
        run: async () => {
          const tokenValue = localStorage.getItem("zadeKernelToken") || "";
          const headers = { "Content-Type": "application/json" };
          if (tokenValue) headers["X-Zade-Token"] = tokenValue;
          const response = await fetch("/memory", {
            method: "POST",
            headers,
            body: JSON.stringify({ kind: "note", title: query.trim(), content: "", source: "palette" })
          });
          if (!response.ok) throw new Error(await response.text());
          const data = await response.json();
          return "Saved — memory #" + data.memory_id;
        }
      });
    }
    return rows.slice(0, 9);
  };

  const renderPalette = () => {
    if (!paletteRefs) return;
    const rows = paletteCommands(paletteRefs.input.value);
    paletteRefs.rows = rows;
    if (paletteIndex >= rows.length) paletteIndex = Math.max(0, rows.length - 1);
    paletteRefs.list.innerHTML = rows.length
      ? rows
          .map(
            (row, i) =>
              '<div class="zade-palette-row' + (i === paletteIndex ? " active" : "") + '" data-idx="' + i + '">' +
              '<span class="zade-palette-label">' + esc(row.label) + "</span>" +
              '<span class="zade-palette-hint">' + esc(row.hint) + "</span></div>"
          )
          .join("")
      : '<div class="zade-palette-empty">Nothing matches.</div>';
  };

  const closePalette = () => {
    if (!paletteRefs) return;
    paletteRefs.overlay.classList.remove("open");
    paletteRefs.input.value = "";
    paletteRefs.note.textContent = "";
    paletteIndex = 0;
  };

  const openPalette = () => {
    if (!paletteRefs) injectPalette();
    paletteRefs.overlay.classList.add("open");
    paletteIndex = 0;
    renderPalette();
    paletteRefs.input.focus();
  };

  const runPaletteRow = async (row) => {
    if (!row) return;
    if (row.kind === "capture") {
      paletteRefs.note.textContent = "Saving…";
      try {
        paletteRefs.note.textContent = await row.run();
        paletteRefs.input.value = "";
        renderPalette();
      } catch (err) {
        paletteRefs.note.textContent = "Save failed: " + formatKernelError(err.message);
      }
      return;
    }
    closePalette();
    row.run();
  };

  const injectPalette = () => {
    if (paletteRefs) return;
    const overlay = el("div", "zade-palette-overlay");
    overlay.innerHTML =
      '<div class="zade-palette" role="dialog" aria-label="Command palette">' +
      '<input type="text" class="zade-palette-input" placeholder="Go anywhere, or type a thought to remember…" aria-label="Command">' +
      '<div class="zade-palette-list"></div>' +
      '<div class="zade-palette-note" aria-live="polite"></div>' +
      '<div class="zade-palette-foot">↑↓ choose · Enter run · Esc close</div>' +
      "</div>";
    document.body.appendChild(overlay);
    paletteRefs = {
      overlay,
      input: overlay.querySelector(".zade-palette-input"),
      list: overlay.querySelector(".zade-palette-list"),
      note: overlay.querySelector(".zade-palette-note"),
      rows: []
    };
    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) closePalette();
    });
    paletteRefs.list.addEventListener("click", (event) => {
      const rowEl = event.target.closest("[data-idx]");
      if (rowEl) runPaletteRow(paletteRefs.rows[Number(rowEl.dataset.idx)]);
    });
    paletteRefs.input.addEventListener("input", () => {
      paletteIndex = 0;
      paletteRefs.note.textContent = "";
      renderPalette();
    });
    paletteRefs.input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); paletteIndex = Math.min(paletteIndex + 1, paletteRefs.rows.length - 1); renderPalette(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); paletteIndex = Math.max(paletteIndex - 1, 0); renderPalette(); }
      else if (event.key === "Enter") { event.preventDefault(); runPaletteRow(paletteRefs.rows[paletteIndex]); }
      else if (event.key === "Escape") { event.preventDefault(); closePalette(); }
    });
  };

  const bindPaletteHotkey = () => {
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (paletteRefs && paletteRefs.overlay.classList.contains("open")) closePalette();
        else openPalette();
      }
    });
  };

  const boot = () => {
    // Type system 1b is the default: serif panel headings.
    // Opt out per page with <body data-zade-type="sans">.
    if (!document.body.hasAttribute("data-zade-type")) {
      document.body.setAttribute("data-zade-type", "serif");
    }
    injectSkipLink();
    injectAmbient();
    injectCursorGlow();
    injectTitlebar();
    injectSidebar();
    hideHeaderWorkspaceLinks();
    normalizeMessages(document);
    normalizeSummaries(document);
    markButtons();
    observeUi();
    bootstrapToken();
    startActivityPoller();
    bindPaletteHotkey();
    // Show the unread badge immediately on load, without waiting for the first
    // visibility-gated poll tick.
    refreshNotifBadge();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
