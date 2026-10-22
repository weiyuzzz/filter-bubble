/* Filter Bubble — 应用外壳行为（两个模块共用）
   主题切换 / 侧边栏折叠与移动端抽屉 / 卡片-列表视图切换 / 顶栏阴影
   与具体业务无关，业务逻辑仍由 app.js、radar.js 负责。 */
(() => {
  "use strict";

  const THEMES = ["light", "dark", "auto"];
  const THEME_LABEL = { light: "浅色", dark: "深色", auto: "跟随系统" };
  const root = document.documentElement;
  const $ = (sel, scope = document) => scope.querySelector(sel);

  /* ---------------- 主题 ---------------- */
  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function applyTheme(mode) {
    const resolved = mode === "auto" ? (systemPrefersDark() ? "dark" : "light") : mode;
    root.setAttribute("data-theme", resolved);
    root.setAttribute("data-theme-mode", mode);
    const button = $("#themeToggle");
    if (button) {
      button.setAttribute("aria-label", `主题：${THEME_LABEL[mode]}（点击切换）`);
      button.setAttribute("title", `主题：${THEME_LABEL[mode]}`);
      button.dataset.mode = mode;
      const label = button.querySelector(".theme-label");
      if (label) label.textContent = THEME_LABEL[mode];
    }
    try { localStorage.setItem("fb.theme", mode); } catch (e) { /* 隐私模式忽略 */ }
  }

  function currentTheme() {
    // ?theme=light|dark|auto 可直接指定主题（便于分享与截图），不写入本地存储
    try {
      const fromUrl = new URLSearchParams(window.location.search).get("theme");
      if (THEMES.includes(fromUrl)) return fromUrl;
    } catch (e) { /* ignore */ }
    try { return localStorage.getItem("fb.theme") || "light"; } catch (e) { return "light"; }
  }

  function cycleTheme() {
    const now = root.getAttribute("data-theme-mode") || currentTheme();
    const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
    applyTheme(next);
  }

  applyTheme(currentTheme());
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if ((root.getAttribute("data-theme-mode") || "light") === "auto") applyTheme("auto");
    });
  }

  /* ---------------- 侧边栏 ---------------- */
  const navState = () => {
    try { return localStorage.getItem("fb.nav") || "expanded"; } catch (e) { return "expanded"; }
  };

  function setNav(state) {
    root.setAttribute("data-nav", state);
    try { localStorage.setItem("fb.nav", state); } catch (e) { /* ignore */ }
    const toggle = $("#sidebarToggle");
    if (toggle) toggle.setAttribute("aria-expanded", String(state !== "collapsed"));
  }

  function setNavForViewport() {
    const stored = navState();
    if (window.innerWidth <= 860) {
      root.setAttribute("data-nav", "closed");
    } else {
      root.setAttribute("data-nav", stored === "collapsed" ? "collapsed" : "expanded");
    }
  }

  setNavForViewport();

  function toggleNav() {
    if (window.innerWidth <= 860) {
      const open = root.getAttribute("data-nav") === "open";
      root.setAttribute("data-nav", open ? "closed" : "open");
      document.body.classList.toggle("nav-open", !open);
    } else {
      setNav(root.getAttribute("data-nav") === "collapsed" ? "expanded" : "collapsed");
    }
  }

  window.addEventListener("resize", () => {
    if (window.innerWidth <= 860) {
      if (!["open", "closed"].includes(root.getAttribute("data-nav"))) {
        root.setAttribute("data-nav", "closed");
      }
    } else {
      setNavForViewport();
      document.body.classList.remove("nav-open");
    }
  });

  /* ---------------- 卡片 / 列表视图 ---------------- */
  const VIEW_KEY = "fb.view";

  function applyView(view) {
    const list = $("#cardList");
    if (list) list.classList.toggle("is-list", view === "list");
    document.querySelectorAll("[data-view-mode]").forEach((button) => {
      const active = button.dataset.viewMode === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* ignore */ }
  }

  function savedView() {
    try { return localStorage.getItem(VIEW_KEY) || "cards"; } catch (e) { return "cards"; }
  }

  function mountViewToggle() {
    const tools = $(".feed-tools");
    if (!tools || $(".view-toggle")) return;
    const seg = document.createElement("div");
    seg.className = "seg view-toggle";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "视图切换");
    seg.innerHTML = `
      <button class="seg-btn" type="button" data-view-mode="cards" aria-pressed="true" title="卡片视图">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="4" width="8" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="13" y="4" width="8" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="3" y="13" width="8" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/><rect x="13" y="13" width="8" height="7" rx="1.5" stroke="currentColor" stroke-width="1.6"/></svg>
        <span>卡片</span>
      </button>
      <button class="seg-btn" type="button" data-view-mode="list" aria-pressed="false" title="紧凑列表视图">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
        <span>列表</span>
      </button>`;
    tools.prepend(seg);
    seg.addEventListener("click", (event) => {
      const button = event.target.closest("[data-view-mode]");
      if (!button) return;
      applyView(button.dataset.viewMode);
    });
    applyView(savedView());
  }

  /* ---------------- 顶栏滚动阴影 ---------------- */
  const topbar = $(".topbar");
  if (topbar) {
    const onScroll = () => topbar.classList.toggle("scrolled", window.scrollY > 8);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ---------------- 全局键盘 ---------------- */
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (root.getAttribute("data-nav") === "open") {
      root.setAttribute("data-nav", "closed");
      document.body.classList.remove("nav-open");
      return;
    }
    document.dispatchEvent(new CustomEvent("fb:escape"));
  });

  /* ---------------- 绑定 ---------------- */
  function bind() {
    const themeBtn = $("#themeToggle");
    if (themeBtn) themeBtn.addEventListener("click", cycleTheme);

    const navBtn = $("#sidebarToggle");
    if (navBtn) navBtn.addEventListener("click", toggleNav);

    const navToggle = $("#navToggle");
    if (navToggle) navToggle.addEventListener("click", toggleNav);

    document.addEventListener("click", (event) => {
      if (!window.innerWidth || window.innerWidth > 860) return;
      if (root.getAttribute("data-nav") !== "open") return;
      const sidebar = $(".sidebar");
      if (!sidebar) return;
      if (sidebar.contains(event.target) || event.target.closest("#sidebarToggle")) return;
      root.setAttribute("data-nav", "closed");
      document.body.classList.remove("nav-open");
    });

    mountViewToggle();
  }

  // 暴露少量通用能力，供页面按需使用
  window.FBShell = {
    theme: () => root.getAttribute("data-theme-mode") || currentTheme(),
    setTheme: applyTheme,
    view: savedView,
    setView: applyView,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
