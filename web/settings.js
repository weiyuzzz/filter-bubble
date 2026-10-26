/* 全局设置页：模型接口（1..N 个）、服务商预设、连接测试、接口健康、额度用量、错误日志 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const body = $("#settingsBody");
  const toasts = $("#toasts");

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const SLOT_COLORS = ["var(--fb-accent)", "var(--fb-warn)", "#7c5cff", "#0ea5a4", "#e0679b", "#c98a00",
    "#3b82f6", "#d946ef"];
  const USAGE_CLASS = ["slot-c1", "slot-c2", "slot-c3", "slot-c4", "slot-c5", "slot-c6"];
  const QUOTA_MODES = [["daily", "每日（自然日清零）"], ["window", "窗口（每 N 小时刷新）"],
    ["total", "总量（累计抵扣，套餐常见）"]];
  const HEALTH_LABEL = {
    ok: "正常", invalid: "配置有误", exhausted: "额度已用尽", throttled: "被限流",
    error: "上次失败", unknown: "未测过",
  };
  const HEALTH_KIND = { ok: "ok", invalid: "warn", exhausted: "warn", throttled: "info",
    error: "warn", unknown: "" };

  let PRESETS = [];
  let PROVIDER_LABELS = { openai: "OpenAI 兼容", anthropic: "Anthropic (Claude)", local: "本地模型" };
  let PROVIDERS = ["openai", "anthropic", "local"];

  function toast(message, bad = false) {
    const node = document.createElement("div");
    node.className = "toast" + (bad ? " bad" : "");
    node.textContent = message;
    toasts.appendChild(node);
    setTimeout(() => node.remove(), 6000);
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
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");

  function presetOf(id) {
    return PRESETS.find((item) => item.id === id) || null;
  }

  // ---------------- Token 用量 ----------------

  function usageMeters(usage, slots) {
    const slotUsage = (usage && usage.slots) || {};
    return `<div class="usage-grid">${slots.map((slot, index) => {
      const data = slotUsage[slot.id] || {};
      const limit = Number(data.limit || 0);
      const used = Number(data.tokens || 0);
      const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
      const pct = limit > 0 ? Math.round(ratio * 100) : null;
      const mode = data.mode || "daily";
      const modeText = mode === "total" ? "总量" : (mode === "window" ? "窗口" : "每日");
      return `<div class="usage-meter">
        <div class="usage-meter-head">
          <span>${esc(slot.name || slot.id)} · ${esc(modeText)}</span>
          <b>${fmt(used)}${limit > 0 ? ` / ${fmt(limit)}（${pct}%）` : "（不限）"}</b>
        </div>
        <div class="usage-track"><span class="usage-fill ${USAGE_CLASS[index % USAGE_CLASS.length]}"
          style="width:${pct === null ? 0 : Math.max(2, pct)}%"></span></div>
        <div class="usage-sub">调用 ${fmt(data.calls || 0)} 次 · 输入 ${fmt(data.prompt)} · 输出 ${fmt(data.completion)}
          ${mode !== "total" ? ` · 累计 ${fmt(data.total_tokens || 0)}` : ""}
          ${limit > 0 && data.reset_at ? ` · ${esc(String(data.reset_at).slice(0, 16).replace("T", " "))} 回满` : ""}</div>
      </div>`;
    }).join("")}</div>`;
  }

  function usageChart(usage, slots) {
    const ids = slots.map((slot) => slot.id);
    const series = ((usage && usage.history) || []).slice(-14).map((day) => {
      const row = { date: (day.date || "").slice(5) };
      ids.forEach((id) => {
        const tokens = day.tokens && day.tokens[id] !== undefined ? day.tokens[id] : day[id];
        row[id] = Number(tokens || 0);
      });
      return row;
    });
    const current = {};
    ids.forEach((id) => { current[id] = Number((((usage || {}).slots || {})[id] || {}).tokens || 0); });
    series.push({ date: ((usage || {}).date || "").slice(5), ...current });

    const hasData = series.some((row) => ids.some((id) => row[id] > 0));
    if (!hasData && series.length <= 1) {
      return `<div class="usage-chart is-empty">
        <p class="hint">还没有 token 消耗记录。生成 AI 解读、雷达摘要或论文概况后，这里会按天显示各接口的用量曲线。</p>
      </div>`;
    }

    const W = 720, H = 220, padL = 56, padR = 16, padT = 18, padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const maxValue = Math.max(1, ...series.map((row) => Math.max(...ids.map((id) => row[id]))));
    const step = Math.pow(10, Math.floor(Math.log10(maxValue)));
    const top = Math.ceil(maxValue / step) * step;
    const x = (index) => series.length === 1 ? padL + plotW / 2 : padL + (plotW * index) / (series.length - 1);
    const y = (value) => padT + plotH - (plotH * (value / top));

    const gridLines = [0, 0.25, 0.5, 0.75, 1].map((factor) => {
      const value = top * factor;
      const yy = y(value);
      return `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}"
                stroke="var(--fb-border)" stroke-dasharray="${factor === 0 ? "0" : "3 4"}"/>
              <text x="${padL - 8}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end" font-size="9"
                fill="var(--fb-text-3)">${fmt(Math.round(value))}</text>`;
    }).join("");
    const xLabels = series.map((row, index) => {
      const show = series.length <= 8 || index % Math.ceil(series.length / 8) === 0 || index === series.length - 1;
      return show ? `<text x="${x(index).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="9"
        fill="var(--fb-text-3)">${esc(row.date)}</text>` : "";
    }).join("");

    const lines = ids.map((id, index) => {
      const color = SLOT_COLORS[index % SLOT_COLORS.length];
      const path = series.map((row, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(row[id]).toFixed(1)}`).join(" ");
      const dots = series.map((row, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(row[id]).toFixed(1)}" r="3"
          fill="${color}"><title>${esc(row.date)}：${fmt(row[id])} tokens</title></circle>`).join("");
      return `<path d="${path}" fill="none" stroke="${color}" stroke-width="2.2"
                stroke-linejoin="round" stroke-linecap="round"
                ${index > 0 ? 'stroke-dasharray="5 4"' : ""}/>${dots}`;
    }).join("");

    const legend = slots.map((slot, index) =>
      `<span class="usage-legend"><i class="dot" style="background:${SLOT_COLORS[index % SLOT_COLORS.length]}"></i>${esc(slot.name || slot.id)}</span>`)
      .join(" ");

    return `<div class="usage-chart">
      <div class="usage-chart-head">
        <span>最近 ${series.length} 天 token 消耗（纵轴：tokens）</span>
        <span class="usage-legend">${legend}</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="各接口 token 消耗折线图" preserveAspectRatio="xMidYMid meet">
        ${gridLines}${lines}${xLabels}
      </svg>
    </div>`;
  }

  // ---------------- 接口卡片 ----------------

  function quotaFields(slot) {
    const quota = slot.quota || {};
    const mode = quota.mode || "daily";
    return `<div class="field">
        <label>额度模式</label>
        <select data-slot-field="quota.mode">${QUOTA_MODES.map(([value, label]) =>
          `<option value="${value}" ${mode === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        <span class="field-hint">按服务商怎么收费选：每天限额、每 N 小时刷新，还是买断总量。</span>
      </div>
      <div class="field">
        <label>额度上限（tokens）</label>
        <input data-slot-field="quota.limit" type="number" min="0" step="1000" value="${esc(quota.limit || 0)}">
        <span class="field-hint">0 表示不限；达到上限会自动换下一个接口。</span>
      </div>
      <div class="field" data-quota-for="window" ${mode === "window" ? "" : "hidden"}>
        <label>窗口长度（小时）</label>
        <input data-slot-field="quota.window_hours" type="number" min="0.5" max="168" step="0.5" value="${esc(quota.window_hours || 24)}">
        <span class="field-hint">套餐按窗口刷新的填这里（例如千帆 5 小时）。</span>
      </div>
      <div class="field" data-quota-for="total" ${mode === "total" ? "" : "hidden"}>
        <label>重置周期（天）</label>
        <input data-slot-field="quota.reset_days" type="number" min="0" max="365" value="${esc(quota.reset_days || 0)}">
        <span class="field-hint">0 表示不自动重置（用完为止）。</span>
      </div>`;
  }

  function slotCard(slot, index, data) {
    const quota = slot.quota || {};
    const preset = presetOf(slot.preset) || {};
    const key = slot.key || {};
    const health = slot.health || {};
    const usage = slot.usage || {};
    const lastUsed = (data.llm.last_used || {}).slot === slot.id;
    const isActive = (data.llm.active_slot || "primary") === slot.id;
    const models = preset.models || [];
    const listId = `models-${slot.id}`;
    const cooldown = health.blocked && health.cooldown_until
      ? `冷却到 ${esc(String(health.cooldown_until).slice(0, 16).replace("T", " "))}` : "";

    return `<section class="panel config-card slot-card ${slot.enabled ? "" : "is-off"}"
        data-slot="${esc(slot.id)}" data-active="${isActive ? "1" : "0"}"
        style="--slot-accent:${SLOT_COLORS[index % SLOT_COLORS.length]}">
      <header class="config-head">
        <div>
          <div class="slot-head">
            <span class="slot-rank">${index + 1}</span>
            <input class="slot-name" data-slot-field="name" value="${esc(slot.name || slot.id)}"
                   aria-label="接口名称">
            ${isActive ? chip("优先使用", "ok") : ""}
            ${lastUsed ? chip("上次实际调用", "info") : ""}
          </div>
          <p class="config-sub">${esc(preset.note || "按顺序尝试：前面的接口失败或额度用尽时自动换下一个。")}</p>
        </div>
        <div class="config-state">
          ${slot.available ? chip("配置可用", "ok") : chip("不可用", "warn")}
          ${chip(HEALTH_LABEL[health.state] || health.state || "未测过", HEALTH_KIND[health.state] || "")}
          ${slot.enabled ? "" : chip("已停用")}
        </div>
      </header>

      <div class="config-grid">
        <div class="field">
          <label>启用</label>
          <select data-slot-field="enabled">
            <option value="1" ${slot.enabled ? "selected" : ""}>启用</option>
            <option value="0" ${slot.enabled ? "" : "selected"}>停用</option>
          </select>
        </div>
        <div class="field">
          <label>服务商预设</label>
          <select data-slot-field="preset">
            <option value="">自定义</option>
            ${PRESETS.map((item) => `<option value="${esc(item.id)}" ${slot.preset === item.id ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
          </select>
          <span class="field-hint" data-slot-hint>${esc(preset.note || "选预设会带出地址、协议与 Key 归属。")}</span>
        </div>
        <div class="field">
          <label>接口协议</label>
          <select data-slot-field="provider">
            ${PROVIDERS.map((value) => `<option value="${esc(value)}" ${slot.provider === value ? "selected" : ""}>${esc(PROVIDER_LABELS[value] || value)}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>模型名</label>
          <input data-slot-field="model" type="text" list="${esc(listId)}" value="${esc(slot.model || "")}"
                 placeholder="例如 deepseek-chat">
          <datalist id="${esc(listId)}">${models.map((name) => `<option value="${esc(name)}"></option>`).join("")}</datalist>
        </div>
        <div class="field config-wide">
          <label>接口地址</label>
          <input data-slot-field="base_url" type="text" value="${esc(slot.base_url || "")}"
                 placeholder="例如 https://qianfan.baidubce.com/v2">
          <span class="field-hint">支持 <code>/v1</code> <code>/v2</code> <code>/v3</code> <code>/v4</code> 结尾，也支持直接填完整端点；没有版本号时自动补 <code>/v1</code>。</span>
        </div>
        <div class="field">
          <label>Key 归属（key_ref）</label>
          <input data-slot-field="key_ref" type="text" value="${esc(slot.key_ref || slot.id)}"
                 placeholder="例如 deepseek / qianfan">
          <span class="field-hint">Key 按这个名字保存，选预设会一起带过来；两个接口填同一个名字就共用一把 Key。
            <b>换服务商时这里必须一起改</b>，否则会把上一家的 Key 发到新地址上（401）。</span>
        </div>
        <div class="field config-wide">
          <label for="key-${esc(slot.id)}">API Key ${key.configured ? `（已配置 ${esc(key.masked)} · ${esc(key.source)}）` : "（未配置）"}</label>
          <input id="key-${esc(slot.id)}" data-slot-field="key" type="password" autocomplete="off" spellcheck="false"
                 placeholder="${key.configured ? "已保存，留空表示不修改" : "粘贴这家服务商的 Key"}">
          <span class="field-hint">
            ${key.configured ? `<button class="link-btn" data-act="clear-key" type="button">清除这个 Key</button>` : "密钥只写本机 data/ai_secrets.json，接口只回掩码。"}
          </span>
        </div>
        ${quotaFields(slot)}
        <div class="field config-wide">
          <label>该接口用量</label>
          <div class="slot-usage-line">
            ${esc(usage.mode === "total" ? "总量" : (usage.mode === "window" ? "窗口" : "今日"))}：
            ${fmt(usage.tokens)} tokens（${fmt(usage.calls)} 次调用，输入 ${fmt(usage.prompt)} / 输出 ${fmt(usage.completion)}）
            累计：${fmt(usage.total_tokens)}${usage.limit > 0 ? ` / 上限 ${fmt(usage.limit)}` : " / 不限"}
            ${usage.reset_at ? ` · ${esc(String(usage.reset_at).slice(0, 16).replace("T", " "))} 恢复` : ""}
          </div>
          <div class="slot-health">
            ${slot.unavailable_reason ? `<span>未就绪原因：<code>${esc(slot.unavailable_reason)}</code></span>` : ""}
            <span>上次成功：<code>${esc((health.last_ok_at || "—").slice(0, 19).replace("T", " "))}</code>
              · 累计成功 ${esc(health.ok_calls || 0)} 次 · 连续失败 ${esc(health.fail_streak || 0)} 次</span>
            ${health.last_error ? `<span>上次错误（<code>${esc((health.last_error_at || "").slice(0, 19).replace("T", " "))}</code>）：<code>${esc(health.last_error)}</code></span>` : ""}
            ${cooldown ? `<span>${esc(cooldown)}</span>` : ""}
          </div>
        </div>
      </div>

      <div class="config-actions slot-actions">
        <button class="primary-btn" data-act="save" type="button">保存设置</button>
        <button class="ghost-btn" data-act="test" type="button">测试连接</button>
        <button class="ghost-btn" data-act="up" type="button" ${index === 0 ? "disabled" : ""}>上移</button>
        <button class="ghost-btn" data-act="down" type="button" ${index === (data.llm.slots || []).length - 1 ? "disabled" : ""}>下移</button>
        <button class="danger-btn" data-act="delete" type="button">删除接口</button>
      </div>
      <div data-test-result style="margin-top:10px"></div>
    </section>`;
  }

  function errorBlock(result) {
    const error = result.error || {};
    const rows = [];
    if (result.reason) rows.push(`原因：${result.reason}`);
    if (result.kind) rows.push(`错误类型：${result.kind}`);
    if (error.status) rows.push(`HTTP 状态：${error.status}`);
    if (error.code) rows.push(`上游错误码：${error.code}`);
    if (error.message) rows.push(`上游消息：${error.message}`);
    if (error.detail) rows.push(`上游原始返回：${error.detail}`);
    if (result.hint || error.hint) rows.push(`建议：${result.hint || error.hint}`);
    if (error.suggested_model) rows.push(`可改用模型：${error.suggested_model}`);
    if (error.request_id) rows.push(`请求 ID：${error.request_id}`);
    if (result.log_path) rows.push(`错误日志：${result.log_path}`);
    return `<div class="error-state" style="align-items:stretch;text-align:left;padding:10px 0">
      <h3 style="color:var(--fb-danger)">连接失败</h3>
      <pre style="white-space:pre-wrap;word-break:break-word;background:var(--fb-surface-2);border:1px solid var(--fb-border);border-radius:var(--fb-r-md);padding:12px;font-size:12px;line-height:1.7;margin-top:8px">${esc(rows.join("\n"))}</pre>
    </div>`;
  }

  // ---------------- 渲染 ----------------

  function render(data) {
    const llm = data.llm || {};
    const ai = data.ai || {};
    const push = data.push || {};
    const slots = llm.slots || [];
    const usage = llm.usage || { slots: {}, history: [], paused: false };
    const stats = llm.stats || {};
    const lastUsed = llm.last_used || {};
    const warnings = llm.warnings || [];
    PRESETS = llm.presets || [];
    PROVIDER_LABELS = llm.provider_labels || PROVIDER_LABELS;
    PROVIDERS = llm.providers || PROVIDERS;

    $("#aiDot").className = "live-dot" + (llm.available ? "" : " live");
    $("#aiState").textContent = llm.available
      ? (slots.length > 1 ? `已配置 ${slots.length} 个接口` : "模型可用")
      : "模型不可用";

    const paused = usage.paused;
    body.innerHTML = `
      <section class="panel config-card">
        <header class="config-head">
          <div>
            <h3>模型接口</h3>
            <p class="config-sub">按顺序尝试（数字越小越优先）：某个接口失败或额度用尽时自动换下一个，全部用完才暂停。三个模块共用这里的配置。</p>
          </div>
          <div class="config-state">
            ${llm.available ? chip("至少一个可用", "ok") : chip("都不可用", "warn")}
            ${lastUsed.slot ? chip(`上次：${lastUsed.name || lastUsed.slot} · ${lastUsed.model || ""}`, "info") : ""}
            ${stats.calls ? chip(`已调用 ${stats.calls} 次`) : ""}
          </div>
        </header>

        ${warnings.length ? `<ul class="warn-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}

        <div class="field-row">
          <div class="field">
            <label for="setEnabled">接入大模型</label>
            <select id="setEnabled">
              <option value="1" ${llm.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${llm.enabled ? "" : "selected"}>关闭（用本地规则）</option>
            </select>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <div class="config-static">${llm.available ? "当前可用" : esc(llm.unavailable_reason || "不可用")}</div>
          </div>
        </div>

        <div class="slot-list">
          ${slots.length ? slots.map((slot, index) => slotCard(slot, index, data)).join("")
            : `<div class="config-static">还没有配置任何接口，点下面的「添加接口」开始。</div>`}
        </div>

        <div class="config-actions">
          <button class="primary-btn" data-act="save-all" type="button">保存全部设置</button>
          <button class="ghost-btn" data-act="add" type="button">＋ 添加接口</button>
          <button class="ghost-btn" data-act="test-all" type="button">检测全部接口</button>
          <button class="ghost-btn" data-act="clear-health" type="button">清除失败冷却</button>
        </div>

        <div id="testResult" style="margin-top:12px"></div>

        <details style="margin-top:var(--fb-sp-4)">
          <summary class="config-summary">请求参数（超时 / 重试 / 温度 / 输出上限 / 暂停窗口）</summary>
          <div class="config-grid" style="margin-top:var(--fb-sp-3)">
            <div class="field"><label for="setTimeout">超时（秒）</label>
              <input id="setTimeout" type="number" min="1" max="300" value="${esc(llm.timeout_seconds || 60)}"></div>
            <div class="field"><label for="setAttempts">最大尝试次数</label>
              <input id="setAttempts" type="number" min="1" max="3" value="${esc(llm.max_attempts || 2)}">
              <span class="field-hint">含首次请求；限流会按 Retry-After 退避。</span></div>
            <div class="field"><label for="setTemperature">温度</label>
              <input id="setTemperature" type="number" min="0" max="1.5" step="0.05" value="${esc(llm.temperature ?? 0.2)}"></div>
            <div class="field"><label for="setMaxTokens">最大输出 token</label>
              <input id="setMaxTokens" type="number" min="50" max="8000" step="50" value="${esc(llm.max_tokens || 1500)}">
              <span class="field-hint">推理模型（deepseek-v4.1、glm-5.x 这类会先「思考」）建议 <b>3000 以上</b>，否则预算会被思考过程吃光、正文为空。</span></div>
            <div class="field"><label for="setQuotaWindow">额度窗口 / 暂停时长（小时）</label>
              <input id="setQuotaWindow" type="number" min="1" max="168" value="${esc(llm.quota_window_hours || 24)}">
              <span class="field-hint">所有接口都用尽后暂停多久再恢复计数。</span></div>
          </div>
        </details>
        <p class="hint">密钥只写入本机 <code>${esc(data.secrets_path || "data/ai_secrets.json")}</code>（按服务商分开存），配置文件 <code>${esc(data.path || "app_settings.json")}</code> 里不会有 Key。</p>
      </section>

      <section class="panel config-card">
        <header class="config-head">
          <div>
            <h3>Token 用量</h3>
            <p class="config-sub">本地记账，用来在超限前切换接口，而不是等报错。</p>
          </div>
          <div class="config-state">
            ${paused
              ? `${chip("AI 已暂停", "warn")}<button class="ghost-btn" id="resumeAiBtn" type="button">立即恢复</button>`
              : chip("正常", "ok")}
          </div>
        </header>
        ${paused && usage.paused_until
          ? `<p class="hint">暂停到 ${esc(String(usage.paused_until).slice(0, 19).replace("T", " "))} 自动恢复并重新计数。</p>` : ""}
        ${usageMeters(usage, slots)}
        ${usageChart(usage, slots)}
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
        <p class="hint">日报里的每个项目都会生成 AI 解读（不再限制条数），成本由上面的额度上限控制；结果按「仓库 + 模型」缓存，不会重复消耗。</p>
        <div class="config-actions">
          <button class="primary-btn" data-act="save-all" type="button">保存设置</button>
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
          <button class="primary-btn" data-act="save-all" type="button">保存设置</button>
          <button class="ghost-btn" id="pushNowBtn" type="button">立即推送一次</button>
        </div>
      </section>

      <section class="panel">
        <h3>最近错误日志 <small>${esc(data.error_log || "")}</small></h3>
        <div id="errorLog"><p class="pending">读取中…</p></div>
      </section>
    `;

    const resumeAi = $("#resumeAiBtn");
    if (resumeAi) resumeAi.addEventListener("click", resumeAiNow);
    const pushNow = $("#pushNowBtn");
    if (pushNow) pushNow.addEventListener("click", pushNowSend);

    loadErrors();
  }

  // ---------------- 交互 ----------------

  function slotNode(target) {
    return target && target.closest ? target.closest("[data-slot]") : null;
  }

  function orderedIds() {
    return $$("[data-slot]").map((node) => node.dataset.slot);
  }

  function collectSlot(node, index) {
    const val = (field) => {
      const el = node.querySelector(`[data-slot-field="${field}"]`);
      return el ? el.value : "";
    };
    const num = (field, fallback) => {
      const parsed = Number(val(field));
      return Number.isFinite(parsed) ? parsed : fallback;
    };
    const slot = {
      id: node.dataset.slot,
      name: (val("name") || "").trim() || node.dataset.slot,
      enabled: val("enabled") === "1",
      preset: val("preset"),
      provider: val("provider"),
      base_url: (val("base_url") || "").trim(),
      model: (val("model") || "").trim(),
      key_ref: (val("key_ref") || "").trim() || node.dataset.slot,
      priority: index + 1,
      quota: {
        mode: val("quota.mode") || "daily",
        limit: num("quota.limit", 0),
        window_hours: num("quota.window_hours", 24),
        reset_days: num("quota.reset_days", 0),
      },
    };
    const key = (val("key") || "").trim();
    if (key) slot.key = key;
    if (node.dataset.clearKey === "1") slot.clear_key = true;
    return slot;
  }

  async function postSettings(payload) {
    const data = await api("/api/ai/settings", { method: "POST", body: JSON.stringify(payload) });
    return data;
  }

  async function saveAll() {
    const slots = $$("[data-slot]").map((node, index) => collectSlot(node, index));
    const payload = {
      llm: {
        enabled: $("#setEnabled") ? $("#setEnabled").value === "1" : true,
        timeout_seconds: Number(($("#setTimeout") || {}).value || 60),
        max_attempts: Number(($("#setAttempts") || {}).value || 2),
        temperature: Number(($("#setTemperature") || {}).value ?? 0.2),
        max_tokens: Number(($("#setMaxTokens") || {}).value || 1500),
        quota_window_hours: Number(($("#setQuotaWindow") || {}).value || 24),
        slots,
      },
      ai: {
        enable_briefs: $("#setBriefsEnabled") ? $("#setBriefsEnabled").value === "1" : true,
        concurrency: Number(($("#setConcurrency") || {}).value || 1),
      },
      push: { enabled: $("#setPushEnabled") ? $("#setPushEnabled").value === "1" : undefined },
    };
    if (payload.push.enabled === undefined) delete payload.push;
    try {
      const data = await postSettings(payload);
      const keys = Object.keys(data.saved || {});
      (data.warnings || []).forEach((warning) => toast(warning, true));
      toast(keys.length ? "已保存：" + keys.slice(0, 6).join("、") : "没有变化", false);
      render(data.settings);
      const keyed = data.api_key_slots || [];
      if (keyed.length) await verifySlots(keyed);
    } catch (error) {
      toast("保存失败：" + error.message, true);
    }
  }

  function resultHtml(result) {
    if (!result) return `<p class="empty">没有返回结果</p>`;
    if (result.skipped) return `<div class="error-state" style="padding:10px 0"><p>跳过：${esc(result.reason || "未就绪")}</p></div>`;
    if (result.ok) {
      return `<div class="claim">
        <div class="claim-head"><p class="claim-text">返回正常</p>${chip((result.latency_ms || 0) + "ms", "ok")}</div>
        <div class="claim-rationale">模型：${esc(result.model || "")} · 接口：${esc(result.base_url || "")} ·
          tokens：${esc(result.tokens || 0)}</div>
        ${result.sample ? `<div class="readme" style="margin-top:8px"><b>返回</b><span>${esc(result.sample)}</span></div>` : ""}
        ${result.note ? `<p class="hint">${esc(result.note)}</p>` : ""}
      </div>`;
    }
    return errorBlock(result);
  }

  async function verifySlots(ids) {
    const box = $("#testResult");
    for (const id of ids) {
      if (box) box.innerHTML = `<p class="pending">正在验证「${esc(id)}」…</p>`;
      try {
        const data = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ slot: id, record: true }) });
        const result = data.result || {};
        toast((result.ok ? "接口可用：" : "接口不可用：") + (result.name || id), !result.ok);
      } catch (error) {
        toast("自检失败：" + error.message, true);
      }
    }
    try {
      render(await api("/api/ai/settings"));
    } catch (error) {
      toast("刷新失败：" + error.message, true);
    }
  }

  async function testSlot(id) {
    const node = $(`[data-slot="${id}"]`);
    const box = node ? $("[data-test-result]", node) : null;
    if (box) box.innerHTML = `<p class="pending">正在请求接口…</p>`;
    try {
      const data = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ slot: id, record: true }) });
      const result = data.result || {};
      toast(result.ok ? "接口可用" : "接口不可用", !result.ok);
      render(data.settings);
      const fresh = $(`[data-slot="${id}"]`);
      const freshBox = fresh ? $("[data-test-result]", fresh) : null;
      if (freshBox) freshBox.innerHTML = resultHtml(result);
    } catch (error) {
      if (box) box.innerHTML = `<p class="empty">测试失败：${esc(error.message)}</p>`;
      toast("测试失败：" + error.message, true);
    }
  }

  async function testAll() {
    const box = $("#testResult");
    if (box) box.innerHTML = `<p class="pending">正在依次测试所有接口…</p>`;
    try {
      const data = await api("/api/ai/health/test-all", { method: "POST", body: "{}" });
      const results = ((data.results || {}).results) || [];
      if (box) box.innerHTML = `<div class="claim">
        <div class="claim-head"><p class="claim-text">检测完成</p>
          ${chip(`${results.filter((r) => r.ok).length}/${results.length} 可用`, results.every((r) => r.ok) ? "ok" : "warn")}</div>
        <div class="claim-rationale">${results.map((r) =>
          `${esc(r.name || r.slot || "")}：${r.ok ? "可用 " + (r.latency_ms || 0) + "ms" : esc(r.reason || "不可用")}`).join("<br>")}</div>
      </div>`;
      render(data.settings);
    } catch (error) {
      if (box) box.innerHTML = `<p class="empty">检测失败：${esc(error.message)}</p>`;
    }
  }

  async function onSlotAction(action, node) {
    const id = node.dataset.slot;
    if (action === "test") return testSlot(id);
    if (action === "clear-key") {
      node.dataset.clearKey = "1";
      const input = $('[data-slot-field="key"]', node);
      if (input) input.value = "";
      return saveAll();
    }
    if (action === "delete") {
      if (!window.confirm(`删除接口「${$('[data-slot-field="name"]', node).value || id}」？`)) return;
      try {
        const data = await postSettings({ delete_slots: [id] });
        toast("已删除接口");
        render(data.settings);
      } catch (error) {
        toast("删除失败：" + error.message, true);
      }
      return;
    }
    if (action === "up" || action === "down") {
      const ids = orderedIds();
      const index = ids.indexOf(id);
      const next = index + (action === "up" ? -1 : 1);
      if (index < 0 || next < 0 || next >= ids.length) return;
      [ids[index], ids[next]] = [ids[next], ids[index]];
      try {
        const data = await postSettings({ slots: ids.map((sid, position) => ({ id: sid, priority: position + 1 })) });
        render(data.settings);
      } catch (error) {
        toast("排序失败：" + error.message, true);
      }
    }
  }

  async function onClick(event) {
    const target = event.target.closest("[data-act]");
    if (!target) return;
    const action = target.dataset.act;
    const node = slotNode(target);
    event.preventDefault();
    if (action === "save" || action === "save-all") return saveAll();
    if (action === "add") {
      try {
        const ids = orderedIds();
        const newId = "slot-" + Date.now().toString(36).slice(-4);
        const data = await postSettings({ slots: [{
          id: newId, name: "新接口", enabled: false, provider: "openai", preset: "",
          base_url: "", model: "", key_ref: newId, priority: ids.length + 1,
          quota: { mode: "daily", limit: 0, window_hours: 24, reset_days: 0 },
        }] });
        toast("已添加接口，填好地址和 Key 后点保存");
        render(data.settings);
      } catch (error) {
        toast("添加失败：" + error.message, true);
      }
      return;
    }
    if (action === "test-all") return testAll();
    if (action === "clear-health") {
      try {
        const data = await api("/api/ai/health/reset", { method: "POST", body: JSON.stringify({ reset_slot: true }) });
        toast("已清除失败冷却与切换状态");
        render(data.settings);
      } catch (error) {
        toast("清除失败：" + error.message, true);
      }
      return;
    }
    if (node) return onSlotAction(action, node);
  }

  function onChange(event) {
    const field = event.target.dataset ? event.target.dataset.slotField : null;
    if (!field) return;
    const node = slotNode(event.target);
    if (!node) return;
    if (field === "preset") {
      const preset = presetOf(event.target.value);
      if (!preset) return;
      const set = (name, value) => {
        const el = node.querySelector(`[data-slot-field="${name}"]`);
        if (el && value !== undefined && value !== null && value !== "") el.value = value;
      };
      set("provider", preset.protocol);
      set("base_url", preset.base_url);
      set("key_ref", preset.key_ref);
      const modelEl = node.querySelector('[data-slot-field="model"]');
      if (modelEl && !modelEl.value.trim() && (preset.models || []).length) modelEl.value = preset.models[0];
      const quota = preset.quota || {};
      if (quota.mode) {
        set("quota.mode", quota.mode);
        const windowEl = node.querySelector('[data-slot-field="quota.window_hours"]');
        if (windowEl && quota.window_hours) windowEl.value = quota.window_hours;
        toggleQuotaFields(node);
      }
      const hint = node.querySelector("[data-slot-hint]");
      if (hint) hint.textContent = preset.note || "";
      toast(`已套用预设「${preset.label}」，记得填 Key 后保存`);
    }
    if (field === "quota.mode") toggleQuotaFields(node);
  }

  function toggleQuotaFields(node) {
    const mode = (node.querySelector('[data-slot-field="quota.mode"]') || {}).value || "daily";
    $$("[data-quota-for]", node).forEach((el) => {
      el.hidden = el.dataset.quotaFor !== mode;
    });
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
    } catch (error) {
      box.innerHTML = `<p class="empty">发送失败：${esc(error.message)}</p>`;
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
  // 事件委托只挂一次：#settingsBody 的内容会被整体替换，但容器本身不变
  body.addEventListener("click", onClick);
  body.addEventListener("change", onChange, true);
  load();
})();