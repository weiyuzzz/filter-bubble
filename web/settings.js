/* 全局设置页：大模型配置、连接测试、错误日志、AI 解读参数 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const body = $("#settingsBody");
  const toasts = $("#toasts");

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function toast(message, bad = false) {
    const node = document.createElement("div");
    node.className = "toast" + (bad ? " bad" : "");
    node.textContent = message;
    toasts.appendChild(node);
    setTimeout(() => node.remove(), 4500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!response.ok) throw new Error((data && (data.error || data.message)) || `HTTP ${response.status}`);
    return data;
  }

  const chip = (text, kind = "") => `<span class="chip ${kind}">${esc(text)}</span>`;

  function usageChart(usage, limits) {
    const slots = usage.slots || {};
    const primary = slots.primary || {};
    const fallback = slots.fallback || {};
    const fmt = (n) => Number(n || 0).toLocaleString("en-US");

    const meter = (label, data, kind) => {
      const limit = Number(data.limit || 0);
      const used = Number(data.tokens || 0);
      const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
      const pct = limit > 0 ? Math.round(ratio * 100) : null;
      return `<div class="usage-meter">
        <div class="usage-meter-head">
          <span>${esc(label)}</span>
          <b>${fmt(used)} tokens${limit > 0 ? ` / ${fmt(limit)}（${pct}%）` : "（不限）"}</b>
        </div>
        <div class="usage-track"><span class="usage-fill ${kind}" style="width:${pct === null ? 0 : Math.max(2, pct)}%"></span></div>
        <div class="usage-sub">调用 ${fmt(data.calls || 0)} 次 · 输入 ${fmt(data.prompt)} · 输出 ${fmt(data.completion)}</div>
      </div>`;
    };

    // 折线图：横轴时间，纵轴 token，两条序列（主 / 备）
    const series = (usage.history || []).slice(-14).map((d) => ({
      date: (d.date || "").slice(5),
      primary: Number(d.primary || 0),
      fallback: Number(d.fallback || 0),
    }));
    series.push({ date: (usage.date || "").slice(5), primary: Number(primary.tokens || 0),
                  fallback: Number(fallback.tokens || 0) });

    const W = 720, H = 220, padL = 52, padR = 16, padT = 18, padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const maxValue = Math.max(1, ...series.map((d) => Math.max(d.primary, d.fallback)));
    // 纵轴取整到「好读」的刻度
    const step = Math.pow(10, Math.floor(Math.log10(maxValue)));
    const top = Math.ceil(maxValue / step) * step;
    const x = (i) => series.length === 1
      ? padL + plotW / 2
      : padL + (plotW * i) / (series.length - 1);
    const y = (v) => padT + plotH - (plotH * (v / top));

    const line = (key) => series.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(" ");
    const dots = (key, color) => series.map((d, i) =>
      `<circle cx="${x(i).toFixed(1)}" cy="${y(d[key]).toFixed(1)}" r="3.2" fill="${color}">
         <title>${esc(d.date)}：${fmt(d[key])} tokens</title>
       </circle>`).join("");
    const gridLines = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const value = top * f;
      const yy = y(value);
      return `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}"
                stroke="var(--fb-border)" stroke-dasharray="${f === 0 ? "0" : "3 4"}"/>
              <text x="${padL - 8}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end" font-size="9"
                fill="var(--fb-text-3)">${fmt(Math.round(value))}</text>`;
    }).join("");
    const xLabels = series.map((d, i) => {
      const show = series.length <= 8 || i % Math.ceil(series.length / 8) === 0 || i === series.length - 1;
      return show
        ? `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="9" fill="var(--fb-text-3)">${esc(d.date)}</text>`
        : "";
    }).join("");

    const hasData = series.some((d) => d.primary > 0 || d.fallback > 0);
    const chart = hasData || series.length > 1
      ? `<div class="usage-chart">
          <div class="usage-chart-head">
            <span>最近 ${series.length} 天 token 消耗（纵轴：tokens）</span>
            <span class="usage-legend">
              <i class="dot primary"></i>主接口
              <i class="dot fallback"></i>备用接口
            </span>
          </div>
          <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="主备接口 token 消耗折线图" preserveAspectRatio="xMidYMid meet">
            ${gridLines}
            <path d="${line("primary")}" fill="none" stroke="var(--fb-accent)" stroke-width="2.2"
                  stroke-linejoin="round" stroke-linecap="round"/>
            <path d="${line("fallback")}" fill="none" stroke="var(--fb-warn)" stroke-width="2.2"
                  stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5 4"/>
            ${dots("primary", "var(--fb-accent)")}
            ${dots("fallback", "var(--fb-warn)")}
            ${xLabels}
          </svg>
        </div>`
      : `<div class="usage-chart is-empty">
          <p class="hint">还没有 token 消耗记录。生成 AI 解读、雷达摘要或论文概况后，这里会按天显示主/备接口的用量曲线。</p>
        </div>`;

    const paused = usage.paused;
    return `
      <div class="usage-head">
        ${paused
          ? `<span class="chip warn">AI 已暂停</span><span class="usage-note">${esc(usage.paused_until || "")} 自动恢复并重新计数</span>
             <button class="ghost-btn" id="resumeAiBtn" type="button">立即恢复</button>`
          : `<span class="chip ok">正常</span><span class="usage-note">统计窗口 ${esc(limits.window_hours || 24)} 小时 · 今天 ${esc(usage.date || "")}</span>`}
      </div>
      <div class="usage-grid">
        ${meter("主接口", primary, "primary")}
        ${meter("备用接口", fallback, "fallback")}
      </div>
      ${chart}`;
  }

  function errorBlock(result) {
    const error = result.error || {};
    const rows = [];
    if (result.reason) rows.push(`原因：${result.reason}`);
    if (error.status) rows.push(`HTTP 状态：${error.status}`);
    if (error.message) rows.push(`上游消息：${error.message}`);
    if (error.detail) rows.push(`上游原始返回：${error.detail}`);
    if (result.hint || error.hint) rows.push(`建议：${result.hint || error.hint}`);
    if (error.suggested_model) rows.push(`可改用模型：${error.suggested_model}`);
    if (result.log_path) rows.push(`错误日志：${result.log_path}`);
    return `<div class="error-state" style="align-items:stretch;text-align:left;padding:14px 0">
      <h3 style="color:var(--fb-danger)">连接失败</h3>
      <pre style="white-space:pre-wrap;word-break:break-word;background:var(--fb-surface-2);border:1px solid var(--fb-border);border-radius:var(--fb-r-md);padding:12px;font-size:12px;line-height:1.7;margin-top:8px">${esc(rows.join("\n"))}</pre>
    </div>`;
  }

  function render(data) {
    const llm = data.llm || {};
    const ai = data.ai || {};
    const key = llm.key || {};
    const stats = llm.stats || {};
    const fb = llm.fallback || {};
    const fbKey = fb.key || {};
    const slot = { active: llm.active_slot || "primary" };
    const switchState = llm.switch || {};
    const limits = llm.token_limits || { primary: 0, fallback: 0, window_hours: 24 };
    const usage = llm.usage || { slots: {}, history: [], paused: false };
    const push = data.push || { enabled: true, ready: false, reason: "", time: "08:00", items: 3 };

    $("#aiDot").className = "live-dot" + (llm.available ? "" : " live");
    $("#aiState").textContent = slot.active === "fallback"
      ? "使用备用模型"
      : (llm.available ? "模型可用" : "模型不可用");

    body.innerHTML = `
      <section class="panel config-card">
        <header class="config-head">
          <div>
            <h3>主接口</h3>
            <p class="config-sub">三个模块默认调用这个接口。地址要和服务商对应，换服务商时地址、模型、key 一起换。</p>
          </div>
          <div class="config-state">
            ${llm.available ? chip("可用", "ok") : chip("不可用", "warn")}
            ${stats.calls ? chip(`已调用 ${stats.calls} 次`) : ""}
          </div>
        </header>

        <div class="config-grid">
          <div class="field">
            <label for="setEnabled">接入大模型</label>
            <select id="setEnabled">
              <option value="1" ${llm.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${llm.enabled ? "" : "selected"}>关闭（用本地规则）</option>
            </select>
          </div>
          <div class="field">
            <label for="setProvider">接口类型</label>
            <select id="setProvider">
              ${(llm.providers || ["openai"]).map((p) => `<option value="${esc(p)}" ${llm.provider === p ? "selected" : ""}>${esc((llm.provider_labels || {})[p] || p)}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="setModel">模型名</label>
            <input id="setModel" type="text" placeholder="例如 deepseek-chat" value="${esc(llm.model || "")}">
          </div>
          <div class="field config-wide">
            <label for="setBaseUrl">接口地址</label>
            <input id="setBaseUrl" type="text" placeholder="例如 https://api.deepseek.com" value="${esc(llm.base_url || "")}">
            <span class="field-hint">DeepSeek 用 <code>https://api.deepseek.com</code>，OpenRouter 用 <code>https://openrouter.ai/api/v1</code>，Claude 用 <code>https://api.anthropic.com</code>，本地填 <code>http://127.0.0.1:11434</code>。</span>
          </div>
          <div class="field config-wide">
            <label for="setApiKey">API Key ${key.configured ? `（已配置 ${esc(key.masked)} · ${esc(key.source)}）` : "（未配置）"}</label>
            <input id="setApiKey" type="password" autocomplete="off" spellcheck="false" placeholder="${key.configured ? "已保存，留空表示不修改" : "粘贴这家服务商自己的 key"}">
            <span class="field-hint">每个服务商的 key 不通用：把 DeepSeek 的 key 填到 OpenRouter 地址上会返回 401。</span>
          </div>
          <div class="field">
            <label for="setTokenLimitPrimary">每日 token 上限</label>
            <input id="setTokenLimitPrimary" type="number" min="0" step="1000" value="${esc(limits.primary || 0)}">
            <span class="field-hint">0 表示不限。达到上限自动切备用接口。</span>
          </div>
          <div class="field">
            <label for="setTokenWindowHours">统计窗口（小时）</label>
            <input id="setTokenWindowHours" type="number" min="1" max="168" value="${esc(limits.window_hours || 24)}">
            <span class="field-hint">默认 24 小时，跨窗口重新计数。</span>
          </div>
        </div>

        <p class="hint">密钥只写入本机 <code>${esc(data.secrets_path || "data/ai_secrets.json")}</code>，不进配置文件，也不会回传到页面。</p>
        <div class="config-actions">
          <button class="primary-btn" id="saveBtn" type="button">保存设置</button>
          <button class="ghost-btn" id="testBtn" type="button">测试连接</button>
          ${key.configured ? `<button class="ghost-btn" id="clearBtn" type="button">清除密钥</button>` : ""}
        </div>
        <div id="testResult" style="margin-top:12px"></div>
      </section>

      <section class="panel config-card">
        <header class="config-head">
          <div>
            <h3>备用接口</h3>
            <p class="config-sub">主接口额度用尽或报 429/402 时自动切过来；第二天自动切回主接口。</p>
          </div>
          <div class="config-state">
            ${slot.active === "fallback"
              ? `<span class="chip warn">正在使用备用</span>`
              : `<span class="chip ok">主接口在用</span>`}
            ${fb.available ? chip("备用可用", "ok") : chip("备用未就绪", "warn")}
          </div>
        </header>

        <div class="config-grid">
          <div class="field">
            <label for="setFallbackEnabled">启用备用接口</label>
            <select id="setFallbackEnabled">
              <option value="1" ${fb.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${fb.enabled ? "" : "selected"}>关闭</option>
            </select>
          </div>
          <div class="field">
            <label for="setFallbackProvider">接口类型</label>
            <select id="setFallbackProvider">
              ${(llm.providers || ["openai"]).map((p) => `<option value="${esc(p)}" ${fb.provider === p ? "selected" : ""}>${esc((llm.provider_labels || {})[p] || p)}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="setFallbackModel">模型名</label>
            <input id="setFallbackModel" type="text" placeholder="例如 deepseek-chat" value="${esc(fb.model || "")}">
          </div>
          <div class="field config-wide">
            <label for="setFallbackBaseUrl">接口地址</label>
            <input id="setFallbackBaseUrl" type="text" placeholder="例如 https://api.deepseek.com" value="${esc(fb.base_url || "")}">
            <span class="field-hint">换成另一家服务商时，地址和 key 必须一起换。</span>
          </div>
          <div class="field config-wide">
            <label for="setFallbackKey">备用 API Key ${fbKey.configured ? `（已配置 ${esc(fbKey.masked)}）` : "（未配置）"}</label>
            <input id="setFallbackKey" type="password" autocomplete="off" spellcheck="false" placeholder="${fbKey.configured ? "已保存，留空表示不修改" : "粘贴备用服务商自己的 key"}">
          </div>
          <div class="field">
            <label for="setTokenLimitFallback">备用接口每日 token 上限</label>
            <input id="setTokenLimitFallback" type="number" min="0" step="1000" value="${esc(limits.fallback || 0)}">
            <span class="field-hint">备用也超限 → 暂停 AI，等待窗口结束后重算。</span>
          </div>
          <div class="field">
            <label>当前状态</label>
            <div class="config-static">
              ${slot.active === "fallback"
                ? `到 ${esc(switchState.fallback_until || "明天")} 自动切回`
                : (fb.unavailable_reason ? esc(fb.unavailable_reason) : "已就绪")}
            </div>
          </div>
        </div>

        <div class="config-actions">
          <button class="primary-btn" id="saveFallbackBtn" type="button">保存设置</button>
          <button class="ghost-btn" id="testFallbackBtn" type="button">测试备用接口</button>
          ${slot.active === "fallback" ? `<button class="ghost-btn" id="resetSlotBtn" type="button">立即切回主接口</button>` : ""}
          ${fbKey.configured ? `<button class="ghost-btn" id="clearFallbackKeyBtn" type="button">清除备用密钥</button>` : ""}
        </div>
        <div id="fallbackTestResult" style="margin-top:12px"></div>
      </section>

      <section class="panel config-card">
        <header class="config-head">
          <div>
            <h3>Token 用量</h3>
            <p class="config-sub">本地记账，用来在超限前切换到备用接口，而不是等报错。</p>
          </div>
        </header>
        ${usageChart(usage, limits)}
      </section>

      <section class="panel">
        <h3>AI 解读</h3>
        <div class="field-row">
          <div class="field"><label for="setBriefsEnabled">GitHub 项目 AI 解读</label>
            <select id="setBriefsEnabled">
              <option value="1" ${ai.enable_briefs ? "selected" : ""}>启用</option>
              <option value="0" ${ai.enable_briefs ? "" : "selected"}>关闭</option>
            </select>
          </div>
          <div class="field"><label for="setConcurrency">并发数（免费模型建议 1）</label>
            <input id="setConcurrency" type="number" min="1" max="4" value="${esc(ai.concurrency)}">
          </div>
        </div>
        <p class="hint">日报里的每个项目都会生成 AI 解读（不再限制条数），成本由上面的 token 限额控制；结果按「仓库 + 模型」缓存，不会重复消耗。</p>
        <div class="config-actions">
          <button class="primary-btn" id="saveAiBtn" type="button">保存设置</button>
        </div>
      </section>

      <section class="panel">
        <header class="config-head">
          <div>
            <h3>微信推送</h3>
            <p class="config-sub">每天 ${esc(push.time || "08:00")} 生成日报后，把前 ${esc(push.items || 3)} 个项目推送到微信（Server酱 Turbo）。</p>
          </div>
          <div class="config-state">
            ${push.enabled && push.ready ? chip("已启用", "ok") : chip("已关闭", "warn")}
          </div>
        </header>
        <div class="field-row">
          <div class="field"><label for="setPushEnabled">推送开关</label>
            <select id="setPushEnabled">
              <option value="1" ${push.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${push.enabled ? "" : "selected"}>关闭</option>
            </select>
          </div>
          <div class="field"><label>实际状态</label>
            <input type="text" value="${esc(push.enabled ? (push.ready ? "可推送" : "开关已开，但发不出去") : "已关闭")}" readonly>
          </div>
        </div>
        <p class="hint">${push.ready
          ? `SendKey 已配置（${esc(push.provider || "")}）。关掉开关后连「立即推送」也不会发，调度器也不会再走每日推送。`
          : `当前发不出去：${esc(push.reason || "原因未知")}`}</p>
        <div id="pushResult"></div>
        <div class="config-actions">
          <button class="primary-btn" id="savePushBtn" type="button">保存设置</button>
          <button class="ghost-btn" id="pushNowBtn" type="button">立即推送一次</button>
        </div>
      </section>

      <section class="panel">
        <h3>最近错误日志 <small>${esc(data.error_log || "")}</small></h3>
        <div id="errorLog"><p class="pending">读取中…</p></div>
      </section>
    `;

    $("#saveBtn").addEventListener("click", save);
    $("#testBtn").addEventListener("click", test);
    const clearBtn = $("#clearBtn");
    if (clearBtn) clearBtn.addEventListener("click", clearKey);
    const testFb = $("#testFallbackBtn");
    if (testFb) testFb.addEventListener("click", testFallback);
    const clearFb = $("#clearFallbackKeyBtn");
    if (clearFb) clearFb.addEventListener("click", clearFallbackKey);
    const resetSlot = $("#resetSlotBtn");
    if (resetSlot) resetSlot.addEventListener("click", resetSlotNow);
    const resumeAi = $("#resumeAiBtn");
    if (resumeAi) resumeAi.addEventListener("click", resumeAiNow);
    const saveFallback = $("#saveFallbackBtn");
    if (saveFallback) saveFallback.addEventListener("click", save);
    const saveAi = $("#saveAiBtn");
    if (saveAi) saveAi.addEventListener("click", save);
    const savePush = $("#savePushBtn");
    if (savePush) savePush.addEventListener("click", save);
    const pushNow = $("#pushNowBtn");
    if (pushNow) pushNow.addEventListener("click", pushNowSend);

    loadErrors();
  }

  async function resumeAiNow() {
    try {
      const data = await api("/api/ai/resume", { method: "POST", body: "{}" });
      toast("已恢复 AI，tokens 重新计数");
      render(data.settings);
    } catch (error) {
      toast("恢复失败：" + error.message, true);
    }
  }

  async function testFallback() {
    const box = $("#fallbackTestResult");
    if (box) box.innerHTML = `<p class="pending">正在请求备用接口…</p>`;
    try {
      const data = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ slot: "fallback" }) });
      const result = data.result || {};
      if (box) {
        box.innerHTML = result.ok
          ? `<div class="claim"><div class="claim-head"><p class="claim-text">备用接口返回正常</p>${chip((result.latency_ms || 0) + "ms", "ok")}</div>
             <div class="claim-rationale">模型：${esc(result.model || "")} · 接口：${esc(result.base_url || "")}</div></div>`
          : `<div class="error-state" style="padding:12px 0"><p>备用接口不可用：${esc(result.reason || "未知原因")}</p></div>`;
      }
      toast(result.ok ? "备用接口可用" : "备用接口不可用", !result.ok);
      render(data.settings);
    } catch (error) {
      if (box) box.innerHTML = `<p class="empty">测试失败：${esc(error.message)}</p>`;
    }
  }

  async function clearFallbackKey() {
    try {
      const data = await api("/api/ai/settings", {
        method: "POST", body: JSON.stringify({ clear_fallback_api_key: true }),
      });
      toast("已清除备用密钥");
      render(data.settings);
    } catch (error) {
      toast("清除失败：" + error.message, true);
    }
  }

  async function resetSlotNow() {
    try {
      const data = await api("/api/ai/settings", {
        method: "POST", body: JSON.stringify({ reset_slot: true }),
      });
      toast("已切回主接口");
      render(data.settings);
    } catch (error) {
      toast("切换失败：" + error.message, true);
    }
  }

  async function loadErrors() {
    const box = $("#errorLog");
    if (!box) return;
    try {
      const data = await api("/api/ai/errors");
      const rows = data.errors || [];
      box.innerHTML = rows.length
        ? `<pre style="white-space:pre-wrap;word-break:break-all;background:var(--fb-surface-2);border:1px solid var(--fb-border);border-radius:var(--fb-r-md);padding:12px;font-size:11.5px;line-height:1.8;max-height:260px;overflow:auto">${esc(rows.join("\n"))}</pre>`
        : `<p class="hint">还没有错误记录。</p>`;
    } catch (error) {
      box.innerHTML = `<p class="hint">读取失败：${esc(error.message)}</p>`;
    }
  }

  async function pushNowSend() {
    const box = $("#pushResult");
    if (!box) return;
    box.innerHTML = `<p class="pending">正在发送…</p>`;
    try {
      // force 只跳过「今天已发过」的判重；开关关掉时依然不发（由后端把关）
      const data = await api("/api/push", { method: "POST", body: JSON.stringify({ force: true }) });
      const result = data.result || {};
      if (result.ok) {
        box.innerHTML = `<div class="claim">
          <div class="claim-head"><p class="claim-text">已发送到微信</p>${chip(result.date || "", "ok")}</div>
          <div class="claim-rationale">${esc(result.title || "")}</div>
        </div>`;
      } else if (result.skipped) {
        box.innerHTML = `<p class="empty">没有发送：${esc(result.reason || "已跳过")}</p>`;
      } else {
        box.innerHTML = `<p class="empty">发送失败：${esc(result.detail || result.reason || "未知原因")}</p>`;
      }
      const state = data.push || {};
      if (box && state.status) {
        box.insertAdjacentHTML("beforeend",
          `<p class="hint">最近记录：${esc(state.date || "")} · ${esc(state.status)}${state.detail ? " · " + esc(state.detail) : ""}</p>`);
      }
    } catch (error) {
      box.innerHTML = `<p class="empty">发送失败：${esc(error.message)}</p>`;
    }
  }

  async function save() {
    const bodyData = {
      llm: {
        enabled: $("#setEnabled").value === "1",
        provider: $("#setProvider").value,
        base_url: $("#setBaseUrl").value.trim(),
        model: $("#setModel").value.trim(),
        // token 限额跟 llm 走，避免前后端字段位置不一致
        token_limits: {
          primary: Number($("#setTokenLimitPrimary").value || 0),
          fallback: Number($("#setTokenLimitFallback").value || 0),
          window_hours: Number($("#setTokenWindowHours").value || 24),
        },
      },
      ai: {
        enable_briefs: $("#setBriefsEnabled").value === "1",
        concurrency: Number($("#setConcurrency").value),
      },
      push: {
        enabled: $("#setPushEnabled").value === "1",
      },
      fallback: {
        enabled: $("#setFallbackEnabled").value === "1",
        provider: $("#setFallbackProvider").value,
        base_url: $("#setFallbackBaseUrl").value.trim(),
        model: $("#setFallbackModel").value.trim(),
      },
      api_key: $("#setApiKey") ? $("#setApiKey").value.trim() : "",
      fallback_api_key: $("#setFallbackKey") ? $("#setFallbackKey").value.trim() : "",
    };
    try {
      const data = await api("/api/ai/settings", { method: "POST", body: JSON.stringify(bodyData) });
      const keys = Object.keys(data.saved || {});
      toast(keys.length ? "已保存：" + keys.join("、") : "没有变化");
      render(data.settings);
    } catch (error) {
      toast("保存失败：" + error.message, true);
    }
  }

  async function test() {
    const box = $("#testResult");
    box.innerHTML = `<p class="pending">正在请求模型…</p>`;
    try {
      const data = await api("/api/ai/test", { method: "POST", body: "{}" });
      const result = data.result || {};
      if (result.ok) {
        box.innerHTML = `<div class="claim">
          <div class="claim-head"><p class="claim-text">模型返回正常</p>${chip(result.latency_ms + "ms", "ok")}</div>
          <div class="claim-rationale">模型：${esc(result.model || "")} · 接口：${esc(result.base_url || "")}</div>
          ${result.sample ? `<div class="readme" style="margin-top:8px"><b>返回</b><span>${esc(result.sample)}</span></div>` : ""}
          ${result.note ? `<p class="hint">${esc(result.note)}</p>` : ""}
        </div>`;
        toast("模型可用");
      } else {
        box.innerHTML = errorBlock(result);
        toast("连接失败，详情见下方", true);
      }
      loadErrors();
    } catch (error) {
      box.innerHTML = `<p class="empty">测试失败：${esc(error.message)}</p>`;
      toast("测试失败：" + error.message, true);
    }
  }

  async function clearKey() {
    try {
      const data = await api("/api/ai/clear-key", { method: "POST", body: "{}" });
      toast("已清除本地密钥");
      render(data.settings);
    } catch (error) {
      toast("清除失败：" + error.message, true);
    }
  }

  async function load() {
    body.innerHTML = `<div class="panel"><p class="pending">正在读取设置…</p></div>`;
    try {
      render(await api("/api/ai/settings"));
    } catch (error) {
      body.innerHTML = `<div class="error-state"><div class="empty-mark">!</div>
        <h3>读取失败</h3><p>${esc(error.message)}</p>
        <button class="ghost-btn" id="retry">重试</button></div>`;
      $("#retry").addEventListener("click", load);
    }
  }

  $("#reloadBtn").addEventListener("click", load);
  load();
})();
