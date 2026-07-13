(function () {
  "use strict";

  const pages = [
    ["Command", "/ui/"],
    ["Founder", "/ui/founder.html"],
    ["Approvals", "/ui/approvals.html"],
    ["Actions", "/ui/actions.html"],
    ["Ledger", "/ui/ledger.html"],
    ["Commitments", "/ui/commitments.html"],
    ["Attention", "/ui/surfacing.html"],
    ["Connectors", "/ui/connectors.html"],
    ["Memory", "/ui/memory.html"],
    ["Trading", "/ui/trading.html"],
    ["Voice", "/ui/voice.html"],
    ["Skills", "/ui/skills.html"],
    ["Evals", "/ui/evals.html"],
    ["Charters", "/ui/charter.html"],
    ["Notifications", "/ui/notifications.html"],
    ["Ops", "/ui/ops.html"],
  ];

  const canonicalPath = (href) => {
    try {
      const url = new URL(href, window.location.origin);
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

  const formatKernelError = (text) => {
    const raw = String(text || "").trim();
    if (!raw || raw[0] !== "{" && raw[0] !== "[") return raw;
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

  const injectWorkspaceNav = () => {
    if (document.querySelector(".zade-workspace-nav")) return;
    const header = document.querySelector("header");
    if (!header) return;

    const nav = document.createElement("nav");
    nav.className = "zade-workspace-nav";
    nav.setAttribute("aria-label", "Zade workspace");

    const active = currentPath();
    pages.forEach(([label, href]) => {
      const a = document.createElement("a");
      a.href = href;
      a.textContent = label;
      if (canonicalPath(href) === active) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    });

    header.insertAdjacentElement("afterend", nav);

    const canonical = new Set(pages.map(([, href]) => canonicalPath(href)));
    header.querySelectorAll('a.button[href^="/ui"], a.button[href="/ui"]').forEach((link) => {
      const href = link.getAttribute("href") || "";
      if (href.includes("#")) return;
      if (canonical.has(canonicalPath(href))) link.classList.add("zade-header-link-hidden");
    });

    header.querySelectorAll(".actions, div").forEach((el) => {
      if (!el.getAttribute("aria-label") && el.querySelector("button")) {
        el.setAttribute("aria-label", "Page actions");
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
      if (shouldNormalize) normalizeMessages(document);
      if (shouldMarkButtons) markButtons();
    });
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ["disabled", "class"],
      childList: true,
      characterData: true,
      subtree: true,
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
    document.body.classList.add("zade-enhanced-ui");
    injectSkipLink();
    injectWorkspaceNav();
    normalizeMessages(document);
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
