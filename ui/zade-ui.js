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
    system: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
  };

  const GROUPS = [
    { label: null, pages: [["Command", "index", I.command]] },
    {
      label: "Operate",
      pages: [
        ["Founder", "founder", I.founder],
        ["Approvals", "approvals", I.approvals],
        ["Ledger", "ledger", I.ledger],
        ["Commitments", "commitments", I.commitments],
        ["Attention", "surfacing", I.surfacing]
      ]
    },
    {
      label: "Intelligence",
      pages: [
        ["Memory", "memory", I.memory],
        ["Trading", "trading", I.trading],
        ["Voice", "voice", I.voice]
      ]
    },
    {
      label: "Govern",
      pages: [
        ["System", "system", I.system]
      ]
    }
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

  const injectSidebar = () => {
    if (document.querySelector(".zade-sidebar")) return;

    const aside = el("aside", "zade-sidebar");
    aside.setAttribute("aria-label", "Zade workspace");

    const logo = el("a", "zade-sidebar-logo");
    logo.href = href("index");
    logo.innerHTML =
      '<div class="zade-mark">Z</div>' +
      '<div><div class="zade-name">Zade</div><div class="zade-tag">Local AI Co-founder</div></div>';
    aside.appendChild(logo);

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

  const boot = () => {
    // Type system 1b is the default: serif panel headings.
    // Opt out per page with <body data-zade-type="sans">.
    if (!document.body.hasAttribute("data-zade-type")) {
      document.body.setAttribute("data-zade-type", "serif");
    }
    injectSkipLink();
    injectSidebar();
    hideHeaderWorkspaceLinks();
    normalizeMessages(document);
    normalizeSummaries(document);
    markButtons();
    observeUi();
    bootstrapToken();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
