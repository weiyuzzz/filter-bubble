/* 系统状态页 — 只读聚合，数据全部来自既有接口 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const body = $("#statusBody");

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const fmt = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "—");

  async function get(path) {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!response.ok) throw new Error((data && data.error) || `HTTP ${response.status}`);
    return data;
  }

  function stateChip(ok, text) {
    return `<span class="chip ${ok ? "ok" : "warn"}">${esc(text)}</span>`;
  }

  function statsLine(stats) {
    const collect = (stats && stats.collect) || {};
    const bits = [];
    if (stats && stats.events != null) bits.push(`事件 ${stats.events}`);
    if (stats && stats.items_considered != null) bits.push(`候选 ${stats.items_considered}`);
    if (collect.sources_enabled) bits.push(`源 ${collect.sources_ok || 0}/${collect.sources_enabled}`);
    if (collect.items_new != null) bits.push(`新增 ${collect.items_new} 条`);
    return bits.length ? bits.join(" · ") : "—";
  }

  function render({ health, github, radar }) {
    const ghState = (github && github.state) || {};
    const radarState = (radar && radar.state) || {};
    const radarSources = (radar && radar.sources) || {};
    const runs = (radar && radar.runs) || [];
    const push = (github && github.push) || {};

    $("#healthDot").className = "live-dot" + (health ? "" : " live");
    $("#healthText").textContent = health ? "服务在线" : "服务不可达";

    const ghCard = `
      <section class="panel">
        <div class="section-title" style="margin-top:0">GitHub 技术雷达</div>
        <div class="ev-meta">
          ${stateChip(!!(github && github.today_ready), github && github.today_ready ? "今日已生成" : "今日未生成")}
          ${ghState.running ? `<span class="chip info">正在刷新</span>` : ""}
          ${github && github.latest_date ? `<span class="chip">最新 ${esc(github.latest_date)}</span>` : ""}
        </div>
        <table class="radar-table">
          <tbody>
            <tr><th>每日刷新</th><td>${esc((github && github.schedule && github.schedule.time) || "08:00")} · ${esc((github && github.schedule && github.schedule.timezone) || "Asia/Shanghai")}</td></tr>
            <tr><th>下次运行</th><td class="mono">${esc(fmt(github && github.schedule && github.schedule.next_run))}</td></tr>
            <tr><th>最近生成</th><td class="mono">${esc(fmt(github && github.latest_generated_at))}</td></tr>
            <tr><th>微信简报</th><td>${esc(push.status || "未发送")} · ${esc(push.configured ? "已配置" : "未配置")}</td></tr>
          </tbody>
        </table>
      </section>`;

    const radarCard = `
      <section class="panel">
        <div class="section-title" style="margin-top:0">全球热点雷达</div>
        <div class="ev-meta">
          ${stateChip(!!(radar && radar.today_ready), radar && radar.today_ready ? "今日已生成" : "今日未生成")}
          ${radarState.running ? `<span class="chip info">${esc(radarState.stage || "运行中")}</span>` : ""}
          ${radarState.last_error ? `<span class="chip warn">上次失败</span>` : ""}
          <span class="chip">在线源 ${esc(radarSources.ok)}</span>
          <span class="chip ${radarSources.error ? "warn" : ""}">失败源 ${esc(radarSources.error)}</span>
          <span class="chip">未接入 ${esc(radarSources.unavailable)}</span>
        </div>
        <table class="radar-table">
          <tbody>
            <tr><th>每日生成</th><td>${esc((radar && radar.schedule && radar.schedule.time) || "09:00")} · ${esc((radar && radar.schedule && radar.schedule.timezone) || "Asia/Shanghai")}</td></tr>
            <tr><th>下次运行</th><td class="mono">${esc(fmt(radar && radar.schedule && radar.schedule.next_run))}</td></tr>
            <tr><th>最近生成</th><td class="mono">${esc(fmt(radar && radar.latest_generated_at))}</td></tr>
            <tr><th>自动推送</th><td>${esc(radar && radar.push_enabled ? "已启用" : "未启用")}</td></tr>
            ${radarState.last_error ? `<tr><th>最近错误</th><td class="mono">${esc(String(radarState.last_error).slice(0, 160))}</td></tr>` : ""}
          </tbody>
        </table>
      </section>`;

    const runRows = runs.length
      ? runs.map((run) => `<tr>
          <td class="mono">${esc(fmt(run.started_at))}</td>
          <td class="mono">${esc(fmt(run.finished_at))}</td>
          <td>${run.status === "ok" ? `<span class="status-ok">成功</span>`
            : run.status === "failed" ? `<span class="status-error">失败</span>`
            : esc(run.status)}</td>
          <td class="mono">${esc(statsLine(run.stats))}</td>
          <td class="mono">${esc(String(run.error || "").slice(0, 90))}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">还没有运行记录</td></tr>`;

    body.innerHTML = `
      <div class="layout" style="grid-template-columns: repeat(auto-fit, minmax(340px, 1fr))">
        ${ghCard}
        ${radarCard}
      </div>
      <section class="panel">
        <div class="section-title" style="margin-top:0">最近运行记录 <small>雷达采集任务</small></div>
        <div style="overflow-x:auto">
          <table class="radar-table">
            <thead><tr><th>开始</th><th>结束</th><th>状态</th><th>统计</th><th>错误</th></tr></thead>
            <tbody>${runRows}</tbody>
          </table>
        </div>
      </section>
      <p class="hint">本页只读：不会触发采集、推送，也不会修改任何配置。</p>`;
  }

  function renderError(error) {
    $("#healthDot").className = "live-dot live";
    $("#healthText").textContent = "读取失败";
    body.innerHTML = `
      <div class="error-state">
        <div class="empty-mark">!</div>
        <h3>状态读取失败</h3>
        <p>${esc(error.message)}。请确认本地服务仍在运行，然后重试。</p>
        <button class="ghost-btn" id="retryBtn" type="button">重试</button>
      </div>`;
    $("#retryBtn").addEventListener("click", load);
  }

  async function load() {
    body.innerHTML = `<div class="panel"><p class="pending">正在读取状态…</p></div>`;
    try {
      const [healthRes, githubRes, radarRes] = await Promise.allSettled([
        get("/api/health"), get("/api/status"), get("/api/radar/status"),
      ]);
      const health = healthRes.status === "fulfilled" && healthRes.value && healthRes.value.ok;
      const github = githubRes.status === "fulfilled" ? githubRes.value : null;
      const radar = radarRes.status === "fulfilled" ? radarRes.value : null;
      if (!github && !radar) throw new Error("两个模块的状态接口都不可用");
      render({ health, github, radar });
    } catch (error) {
      renderError(error);
    }
  }

  $("#reloadBtn").addEventListener("click", load);
  load();
})();
