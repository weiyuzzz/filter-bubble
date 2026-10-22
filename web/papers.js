/* 学术论文页（一级导航）：按学科浏览论文 + 每篇的 AI 中文概况 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const toasts = $("#toasts");

  const AI_LABELS = [
    ["research", "研究内容"], ["method", "核心方法"], ["findings", "主要发现"],
    ["significance", "研究意义"], ["limits", "信息局限"],
  ];

  const state = {
    discipline: localStorage.getItem("radar.papers") || "all",
    items: [],
    status: null,
    busy: false,
    timer: null,
  };

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const chip = (text, kind = "") => `<span class="chip ${kind}">${esc(text)}</span>`;

  function toast(message, bad = false) {
    const node = document.createElement("div");
    node.className = "toast" + (bad ? " bad" : "");
    node.textContent = message;
    toasts.appendChild(node);
    setTimeout(() => node.remove(), 4500);
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const text = String(iso);
    return text.length >= 16 ? text.slice(0, 16).replace("T", " ") : text;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!response.ok) throw new Error((data && (data.error || data.message)) || `HTTP ${response.status}`);
    return data;
  }

  function renderDisciplines(status) {
    const box = $("#papersDisciplines");
    if (!box) return;
    const list = (status && status.disciplines) || [];
    const counts = (status && status.counts) || {};
    box.innerHTML = list.map((d) => {
      const total = counts[d.key] || 0;
      const active = d.key === state.discipline;
      return `<button class="papers-chip ${active ? "is-active" : ""}" role="tab"
        aria-selected="${active}" data-discipline="${esc(d.key)}">${esc(d.label)}
        <span class="count-pill">${total}</span></button>`;
    }).join("");
    const crumb = $("#papersCrumb");
    if (crumb) {
      const found = list.find((d) => d.key === state.discipline);
      crumb.textContent = found ? found.label : "全部论文";
    }
  }

  function aiBlock(paper) {
    const ai = paper.ai || {};
    if (ai.status === "done" && ai.overview) {
      const rows = AI_LABELS
        .filter(([key]) => ai.overview[key])
        .map(([key, label]) => `<div class="paper-ai-row"><span>${label}</span><p>${esc(ai.overview[key])}</p></div>`)
        .join("");
      return `<div class="paper-ai">
        <div class="paper-ai-head">AI 中文概况 ${chip(ai.model || "模型", "info")}</div>
        ${rows}
      </div>`;
    }
    if (ai.status === "failed") {
      return `<div class="paper-ai is-failed">
        <div class="paper-ai-head">AI 概况生成失败</div>
        <p class="paper-ai-error">${esc(ai.error || "未知错误")}</p>
        <button class="ghost-btn paper-retry" type="button" data-retry="${esc(paper.id)}">重试</button>
      </div>`;
    }
    return `<div class="paper-ai is-pending">
      <div class="paper-ai-head">AI 中文概况</div>
      <p class="pending">${ai.status === "queued" ? "已排队，等待分析…" : "正在分析…"}</p>
    </div>`;
  }

  function card(paper) {
    const url = paper.url || (paper.doi ? "https://doi.org/" + paper.doi : "");
    const labels = (paper.discipline_labels || []).map((x) => chip(x)).join("");
    const source = paper.venue || paper.source;
    return `<article class="paper-card">
      <h3 class="paper-title">${url
        ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(paper.title)}</a>`
        : esc(paper.title)}</h3>
      <div class="paper-meta">
        ${labels}
        ${chip(source)}
        ${paper.published_at ? `<span class="paper-date">${esc(paper.published_at)}</span>` : ""}
      </div>
      <div class="paper-abstract-block">
        <div class="paper-label">原始摘要</div>
        <p class="paper-abstract">${paper.abstract ? esc(paper.abstract) : "该来源没有提供摘要"}</p>
      </div>
      ${aiBlock(paper)}
      ${url ? `<a class="paper-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a>` : ""}
    </article>`;
  }

  function renderNotice(data, status) {
    const box = $("#papersNotice");
    if (!box) return;
    const parts = [];
    const failed = ((status && status.sources) || []).filter((s) => s.status !== "ok");
    if (failed.length) {
      parts.push(`<span class="chip warn">来源失败：${failed.map((s) => esc(s.source)).join("、")}</span>`);
    }
    const stats = (status && status.stats) || {};
    if (stats.total) {
      parts.push(chip(`共 ${stats.total} 篇`));
      parts.push(chip(`摘要覆盖 ${Math.round((stats.abstract_ratio || 0) * 100)}%`));
    }
    if (stats.ai_done) parts.push(chip(`已完成 AI 概况 ${stats.ai_done} 篇`, "ok"));
    if (stats.ai_failed) parts.push(chip(`AI 失败 ${stats.ai_failed} 篇`, "warn"));
    if (status && status.queue) parts.push(chip(`队列 ${status.queue}`));
    const budget = (status && status.ai_budget) || {};
    if (budget.limit) {
      parts.push(chip(`今日 AI 额度 ${budget.used || 0}/${budget.limit}`));
    }

    let banner = "";
    const errors = ((data && data.papers) || []).map((p) => (p.ai || {}).error || "").filter(Boolean);
    if (errors.length) {
      const quota = errors.some((e) => /rate limit|额度|quota/i.test(e));
      banner = quota
        ? `<div class="papers-banner">模型额度不足（免费模型有每日上限），论文原文与摘要仍可正常查看。额度恢复后点卡片上的「重试」即可生成概况。</div>`
        : `<div class="papers-banner">AI 概况暂时失败：${esc(errors[0].slice(0, 80))}</div>`;
    }
    box.innerHTML = (parts.length ? `<div class="ev-meta">${parts.join("")}</div>` : "") + banner;
  }

  function renderSources(status) {
    const box = $("#papersSources");
    if (!box) return;
    const runs = (status && status.sources) || [];
    if (!runs.length) { box.innerHTML = ""; return; }
    const rows = runs.map((r) => `<tr>
      <td>${esc(r.source)}</td>
      <td>${r.status === "ok" ? chip("正常", "ok") : chip(r.status === "running" ? "采集中" : "失败", "warn")}</td>
      <td class="mono">${r.fetched ?? "—"}</td>
      <td class="mono">${r.with_abstract ?? "—"}</td>
      <td class="mono">${r.duration_ms ? r.duration_ms + "ms" : "—"}</td>
      <td class="mono">${esc(fmtTime(r.finished_at || r.started_at))}</td>
      <td class="mono">${esc((r.error || "").slice(0, 60))}</td>
    </tr>`).join("");
    box.innerHTML = `<div class="section-title">论文来源状态</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>来源</th><th>状态</th><th>获取</th><th>含摘要</th><th>耗时</th><th>最近</th><th>错误</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }

  function applyStatusLine(status) {
    const dot = $("#papersDot");
    const label = $("#papersState");
    if (!dot || !label) return;
    const running = status && status.state && status.state.running;
    if (running) {
      label.textContent = status.state.stage || "运行中";
      dot.classList.add("live");
    } else {
      dot.classList.remove("live");
      label.textContent = "就绪";
    }
    const updated = $("#papersUpdated");
    if (updated) {
      const stamp = ((status && status.stats) || {}).last_fetched_at || "";
      updated.textContent = stamp ? "更新于 " + fmtTime(stamp) : "";
    }
  }

  async function load() {
    const result = $("#papersResult");
    if (!result) return;
    try {
      const [data, status] = await Promise.all([
        api(`/api/radar/papers?discipline=${encodeURIComponent(state.discipline)}&limit=10`),
        api("/api/radar/papers/status"),
      ]);
      state.status = status;
      state.items = data.papers || [];
      renderDisciplines(status);
      renderNotice(data, status);
      renderSources(status);
      applyStatusLine(status);

      if (!state.items.length) {
        result.innerHTML = `<div class="empty-state"><div class="empty-mark">∅</div>
          <h3>${esc(data.label || "该学科")}还没有论文</h3>
          <p>点右上角「采集」抓取最近几天的公开论文；如果来源都失败，下方会显示具体错误。</p></div>`;
      } else {
        result.innerHTML = `<div class="papers-list">${state.items.map(card).join("")}</div>`;
      }

      const pending = state.items.some((p) => ["pending", "queued"].includes((p.ai || {}).status));
      if (state.timer) clearTimeout(state.timer);
      if (pending) state.timer = setTimeout(load, 6000);
    } catch (error) {
      result.innerHTML = `<div class="error-state"><div class="empty-mark">!</div>
        <h3>论文加载失败</h3><p>${esc(error.message)}</p>
        <button class="ghost-btn" id="papersRetryLoad" type="button">重试</button></div>`;
      const retry = $("#papersRetryLoad");
      if (retry) retry.addEventListener("click", load);
    }
  }

  async function collect() {
    if (state.busy) return;
    state.busy = true;
    const button = $("#papersCollectBtn");
    if (button) button.disabled = true;
    try {
      const data = await api("/api/radar/papers/collect", { method: "POST", body: "{}" });
      toast(data.started ? "已开始采集论文…" : "已有采集任务在跑");
      for (let i = 0; i < 80; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const status = await api("/api/radar/papers/status");
        applyStatusLine(status);
        if (!(status.state && status.state.running)) break;
      }
      toast("采集完成");
      await load();
    } catch (error) {
      toast("采集失败：" + error.message, true);
    } finally {
      state.busy = false;
      if (button) button.disabled = false;
    }
  }

  async function retry(id) {
    try {
      await api("/api/radar/papers/retry", { method: "POST", body: JSON.stringify({ id }) });
      toast("已重新排队分析");
      load();
    } catch (error) {
      toast("重试失败：" + error.message, true);
    }
  }

  document.addEventListener("click", (event) => {
    const discipline = event.target.closest("[data-discipline]");
    if (discipline) {
      state.discipline = discipline.dataset.discipline;
      localStorage.setItem("radar.papers", state.discipline);
      load();
      return;
    }
    const retryBtn = event.target.closest("[data-retry]");
    if (retryBtn) retry(retryBtn.dataset.retry);
  });

  $("#papersCollectBtn").addEventListener("click", collect);
  $("#papersRefreshBtn").addEventListener("click", load);

  load();
})();
