/* 全球热点雷达 — front-end controller (independent from app.js) */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const toasts = $("#toasts");

  const state = {
    view: localStorage.getItem("radar.view") || "daily",
    report: null,
    status: null,
    poll: null,
    busy: false,
  };

  const nf = new Intl.NumberFormat("zh-CN");

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toast(message, bad = false) {
    const node = document.createElement("div");
    node.className = "toast" + (bad ? " bad" : "");
    node.textContent = message;
    toasts.appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const text = String(iso);
    return text.length >= 16 ? text.slice(0, 16).replace("T", " ") : text;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!response.ok && response.status !== 202) {
      throw new Error((data && (data.error || data.message)) || `HTTP ${response.status}`);
    }
    return data;
  }

  // ---------- 已读标记：打开深入调查后 NEW 消失 ----------
  function markSeen(card) {
    if (!card) return;
    const storyId = card.dataset.story;
    const badge = card.querySelector(".badge-new");
    if (badge) badge.remove();
    if (!storyId) return;
    fetch("/api/seen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "radar", key: storyId }),
    }).catch(() => { /* 离线时忽略，下次打开再标一次 */ });
  }

  // ---------- view switching ----------
  function switchView(name) {
    state.view = name;
    localStorage.setItem("radar.view", name);
    $$(".radar-tab").forEach((tab) => {
      const active = tab.dataset.view === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $$(".radar-view").forEach((view) => {
      view.hidden = view.id !== `view-${name}`;
    });
    if (name === "daily") loadDaily();
    if (name === "sources") loadSources();
    if (name === "history") loadHistory();
    if (name === "settings") loadSettings();
    if (name === "tech") loadTech();
  }

  // ---------- status board ----------
  function applyStatus(status) {
    state.status = status;
    const label = $("#radarState");
    const dot = $("#radarDot");
    const running = status.state && status.state.running;
    if (running) {
      label.textContent = status.state.stage || "运行中";
      dot.classList.add("live");
    } else if (status.state && status.state.last_error) {
      label.textContent = "上次失败";
      dot.classList.remove("live");
    } else {
      label.textContent = status.today_ready ? "今日已更新" : "等待首次采集";
      dot.classList.remove("live");
    }
    const sources = status.sources || {};
    $("#mSources").textContent = sources.ok != null ? sources.ok : "—";
    $("#mFailed").textContent = sources.error != null ? sources.error : "—";
    const updated = $("#radarUpdated");
    if (updated) {
      const stamp = status.last_collect_at;
      updated.textContent = stamp ? "采集于 " + fmtTime(stamp) : "";
    }
    if (status.latest_date && $("#mEvents").textContent === "—") loadDaily();
  }

  async function loadStatus() {
    try {
      const status = await api("/api/radar/status");
      applyStatus(status);
      return status;
    } catch (error) {
      toast("状态读取失败：" + error.message, true);
      return null;
    }
  }

  // ---------- daily view ----------
  function chip(text, kind = "") {
    return `<span class="chip ${kind}">${esc(text)}</span>`;
  }

  function confidenceChip(item) {
    const map = {
      verified_multi: "ok", reported: "info", unverified: "warn",
    };
    return chip(item.confidence_label || item.confidence || "未知状态", map[item.confidence] || "");
  }

  function highlightCard(item) {
    const earliest = item.earliest_source;
    const angles = item.angles || [];
    const seenTitles = new Set();
    const links = (item.sources || [])
      .filter((s) => s.url && (s.url.startsWith("http") || s.url.startsWith("radar://")))
      .filter((s) => {
        const key = (s.title || "").slice(0, 40);
        if (seenTitles.has(key)) return false;
        seenTitles.add(key);
        return true;
      })
      .slice(0, 4)
      .map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" title="${esc(s.title)}">${esc(s.platform)} · ${esc(s.title)}</a>`)
      .join("");
    const otherAngles = angles.slice(1, 4)
      .map((a) => `<span class="chip">${esc((a.title || "").slice(0, 28))}</span>`)
      .join("");
    const trend = item.rank_trend || {};
    const trendChip = {
      up: chip("↑ 升温", "ok"),
      down: chip("↓ 降温", "warn"),
      flat: chip("— 持平"),
      new: chip("新上榜", "info"),
    }[trend.state] || "";
    return `
      <article class="ev-card" data-event="${esc(item.event_id)}" data-story="${esc(item.story_id)}">
        <div class="ev-top">
          <h3 class="ev-title">${esc(item.headline || item.title)}</h3>
          ${item.is_new ? `<span class="badge badge-new">NEW</span>` : ""}
          <span class="ev-heat">🔥 ${esc(item.heat)}</span>
        </div>
        ${item.summary ? `<p class="ev-summary">${esc(item.summary)}</p>` : ""}
        ${(item.points || []).length ? `<div class="ev-meta">${item.points.map((p) => chip(p, "info")).join("")}</div>` : ""}
        <div class="ev-meta">
          ${confidenceChip(item)}
          ${chip(item.category || "综合")}
          ${item.tech_flag ? chip("科技专题", "tech") : ""}
          ${item.angle_count > 1 ? chip(item.angle_count + " 个角度", "info") : ""}
          ${trendChip}
          ${(item.platforms || []).slice(0, 4).map((p) => chip(p)).join("")}
          ${(item.flags || []).map((f) => chip(f, "warn")).join("")}
          ${item.summary_provider && item.summary_provider !== "extractive" ? chip("摘要 " + item.summary_provider, "ok") : ""}
        </div>
        ${otherAngles ? `<div class="ev-meta"><span class="hint">其他角度：</span>${otherAngles}</div>` : ""}
        <div class="ev-links">
          ${earliest && earliest.url ? `<a href="${esc(earliest.url)}" target="_blank" rel="noopener noreferrer">最早可查 · ${esc(earliest.platform)} · ${esc(fmtTime(earliest.published_at))}</a>` : `<span class="chip warn">尚未找到最早来源</span>`}
          ${links}
        </div>
        <div class="ev-actions">
          <button class="ghost-btn" data-action="investigate" data-event="${esc(item.event_id)}">深入调查</button>
          ${item.latest_update && item.latest_update.url ? `<a class="ghost-btn" href="${esc(item.latest_update.url)}" target="_blank" rel="noopener noreferrer">最新进展</a>` : ""}
        </div>
      </article>`;
  }

  function renderDaily(payload) {
    const grid = $("#dailyGrid");
    if (!payload) { grid.innerHTML = `<p class="empty">还没有日报。点击右上角「采集」生成。</p>`; return; }
    state.report = payload;
    const stats = payload.stats || {};
    $("#mEvents").textContent = stats.stories != null ? nf.format(stats.stories) : (stats.events != null ? nf.format(stats.events) : "—");
    $("#mItems").textContent = stats.covered != null ? nf.format(stats.covered) : "—";

    const highlights = payload.highlights || [];
    const zhWeb = payload.zh_web || [];
    const intl = payload.intl || [];
    const tech = payload.tech || [];
    const updates = payload.investigation_updates || [];
    const coverage = payload.coverage || {};
    const gaps = payload.evidence_gaps || [];

    let html = `<div class="section-title">今日重点故事 <small>${esc(payload.date)} · 同一事件的多个角度已合并</small></div>`;
    html += highlights.length
      ? highlights.map(highlightCard).join("")
      : `<p class="empty">今天还没有聚合出事件。</p>`;

    html += `<div class="section-title">中文网络热点 <small>热榜位置不等于真实民意</small></div>`;
    html += zhWeb.length
      ? `<div class="panel"><table class="radar-table"><thead><tr><th>事件</th><th>热度</th><th>平台</th><th>核验</th><th>说明</th></tr></thead><tbody>` +
        zhWeb.map((row) => `<tr>
          <td>${esc(row.title)}</td>
          <td class="mono">${esc(row.heat)}</td>
          <td>${(row.platforms || []).map((p) => chip(p)).join("")}</td>
          <td>${esc(row.confidence || "")}</td>
          <td class="mono">${esc(row.duplication_note || "")}</td>
        </tr>`).join("") + `</tbody></table></div>`
      : `<p class="empty">暂无中文热榜数据。</p>`;

    html += `<div class="section-title">国际热点 <small>关注尚未进入中文信息环境的议题</small></div>`;
    html += intl.length
      ? `<div class="panel"><table class="radar-table"><thead><tr><th>事件</th><th>热度</th><th>平台</th><th>中文报道</th><th>备注</th></tr></thead><tbody>` +
        intl.map((row) => `<tr>
          <td>${esc(row.title)}</td>
          <td class="mono">${esc(row.heat)}</td>
          <td>${(row.platforms || []).map((p) => chip(p)).join("")}</td>
          <td class="mono">${esc(row.chinese_coverage)}</td>
          <td class="mono">${esc(row.note || "")}</td>
        </tr>`).join("") + `</tbody></table></div>`
      : `<p class="empty">暂无国际数据。</p>`;

    html += `<div class="section-title">科技与 AI 专题 <small>标注是否完成独立验证</small></div>`;
    html += tech.length
      ? tech.map((row) => `<div class="panel">
          <h3>${esc(row.title)}</h3>
          <div class="ev-meta">
            ${chip("热度 " + row.heat)}
            ${row.independently_verified ? chip("已多方独立报道", "ok") : chip("未经独立验证", "warn")}
            ${chip("主张 " + row.claims_count + " 项")}
          </div>
          <div class="ev-actions">
            <button class="ghost-btn" data-action="investigate" data-event="${esc(row.event_id)}">科技核验</button>
          </div>
        </div>`).join("")
      : `<p class="empty">今天没有触发科技专题。</p>`;

    if (updates.length) {
      html += `<div class="section-title">事件调查更新</div>`;
      html += `<div class="panel"><ul class="timeline">${updates.map((row) => `
        <li><div class="t-time">${esc(fmtTime(row.updated_at))}</div>
        <div class="t-title">${esc(row.title)}</div>
        <div class="t-meta">${esc(row.claims)} 项主张 · 状态 ${esc((row.statuses || []).join(" / "))}</div></li>`).join("")}</ul></div>`;
    }

    html += `<div class="section-title">数据源覆盖说明</div>`;
    html += `<div class="panel">
      <div class="ev-meta">
        ${chip(`采集 ${stats.collected || 0} 条`)}
        ${chip(`过滤 ${stats.filtered || 0} 条`, stats.filtered ? "warn" : "")}
        ${chip(`入库 ${stats.stored || 0} 条`)}
        ${chip(`覆盖 ${stats.covered || 0} 条`, "ok")}
        ${chip(`${stats.stories || 0} 个故事 / ${stats.events || 0} 个事件`)}
        ${payload.summary_backend && payload.summary_backend.available ? chip("摘要：模型 " + (payload.summary_backend.model || ""), "ok") : chip("摘要：本地抽取式")}
      </div>
      <h4>成功读取（${(coverage.ok || []).length}）</h4>
      <div class="ev-meta">${(coverage.ok || []).map((s) => chip(`${s.platform} ${s.items}`)).join("") || `<span class="empty">无</span>`}</div>
      ${(coverage.failed || []).length ? `<h4>失败（${coverage.failed.length}）</h4><div class="ev-meta">${coverage.failed.map((s) => chip(`${s.platform}：${(s.error || "").slice(0, 40)}`, "warn")).join("")}</div>` : ""}
      ${(coverage.paused || []).length ? `<h4>自动停用（${coverage.paused.length}）</h4><div class="ev-meta">${coverage.paused.map((s) => chip(`${s.platform}：${(s.reason || "").slice(0, 40)}`, "warn")).join("")}</div>` : ""}
      ${(coverage.skipped || []).length ? `<h4>本轮跳过（${coverage.skipped.length}）</h4><div class="ev-meta">${coverage.skipped.map((s) => chip(`${s.name}：${(s.reason || "").slice(0, 40)}`)).join("")}</div>` : ""}
      ${(coverage.unavailable || []).length ? `<h4>未接入（无合规免费入口）</h4><div class="ev-meta">${coverage.unavailable.map((s) => chip(s.platform)).join("")}</div>` : ""}
      ${(coverage.disabled || []).length ? `<h4>已停用 / 受限</h4><div class="ev-meta">${coverage.disabled.map((s) => chip(`${s.platform}：${(s.notes || "").slice(0, 42)}`)).join("")}</div>` : ""}
      ${gaps.length ? `<h4>证据缺口</h4><ul class="claim-evidence">${gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>` : ""}
      <h4>说明</h4><p class="hint">${esc(payload.disclaimer || "")}</p>
      ${payload.scoring_weights ? `<p class="hint">评分权重：热度增速 ${(payload.scoring_weights.velocity * 100).toFixed(0)}% · 跨平台 ${(payload.scoring_weights.cross_platform * 100).toFixed(0)}% · 互动 ${(payload.scoring_weights.engagement * 100).toFixed(0)}% · 佐证 ${(payload.scoring_weights.verification * 100).toFixed(0)}% · 兴趣 ${(payload.scoring_weights.interest * 100).toFixed(0)}%</p>` : ""}
    </div>`;

    grid.innerHTML = html;
  }

  async function loadDaily() {
    try {
      const data = await api("/api/radar/report");
      renderDaily(data && data.payload ? data.payload : null);
      await loadStatus();
    } catch (error) {
      renderDaily(null);
      toast("日报读取失败：" + error.message, true);
    }
  }

  // ---------- investigation ----------
  function renderTimeline(origin) {
    const rows = (origin && origin.timeline) || [];
    if (!rows.length) {
      return `<p class="empty">${esc((origin && origin.note) || "尚未找到来源")}</p>`;
    }
    return `<ul class="timeline">${rows.map((row) => `
      <li>
        <div class="t-time">${esc(fmtTime(row.published_at))} · ${esc(row.platform)} · ${esc(row.source_type)}${row.first_hand ? " · 一手材料" : " · 转述"}</div>
        <div class="t-title"><a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a></div>
        <div class="t-meta">${esc(row.independent_group)}${row.quotes_previous ? " · 引用了更早来源" : ""}</div>
      </li>`).join("")}</ul>`;
  }

  function renderClaims(claims) {
    if (!claims || !claims.length) return `<p class="empty">没有拆解出可核验的事实主张。</p>`;
    return claims.map((claim) => {
      const kind = claim.status === "multi_support" || claim.status === "direct_support" ? "ok"
        : claim.status === "disputed" || claim.status === "contradicted" || claim.status === "corrected" ? "warn" : "";
      const evidence = (claim.evidence || []).map((row) => `
        <li>
          <a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.platform)} · ${esc(row.title)}</a>
          <span class="mono"> · ${esc(row.relation)} · 独立来源 ${esc(row.independent_group)}</span>
        </li>`).join("");
      return `<div class="claim">
        <div class="claim-head">
          <p class="claim-text">${esc(claim.text)}</p>
          ${chip(claim.status_label, kind)}
        </div>
        <div class="claim-rationale">${esc(claim.rationale)}${claim.claimant ? " · 来源：" + esc(claim.claimant) : ""}${claim.stated_at ? " · " + esc(fmtTime(claim.stated_at)) : ""}</div>
        ${evidence ? `<ul class="claim-evidence">${evidence}</ul>` : ""}
      </div>`;
    }).join("");
  }

  function renderTechSections(tech) {
    const s = tech.sections || {};
    const list = (value) => (value && value.length) ? value.map((v) => `<li>${esc(v)}</li>`).join("") : `<li class="empty">无</li>`;
    return `
      <div class="panel"><h3>① 发布事件摘要</h3><p class="ev-summary">${esc(s.event_summary)}</p></div>
      <div class="panel"><h3>② 发布方的核心主张</h3><ul class="claim-evidence">${list(s.core_claims)}</ul></div>
      <div class="panel"><h3>③ 原始材料与技术参数</h3>
        <table class="radar-table"><tbody>${(s.raw_material || []).map((m) => `<tr><td>${esc(fmtTime(m.published_at))}</td><td><a href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">${esc(m.title)}</a></td><td class="mono">${esc(m.platform)}</td></tr>`).join("") || `<tr><td class="empty">无</td></tr>`}</tbody></table>
        ${(s.metrics || []).length ? `<h4>可提取指标</h4><div class="ev-meta">${s.metrics.map((m) => chip(`${m.value}${m.unit}`)).join("")}</div>` : ""}
      </div>
      <div class="panel"><h3>④ 国内报道的主要内容</h3><p class="hint">${esc(s.domestic_coverage && s.domestic_coverage.count)} 条 · ${(s.domestic_coverage && s.domestic_coverage.outlets || []).map((o) => chip(o)).join("")}</p></div>
      <div class="panel"><h3>⑤ 国外媒体与专业研究者评价</h3><p class="hint">${esc(s.intl_coverage && s.intl_coverage.count)} 条 ${s.intl_coverage && s.intl_coverage.note ? "· " + esc(s.intl_coverage.note) : ""}</p><div class="ev-meta">${((s.intl_coverage && s.intl_coverage.outlets) || []).map((o) => chip(o)).join("") || `<span class="empty">无</span>`}</div></div>
      <div class="panel"><h3>⑥ 独立验证与可比指标</h3>
        <p class="ev-summary">${esc(s.comparability)}</p>
        <h4>可比性检查</h4><ul class="claim-evidence">${list(s.comparability_rules)}</ul>
      </div>
      <div class="panel"><h3>⑦ 支持证据</h3>${renderClaims(s.supporting_evidence || [])}</div>
      <div class="panel"><h3>⑧ 反对或限制性证据</h3>${renderClaims(s.limiting_evidence || [])}</div>
      <div class="panel"><h3>⑨ 技术成熟度与产业化证据</h3><p class="ev-summary">${esc(s.maturity)}</p></div>
      <div class="panel"><h3>⑩ 已确认事实 / 仍存争议 / 证据缺口</h3>
        <h4>已确认</h4><ul class="claim-evidence">${list(s.confirmed)}</ul>
        <h4>仍存争议</h4><ul class="claim-evidence">${list(s.disputed)}</ul>
        <h4>证据缺口</h4><ul class="claim-evidence">${list(s.gaps)}</ul>
      </div>
      <div class="panel"><h3>⑪ 原文引用与来源链接</h3>
        <ul class="claim-evidence">${(s.sources || []).map((x) => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a> <span class="mono">· ${esc(fmtTime(x.published_at))} · ${esc(x.platform)}</span></li>`).join("") || `<li class="empty">无</li>`}</ul>
      </div>`;
  }

  function renderInvestigation(payload) {
    const box = $("#investigateResult");
    if (!payload) { box.innerHTML = `<p class="empty">调查失败。</p>`; return; }
    const event = payload.event || {};
    const origin = payload.origin || {};
    const legend = payload.status_legend || {};
    let html = `
      <div class="section-title">${esc(event.title || "")} <small>${payload.kind === "tech" ? "科技突破核验" : "事件调查"}</small></div>
      <div class="panel">
        <div class="ev-meta">
          ${chip("证据 " + (payload.evidence_count || 0) + " 条")}
          ${chip("独立域名 " + (payload.independent_domains || 0))}
          ${event.heat != null ? chip("热度 " + event.heat) : ""}
          ${event.tech_flag ? chip("科技专题", "tech") : ""}
        </div>
        <h4>起源追溯</h4>
        <p class="ev-summary">${esc(origin.note || "")}</p>
        <p class="hint">${esc(origin.traceable_scope || "")}</p>
        ${renderTimeline(origin)}
      </div>

      <div class="panel">
        <h3>事实主张拆解</h3>
        ${renderClaims(payload.claims)}
        <h4>状态说明</h4>
        <div class="ev-meta">${Object.entries(legend).map(([key, label]) => chip(label)).join("")}</div>
      </div>

      <div class="panel">
        <h3>结论汇总</h3>
        <h4>已获支持</h4><ul class="claim-evidence">${(payload.confirmed || []).map((t) => `<li>${esc(t)}</li>`).join("") || `<li class="empty">无</li>`}</ul>
        <h4>存在争议 / 与证据矛盾</h4><ul class="claim-evidence">${(payload.disputed || []).map((t) => `<li>${esc(t)}</li>`).join("") || `<li class="empty">无</li>`}</ul>
        <h4>证据不足</h4><ul class="claim-evidence">${(payload.unverified || []).map((t) => `<li>${esc(t)}</li>`).join("") || `<li class="empty">无</li>`}</ul>
        ${(payload.corrections || []).length ? `<h4>更正 / 撤回材料</h4><ul class="claim-evidence">${payload.corrections.map((c) => `<li><a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.title)}</a></li>`).join("")}</ul>` : ""}
        <h4>证据缺口</h4><ul class="claim-evidence">${(payload.gaps || []).map((g) => `<li>${esc(g)}</li>`).join("") || `<li class="empty">无</li>`}</ul>
        <p class="hint">${esc(payload.method || "")}</p>
      </div>`;
    if (payload.tech) html += `<div class="section-title">国内外交叉核验</div>` + renderTechSections(payload.tech);
    box.innerHTML = html;
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function investigate(eventId, query) {
    if (state.busy) return;
    state.busy = true;
    $("#investigateResult").innerHTML = `<p class="empty">正在整理证据…</p>`;
    try {
      const body = eventId ? { event_id: eventId } : { query };
      const payload = await api("/api/radar/investigate", { method: "POST", body: JSON.stringify(body) });
      renderInvestigation(payload);
    } catch (error) {
      $("#investigateResult").innerHTML = `<p class="empty">调查失败：${esc(error.message)}</p>`;
      toast("调查失败：" + error.message, true);
    } finally {
      state.busy = false;
    }
  }

  // ---------- tech view ----------
  async function loadTech() {
    const box = $("#techResult");
    try {
      const data = await api("/api/radar/report");
      const payload = data && data.payload;
      const tech = (payload && payload.tech) || [];
      if (!tech.length) {
        box.innerHTML = `<p class="empty">今天没有触发科技专题。满足「科技关键词 + 热度阈值」的事件会自动出现在这里。</p>`;
        return;
      }
      box.innerHTML = `<div class="section-title">科技突破核验 <small>国内外交叉验证</small></div>` +
        tech.map((row) => `<div class="ev-card">
          <div class="ev-top"><h3 class="ev-title">${esc(row.title)}</h3><span class="ev-heat">🔥 ${esc(row.heat)}</span></div>
          <div class="ev-meta">
            ${row.independently_verified ? chip("已多方独立报道", "ok") : chip("未经独立验证", "warn")}
            ${chip("主张 " + row.claims_count + " 项")}
            ${(row.tech_reason || []).map((r) => chip(r, "info")).join("")}
          </div>
          ${row.note ? `<p class="ev-summary">${esc(row.note)}</p>` : ""}
          <div class="ev-actions"><button class="ghost-btn" data-action="investigate" data-event="${esc(row.event_id)}">生成核验报告</button></div>
        </div>`).join("");
    } catch (error) {
      box.innerHTML = `<p class="empty">读取失败：${esc(error.message)}</p>`;
    }
  }

  // ---------- sources view ----------
  async function loadSources() {
    const box = $("#sourcesResult");
    try {
      const data = await api("/api/radar/sources");
      const rows = data.sources || [];
      const summary = data.summary || {};
      const rowHtml = rows.map((row) => {
        const status = row.status === "ok" ? `<span class="status-ok">正常</span>`
          : row.status === "error" ? `<span class="status-error">失败 ${esc(row.consecutive_failures)}</span>`
          : row.status === "paused" ? `<span class="status-error">已自动停用</span>`
          : `<span class="status-off">未知</span>`;
        return `<tr>
          <td>${esc(row.name)}<br><span class="mono">${esc(row.source_id)}</span></td>
          <td>${esc(row.platform)}</td>
          <td>${esc(row.kind)}</td>
          <td>${row.enabled ? "启用" : `<span class="status-off">停用</span>`}</td>
          <td>${status}</td>
          <td class="mono">${esc(row.last_item_count)}</td>
          <td class="mono">${esc(row.last_latency_ms != null ? row.last_latency_ms + "ms" : "—")}</td>
          <td class="mono">${esc(fmtTime(row.last_ok_at))}</td>
          <td class="mono">${esc((row.last_error || "").slice(0, 60))}</td>
          <td>${esc(row.notes || "")}</td>
        </tr>`;
      }).join("");
      box.innerHTML = `
        <div class="section-title">数据源管理 <small>共 ${rows.length} 个 · 正常 ${summary.ok || 0} · 失败 ${summary.error || 0} · 自动停用 ${summary.paused || 0} · 未接入 ${summary.unavailable || 0}</small></div>
        <div class="panel"><table class="radar-table">
          <thead><tr><th>名称</th><th>平台</th><th>类型</th><th>状态</th><th>健康</th><th>条数</th><th>延迟</th><th>最近成功</th><th>最近错误</th><th>说明</th></tr></thead>
          <tbody>${rowHtml}</tbody>
        </table></div>
        <div class="ev-actions">
          <button class="ghost-btn" id="resetSources" type="button">重置失败计数（解除自动停用）</button>
        </div>
        <p class="hint">连续失败 3 次开始降频，6 次自动停用；重置后下一轮会重新尝试。未接入 = 该平台没有合规的免费公开入口，系统不会绕过登录、验证码或访问控制去抓取。</p>`;
      const resetBtn = $("#resetSources");
      if (resetBtn) {
        resetBtn.addEventListener("click", async () => {
          try {
            const result = await api("/api/radar/sources/reset", { method: "POST", body: "{}" });
            toast(`已重置 ${result.reset} 个数据源的失败计数`);
            loadSources();
          } catch (error) {
            toast("重置失败：" + error.message, true);
          }
        });
      }
    } catch (error) {
      box.innerHTML = `<p class="empty">读取失败：${esc(error.message)}</p>`;
    }
  }

  // ---------- history view ----------
  async function loadHistory() {
    const box = $("#historyResult");
    try {
      const data = await api("/api/radar/history");
      const reports = data.reports || [];
      const runs = data.runs || [];
      box.innerHTML = `
        <div class="section-title">历史日报</div>
        <div class="panel"><table class="radar-table">
          <thead><tr><th>日期</th><th>生成时间</th><th>版本</th><th></th></tr></thead>
          <tbody>${reports.map((r) => `<tr>
            <td class="mono">${esc(r.date)}</td><td class="mono">${esc(fmtTime(r.generated_at))}</td>
            <td class="mono">v${esc(r.version)}</td>
            <td><button class="ghost-btn" data-action="open-report" data-date="${esc(r.date)}">查看</button></td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">还没有历史日报</td></tr>`}</tbody>
        </table></div>
        <div class="section-title">运行记录</div>
        <div class="panel"><table class="radar-table">
          <thead><tr><th>开始</th><th>结束</th><th>状态</th><th>统计</th><th>错误</th></tr></thead>
          <tbody>${runs.map((r) => `<tr>
            <td class="mono">${esc(fmtTime(r.started_at))}</td>
            <td class="mono">${esc(fmtTime(r.finished_at))}</td>
            <td>${r.status === "ok" ? `<span class="status-ok">成功</span>` : r.status === "failed" ? `<span class="status-error">失败</span>` : esc(r.status)}</td>
            <td class="mono">${esc(JSON.stringify(r.stats || {}).slice(0, 120))}</td>
            <td class="mono">${esc((r.error || "").slice(0, 80))}</td>
          </tr>`).join("") || `<tr><td colspan="5" class="empty">还没有运行记录</td></tr>`}</tbody>
        </table></div>`;
    } catch (error) {
      box.innerHTML = `<p class="empty">读取失败：${esc(error.message)}</p>`;
    }
  }

  async function openReport(date) {
    try {
      const data = await api(`/api/radar/report?date=${encodeURIComponent(date)}`);
      renderDaily(data.payload);
      switchView("daily");
      toast(`已载入 ${date} 的日报`);
    } catch (error) {
      toast("读取失败：" + error.message, true);
    }
  }

  // ---------- settings view ----------
  async function loadSettings() {
    const box = $("#settingsResult");
    try {
      const data = await api("/api/radar/settings");
      const s = data.settings || {};
      const interests = s.interests || {};
      const push = s.push || {};
      const weights = s.scoring || {};
      const toggles = s.source_toggles || {};
      const llm = s.llm || {};
      box.innerHTML = `
        <div class="section-title">监控设置 <small>保存后写入 ${esc(data.path)}</small></div>
        <div class="panel">
          <h3>执行与报告</h3>
          <div class="field-row">
            <div class="field"><label>每日生成时间（HH:MM）</label><input id="setDailyTime" type="text" value="${esc((s.schedule && s.schedule.daily_time) || "09:00")}"></div>
            <div class="field"><label>日报事件数上限</label><input id="setTopEvents" type="number" min="5" max="30" value="${esc((s.report && s.report.top_events) || 12)}"></div>
            <div class="field"><label>自动触发调查的热度阈值</label><input id="setMinHeat" type="number" min="0" max="100" value="${esc((s.investigation && s.investigation.min_heat) || 70)}"></div>
          </div>
        </div>
        <div class="panel">
          <h3>兴趣主题</h3>
          <div class="field"><label>关注主题（每行一个）</label><textarea id="setTopics">${esc((interests.topics || []).join("\n"))}</textarea></div>
          <div class="field"><label>关键词（每行一个，留空则只用主题）</label><textarea id="setKeywords">${esc((interests.keywords || []).join("\n"))}</textarea></div>
          <div class="field"><label>排除词（每行一个）</label><textarea id="setExclude">${esc((interests.exclude || []).join("\n"))}</textarea></div>
        </div>
        <div class="panel">
          <h3>评分权重</h3>
          <div class="field-row">
            <div class="field"><label>热度增速</label><input id="wVelocity" type="number" step="0.05" min="0" max="1" value="${esc(weights.velocity)}"></div>
            <div class="field"><label>跨平台传播</label><input id="wCross" type="number" step="0.05" min="0" max="1" value="${esc(weights.cross_platform)}"></div>
            <div class="field"><label>互动强度</label><input id="wEngage" type="number" step="0.05" min="0" max="1" value="${esc(weights.engagement)}"></div>
            <div class="field"><label>新闻与官方佐证</label><input id="wVerify" type="number" step="0.05" min="0" max="1" value="${esc(weights.verification)}"></div>
            <div class="field"><label>兴趣匹配</label><input id="wInterest" type="number" step="0.05" min="0" max="1" value="${esc(weights.interest)}"></div>
          </div>
        </div>
        <div class="panel">
          <h3>推送</h3>
          <div class="field-row">
            <div class="field"><label>启用每日自动推送</label>
              <select id="setPushEnabled">
                <option value="1" ${push.enabled ? "selected" : ""}>启用</option>
                <option value="0" ${push.enabled ? "" : "selected"}>关闭</option>
              </select>
            </div>
            <div class="field"><label>推送项目数</label><input id="setPushItems" type="number" min="1" max="10" value="${esc(push.items || 5)}"></div>
            <div class="field"><label>重大事件提醒阈值</label><input id="setAlertThreshold" type="number" min="0" max="100" value="${esc(push.alert_threshold || 82)}"></div>
          </div>
          <p class="hint">推送复用 Server酱 Turbo 通道：优先使用 RADAR_SERVERCHAN_SENDKEY，未配置时回退到 SERVERCHAN_SENDKEY，不会改动 GitHub 简报的推送。</p>
        </div>
        <div class="panel">
          <h3>大模型</h3>
          <p class="hint">大模型已经改为全局设置，对所有模块生效（GitHub 项目 AI 解读、热点摘要）。<a href="/settings.html">前往全局设置</a> 配置接口、模型与密钥，并可测试连接、查看错误日志。</p>
          <div class="ev-meta">
            ${llm.available ? chip("模型可用 · " + (llm.model || ""), "ok") : chip("模型未启用或不可用：" + (llm.unavailable_reason || ""), "warn")}
          </div>
        </div>
        <div class="panel">
          <h3>数据源开关</h3>
          <div class="toggle-grid">
            ${Object.entries(toggles).map(([key, on]) => `<label class="source-toggle"><input type="checkbox" data-source="${esc(key)}" ${on ? "checked" : ""}> ${esc(key)}</label>`).join("")}
          </div>
        </div>
        <div class="ev-actions"><button class="primary-btn" id="saveSettings">保存设置</button></div>`;
      $("#saveSettings").addEventListener("click", saveSettings);
    } catch (error) {
      box.innerHTML = `<p class="empty">读取失败：${esc(error.message)}</p>`;
    }
  }

  async function saveSettings() {
    const lines = (id) => ($(id).value || "").split("\n").map((v) => v.trim()).filter(Boolean);
    const num = (id) => Number($(id).value);
    const body = {
      daily_time: $("#setDailyTime").value.trim(),
      top_events: num("#setTopEvents"),
      min_heat: num("#setMinHeat"),
      topics: lines("#setTopics"),
      keywords: lines("#setKeywords"),
      exclude: lines("#setExclude"),
      push_enabled: $("#setPushEnabled").value === "1",
      push_items: num("#setPushItems"),
      alert_threshold: num("#setAlertThreshold"),
      weights: {
        velocity: num("#wVelocity"), cross_platform: num("#wCross"),
        engagement: num("#wEngage"), verification: num("#wVerify"),
        interest: num("#wInterest"),
      },
      sources: {},
    };
    $$("input[data-source]").forEach((input) => {
      body.sources[input.dataset.source] = input.checked;
    });
    try {
      const data = await api("/api/radar/settings", { method: "POST", body: JSON.stringify(body) });
      const savedKeys = Object.keys(data.saved || {});
      toast(savedKeys.length ? "设置已保存：" + savedKeys.join("、") : "没有需要保存的改动");
      loadSettings();
      loadStatus();
    } catch (error) {
      toast("保存失败：" + error.message, true);
    }
  }

  // ---------- actions ----------
  async function triggerCollect() {
    try {
      const data = await api("/api/radar/collect", { method: "POST", body: "{}" });
      toast(data && data.started ? "已开始采集…" : "采集正在进行中");
      startPolling();
    } catch (error) {
      toast("采集失败：" + error.message, true);
    }
  }

  async function triggerPush() {
    try {
      const data = await api("/api/radar/push", { method: "POST", body: "{}" });
      const result = (data && data.result) || {};
      if (result.ok) toast(result.skipped ? "今天已经推送过了" : "已推送到微信");
      else toast(result.reason || "推送失败", true);
    } catch (error) {
      toast("推送失败：" + error.message, true);
    }
  }

  function startPolling() {
    if (state.poll) return;
    state.poll = setInterval(async () => {
      const status = await loadStatus();
      if (!status || !status.state || !status.state.running) {
        clearInterval(state.poll);
        state.poll = null;
        if (status && status.today_ready) {
          toast("采集完成，日报已更新");
          if (state.view === "daily") loadDaily();
        }
      }
    }, 5000);
  }

  // ---------- wiring ----------
  function bind() {
    $("#radarTabs").addEventListener("click", (event) => {
      const tab = event.target.closest(".radar-tab");
      if (tab) switchView(tab.dataset.view);
    });

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "investigate") {
        markSeen(button.closest(".ev-card"));
        switchView("investigate");
        investigate(button.dataset.event, null);
      } else if (action === "open-report") {
        openReport(button.dataset.date);
      }
    });


    $("#collectBtn").addEventListener("click", triggerCollect);
    $("#pushBtn").addEventListener("click", triggerPush);

    $("#investigateForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("#investigateInput").value.trim();
      if (!value) return;
      investigate(null, value);
    });
  }

  bind();
  switchView(state.view);
  loadStatus().then((status) => {
    if (status && status.state && status.state.running) startPolling();
  });
})();
