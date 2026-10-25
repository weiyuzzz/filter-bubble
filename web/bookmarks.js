/* Filter Bubble — 我的收藏：文件夹分类 + 在收藏里搜索 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const toasts = $("#bmToasts");

  const state = {
    folder: "all",
    query: "",
    folders: [],
    items: [],
    counts: { total: 0, unfiled: 0, folders: 0 },
    busy: false,
    timer: null,
    loaded: false,
    formMode: "create",
    formId: null,
    armedFolder: null,
    armedTimer: null,
  };

  const ICONS = {
    open: '<svg viewBox="0 0 24 24" fill="none"><path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    pen: '<svg viewBox="0 0 24 24" fill="none"><path d="m4 20 1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none"><path d="m5 12.5 4.2 4.2L19 7" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5m0 3h.01M10.3 4.2 2.7 18a1.4 1.4 0 0 0 1.3 2h16a1.4 1.4 0 0 0 1.3-2L13.7 4.2a1.4 1.4 0 0 0-2.4 0Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  const nf = new Intl.NumberFormat("zh-CN");

  /* ---------------- 小工具 ---------------- */
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function compact(n) {
    n = Number(n || 0);
    if (n >= 10000) {
      const v = n / 10000;
      return (v >= 10 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, "")) + "万";
    }
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return nf.format(n);
  }

  function timeAgo(iso) {
    if (!iso) return "—";
    const then = new Date(iso);
    if (Number.isNaN(then.getTime())) return "—";
    const sec = Math.max(0, (Date.now() - then.getTime()) / 1000);
    if (sec < 90) return "刚刚";
    if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
    if (sec < 86400) return Math.round(sec / 3600) + " 小时前";
    if (sec < 86400 * 30) return Math.round(sec / 86400) + " 天前";
    if (sec < 86400 * 365) return Math.round(sec / 86400 / 30) + " 个月前";
    return Math.round(sec / 86400 / 365) + " 年前";
  }

  function hueFor(text) {
    let h = 0;
    const value = String(text || "");
    for (let i = 0; i < value.length; i += 1) h = (h * 31 + value.charCodeAt(i)) % 360;
    return h;
  }

  function toast(message, kind = "ok", timeout = 3800) {
    const el = document.createElement("div");
    el.className = "toast" + (kind === "error" ? " error" : "");
    el.innerHTML = `${kind === "error" ? ICONS.alert : ICONS.check}<span>${esc(message)}</span>`;
    toasts.appendChild(el);
    while (toasts.children.length > 3) toasts.firstElementChild.remove();
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 320);
    }, timeout);
  }

  async function api(path, options = {}) {
    const opts = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (_e) { data = {}; }
    if (!res.ok && res.status !== 202) throw new Error(data.error || `请求失败 (${res.status})`);
    return { status: res.status, data };
  }

  /* ---------------- 读取 ---------------- */
  async function load({ silent = false } = {}) {
    if (state.busy) return;
    state.busy = true;
    try {
      const qs = new URLSearchParams();
      qs.set("folder", String(state.folder));
      if (state.query) qs.set("q", state.query);
      const { data } = await api("/api/bookmarks?" + qs.toString());
      state.folders = data.folders || [];
      state.items = data.items || [];
      state.counts = data.counts || { total: 0, unfiled: 0, folders: 0 };
      state.loaded = true;
      render();
    } catch (err) {
      if (!silent) toast(err.message, "error");
      if (!state.loaded) {
        $("#bmTotal").textContent = "读取失败";
        $("#bmResult").textContent = "读取失败";
        $("#bmList").innerHTML = "";
        $("#bmEmpty").classList.remove("hidden");
        $("#bmEmptyTitle").textContent = "收藏读不出来";
        $("#bmEmptyText").textContent = `${err.message}。确认本地服务还在运行，然后点右上角「刷新」。`;
      }
    } finally {
      state.busy = false;
    }
  }

  /* ---------------- 渲染 ---------------- */
  function filterLabel() {
    if (state.folder === "none") return "未分类";
    const folder = state.folders.find((f) => String(f.id) === String(state.folder));
    return folder ? folder.name : "全部收藏";
  }

  function renderFolders() {
    const total = state.counts.total || 0;
    const unfiled = state.counts.unfiled || 0;
    const rows = [
      `<button class="bm-folder${state.folder === "all" ? " is-active" : ""}" type="button"
               data-folder="all" aria-pressed="${state.folder === "all" ? "true" : "false"}">
         <span class="bm-folder-name">全部收藏</span>
         <span class="bm-folder-count">${total}</span>
       </button>`,
      `<button class="bm-folder${state.folder === "none" ? " is-active" : ""}" type="button"
               data-folder="none" aria-pressed="${state.folder === "none" ? "true" : "false"}">
         <span class="bm-folder-name">未分类</span>
         <span class="bm-folder-count">${unfiled}</span>
       </button>`,
    ];
    state.folders.forEach((f) => {
      const id = String(f.id);
      const active = state.folder === id;
      const armed = state.armedFolder === id;
      rows.push(`<div class="bm-folder-row">
        <button class="bm-folder${active ? " is-active" : ""}" type="button"
                data-folder="${esc(id)}" aria-pressed="${active ? "true" : "false"}">
          <span class="bm-folder-name">${esc(f.name)}</span>
          <span class="bm-folder-count">${f.count}</span>
        </button>
        <button class="bm-folder-act" type="button" data-rename="${esc(id)}"
                title="重命名 ${esc(f.name)}" aria-label="重命名 ${esc(f.name)}">${ICONS.pen}</button>
        <button class="bm-folder-act${armed ? " is-armed" : ""}" type="button" data-delete="${esc(id)}"
                title="${armed ? "再点一次就删除" : "删除文件夹，里面的收藏回到未分类"}"
                aria-label="删除文件夹 ${esc(f.name)}">${armed ? ICONS.check : ICONS.trash}</button>
      </div>`);
    });
    $("#bmFolders").innerHTML = rows.join("");
  }

  function renderItem(item) {
    const full = item.full_name || "";
    const parts = full.split("/");
    const owner = item.owner || parts[0] || "";
    const name = item.name || parts[1] || full;
    const url = item.url || `https://github.com/${full}`;
    const initials = owner.replace(/[^a-zA-Z0-9]/g, "").slice(0, 2).toUpperCase() || "GH";
    const desc = item.description || "收藏时这个项目还没有填写简介。";
    const topics = (item.topics || []).slice(0, 4);
    const folderId = item.folder_id == null ? "none" : String(item.folder_id);
    const stars = Number(item.stars || 0);
    const forks = Number(item.forks || 0);
    const options = [`<option value="none"${folderId === "none" ? " selected" : ""}>未分类</option>`]
      .concat(state.folders.map((f) => (
        `<option value="${f.id}"${String(f.id) === folderId ? " selected" : ""}>${esc(f.name)}</option>`
      ))).join("");
    const tags = topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("");
    return `<article class="card bm-card" data-repo="${esc(full)}">
      <div class="repo-head">
        <div class="avatar" style="--h:${hueFor(owner)}" aria-hidden="true">
          <span>${esc(initials)}</span>
          <img src="${esc(item.avatar || `https://github.com/${owner}.png?size=96`)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">
        </div>
        <div class="repo-id">
          <a class="repo-name" href="${esc(url)}" target="_blank" rel="noreferrer"><span class="owner">${esc(owner)} /</span> ${esc(name)}</a>
          <div class="repo-sub">
            ${item.language ? `<span>${esc(item.language)}</span>` : ""}
            <span>★ ${compact(stars)}</span>
            <span>收藏于 ${esc(timeAgo(item.created_at))}</span>
          </div>
        </div>
      </div>
      <p class="desc">${esc(desc)}</p>
      ${tags ? `<div class="tags">${tags}</div>` : ""}
      <div class="card-stats">
        <div class="stat"><strong>${compact(stars)}</strong><span>总计 star</span></div>
        <div class="stat"><strong>${compact(forks)}</strong><span>Fork</span></div>
        <div class="insight">${item.folder_name ? "文件夹：" + esc(item.folder_name) : "未分类"}</div>
      </div>
      <div class="card-foot">
        <div class="reason"><span class="text" title="${esc(full)}">${esc(full)}</span></div>
        <div class="actions bm-actions">
          <select class="bm-move" data-move="${esc(full)}" title="移动到文件夹" aria-label="把 ${esc(full)} 移动到文件夹">${options}</select>
          <a class="act ext" href="${esc(url)}" target="_blank" rel="noreferrer" title="在 GitHub 打开" aria-label="在 GitHub 打开 ${esc(full)}">${ICONS.open}</a>
          <button class="act bm-remove" type="button" data-remove="${esc(full)}" title="从收藏里移除" aria-label="从收藏里移除 ${esc(full)}">${ICONS.trash}</button>
        </div>
      </div>
    </article>`;
  }

  function render() {
    const total = state.counts.total || 0;
    const count = state.items.length;
    renderFolders();

    $("#bmTotal").textContent = `${total} 个收藏 · ${state.counts.folders || 0} 个文件夹`;
    const qualifier = state.query ? `「${state.query}」` : "";
    $("#bmResult").textContent = total
      ? `${count} / ${total} 个收藏 · ${filterLabel()}${qualifier ? " · 搜索 " + qualifier : ""}`
      : "还没有收藏";
    $("#bmFolderHint").textContent = state.counts.folders
      ? `${state.counts.folders} 个文件夹 · 未分类 ${state.counts.unfiled} 个`
      : "点「新建」按主题分类收藏";

    const list = $("#bmList");
    const empty = $("#bmEmpty");
    if (!count) {
      list.innerHTML = "";
      empty.classList.remove("hidden");
      if (!total) {
        $("#bmEmptyTitle").textContent = "还没有收藏";
        $("#bmEmptyText").textContent = "在今日榜单的卡片上点书签图标，把想回看的项目留在这里；收藏会把项目当时的简介、语言、star 一起存下来。";
      } else if (state.query) {
        $("#bmEmptyTitle").textContent = "这个搜索没有结果";
        $("#bmEmptyText").textContent = `收藏里没有匹配「${state.query}」的项目，搜索范围是名称、简介、语言和 topics。`;
      } else {
        $("#bmEmptyTitle").textContent = "这个文件夹还是空的";
        $("#bmEmptyText").textContent = "把收藏卡片右下角的文件夹选择器改一下，就能把它移到这里。";
      }
      return;
    }
    empty.classList.add("hidden");
    list.innerHTML = state.items.map(renderItem).join("");
  }

  function renderLoading() {
    $("#bmList").innerHTML = '<div class="skel-card"></div><div class="skel-card"></div>';
    $("#bmResult").textContent = "正在读取收藏…";
  }

  /* ---------------- 搜索 ---------------- */
  function syncSearchClear() {
    const clear = $("#bmSearchClear");
    if (clear) clear.hidden = !$("#bmSearch").value;
  }

  function submitSearch() {
    const query = $("#bmSearch").value.trim();
    syncSearchClear();
    if (query === state.query) return;
    state.query = query;
    load();
  }

  /* ---------------- 文件夹 ---------------- */
  function openFolderForm(mode, id) {
    state.formMode = mode;
    state.formId = id || null;
    const folder = state.folders.find((f) => String(f.id) === String(id));
    $("#bmFolderFormLabel").textContent = mode === "rename" ? "改文件夹名" : "新文件夹名";
    $("#bmFolderCreate").textContent = mode === "rename" ? "保存" : "创建";
    $("#bmFolderName").value = mode === "rename" && folder ? folder.name : "";
    $("#bmFolderForm").hidden = false;
    $("#bmFolderName").focus();
    $("#bmFolderName").select();
  }

  function closeFolderForm() {
    state.formMode = "create";
    state.formId = null;
    $("#bmFolderForm").hidden = true;
    $("#bmFolderName").value = "";
  }

  async function submitFolderForm() {
    const name = $("#bmFolderName").value.trim();
    if (!name) {
      toast("文件夹名不能为空", "error");
      $("#bmFolderName").focus();
      return;
    }
    const mode = state.formMode;
    const body = mode === "rename"
      ? { action: "rename", id: state.formId, name }
      : { action: "create", name };
    try {
      await api("/api/bookmarks/folder", { method: "POST", body: JSON.stringify(body) });
      closeFolderForm();
      toast(mode === "rename" ? `文件夹已改名为「${name}」` : `已新建文件夹「${name}」`);
      await load({ silent: true });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteFolder(id) {
    const folder = state.folders.find((f) => String(f.id) === String(id));
    const label = folder ? folder.name : "这个文件夹";
    if (state.armedFolder !== String(id)) {
      // 和「清空口味记忆」一样的二次确认：删文件夹会把里面的收藏退回未分类
      state.armedFolder = String(id);
      clearTimeout(state.armedTimer);
      state.armedTimer = setTimeout(() => {
        state.armedFolder = null;
        renderFolders();
      }, 3500);
      renderFolders();
      toast(`再点一次「✓」确认删除「${label}」（收藏会退回未分类，不会删掉）`, "ok", 3500);
      return;
    }
    state.armedFolder = null;
    clearTimeout(state.armedTimer);
    try {
      await api("/api/bookmarks/folder", { method: "POST", body: JSON.stringify({ action: "delete", id }) });
      if (String(state.folder) === String(id)) state.folder = "all";
      toast(`已删除文件夹「${label}」，里面的收藏回到未分类`);
      await load({ silent: true });
    } catch (err) {
      toast(err.message, "error");
      renderFolders();
    }
  }

  /* ---------------- 移动 / 移除 ---------------- */
  async function moveItem(repo, folderId, select) {
    if (select) select.disabled = true;
    try {
      await api("/api/bookmarks/move", {
        method: "POST",
        body: JSON.stringify({ repo, folder_id: folderId }),
      });
      const target = folderId && folderId !== "none"
        ? (state.folders.find((f) => String(f.id) === String(folderId)) || {}).name || "文件夹"
        : "未分类";
      toast(`已把 ${repo} 移到「${target}」`);
      await load({ silent: true });
    } catch (err) {
      toast(err.message, "error");
      if (select) select.disabled = false;
    }
  }

  async function removeItem(repo) {
    try {
      await api("/api/bookmark", { method: "POST", body: JSON.stringify({ repo }) });
      toast(`已把 ${repo} 从收藏里移除`);
      await load({ silent: true });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    $("#bmSearch").addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      submitSearch();
    });
    $("#bmSearch").addEventListener("input", () => {
      syncSearchClear();
      if (state.timer) clearTimeout(state.timer);
      state.timer = setTimeout(submitSearch, 350);
    });
    $("#bmSearch").addEventListener("search", () => submitSearch());
    $("#bmSearchBtn").addEventListener("click", submitSearch);
    $("#bmSearchClear").addEventListener("click", () => {
      $("#bmSearch").value = "";
      syncSearchClear();
      submitSearch();
    });
    $("#bmReload").addEventListener("click", () => load());

    $("#bmFolderNew").addEventListener("click", () => {
      if (!$("#bmFolderForm").hidden && state.formMode === "create") {
        closeFolderForm();
        return;
      }
      openFolderForm("create");
    });
    $("#bmFolderCreate").addEventListener("click", submitFolderForm);
    $("#bmFolderCancel").addEventListener("click", closeFolderForm);
    $("#bmFolderName").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); submitFolderForm(); }
      if (event.key === "Escape") { event.preventDefault(); closeFolderForm(); }
    });

    $("#bmFolders").addEventListener("click", (event) => {
      const rename = event.target.closest("[data-rename]");
      if (rename) return openFolderForm("rename", rename.dataset.rename);
      const del = event.target.closest("[data-delete]");
      if (del) return deleteFolder(del.dataset.delete);
      const pick = event.target.closest("[data-folder]");
      if (!pick) return;
      state.folder = pick.dataset.folder;
      closeFolderForm();
      load({ silent: true });
    });

    $("#bmList").addEventListener("click", (event) => {
      const remove = event.target.closest("[data-remove]");
      if (remove) {
        event.preventDefault();
        return removeItem(remove.dataset.remove);
      }
    });
    $("#bmList").addEventListener("change", (event) => {
      const select = event.target.closest("[data-move]");
      if (!select) return;
      moveItem(select.dataset.move, select.value, select);
    });
  }

  async function init() {
    bind();
    syncSearchClear();
    renderLoading();
    await load();
  }

  init();
})();