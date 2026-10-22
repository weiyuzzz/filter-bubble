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

    const series = (usage.history || []).slice(-14);
    series.push({ date: usage.date || "", primary: Number(primary.tokens || 0),
                  fallback: Number(fallback.tokens || 0) });
    const max = Math.max(1, ...series.map((d) => Number(d.primary || 0) + Number(d.fallback || 0)));
    const W = 640, H = 150, padL = 30, padB = 22, gap = 8;
    const bw = series.length ? Math.max(6, (W - padL - 10 - gap * (series.length - 1)) / series.length) : 10;
    const chartH = H - padB - 18;
    const bars = series.map((d, i) => {
      const pv = Number(d.primary || 0);
      const fv = Number(d.fallback || 0);
      const x = padL + i * (bw + gap);
      const hp = (pv / max) * chartH;
      const hf = (fv / max) * chartH;
      const yf = H - padB - hf;
      const yp = yf - hp;
      const label = (d.date || "").slice(5);
      return `<g>
        <title>${esc(d.date || "")}：主 ${fmt(pv)} · 备 ${fmt(fv)}</title>
        <rect x="${x.toFixed(1)}" y="${yp.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0, hp).toFixed(1)}" rx="3" fill="var(--fb-accent)"/>
        <rect x="${x.toFixed(1)}" y="${yf.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0, hf).toFixed(1)}" rx="3" fill="var(--fb-warn)"/>
        ${i % 2 === 0 || series.length <= 7 ? `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="9" fill="var(--fb-text-3)">${esc(label)}</text>` : ""}
      </g>`;
    }).join("");

    const paused = usage.paused;
    const hasData = series.some((d) => Number(d.primary || 0) + Number(d.fallback || 0) > 0);
    const chart = hasData
      ? `<div class="usage-chart">
          <div class="usage-chart-head">
            <span>最近 ${series.length} 天 token 消耗</span>
            <span class="usage-legend"><i class="dot primary"></i>主接口 <i class="dot fallback"></i>备用接口</span>
          </div>
          <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="最近 token 消耗柱状图" preserveAspectRatio="xMidYMid meet">
            <line x1="${padL}" y1="${H - padB}" x2="${W - 10}" y2="${H - padB}" stroke="var(--fb-border)"/>
            ${bars}
          </svg>
        </div>`
      : `<div class="usage-chart is-empty">
          <p class="hint">还没有 token 消耗记录。生成 AI 解读、雷达摘要或论文概况后，这里会按天显示主/备接口的用量。</p>
        </div>`;

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

    $("#aiDot").className = "live-dot" + (llm.available ? "" : " live");
    $("#aiState").textContent = slot.active === "fallback"
      ? "使用备用模型"
      : (llm.available ? "模型可用" : "模型不可用");

    body.innerHTML = `
      <section class="panel">
        <h3>大模型</h3>
        <div class="field-row">
          <div class="field"><label for="setEnabled">接入大模型</label>
            <select id="setEnabled">
              <option value="1" ${llm.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${llm.enabled ? "" : "selected"}>关闭（用本地规则）</option>
            </select>
          </div>
          <div class="field"><label for="setProvider">接口类型</label>
            <select id="setProvider">
              <option value="openai_compatible" ${llm.provider === "openai_compatible" ? "selected" : ""}>OpenAI 兼容 / OpenRouter</option>
              <option value="ollama" ${llm.provider === "ollama" ? "selected" : ""}>本地 Ollama</option>
            </select>
          </div>
          <div class="field"><label for="setBaseUrl">接口地址</label>
            <input id="setBaseUrl" type="text" placeholder="https://openrouter.ai/api/v1" value="${esc(llm.base_url || "")}">
          </div>
          <div class="field"><label for="setModel">模型名</label>
            <input id="setModel" type="text" placeholder="例如 google/gemma-4-26b-a4b-it:free" value="${esc(llm.model || "")}">
          </div>
        </div>

        <div class="field-row" style="margin-top:12px">
          <div class="field"><label for="setApiKey">API Key ${key.configured ? `（已配置 ${esc(key.masked)} · ${esc(key.source)}）` : "（未配置）"}</label>
            <input id="setApiKey" type="password" autocomplete="off" placeholder="${key.configured ? "留空表示不修改" : "粘贴 OpenRouter 的 sk-or-... key"}">
          </div>
          <div class="field"><label for="setTokenLimitPrimary">每日 token 上限（0 = 不限）</label>
            <input id="setTokenLimitPrimary" type="number" min="0" step="1000" value="${esc(limits.primary || 0)}">
          </div>
          <div class="field"><label for="setTokenWindowHours">统计窗口（小时）</label>
            <input id="setTokenWindowHours" type="number" min="1" max="168" value="${esc(limits.window_hours || 24)}">
          </div>
        </div>

        <div class="ev-meta" style="margin-top:12px">
          ${llm.available ? chip("模型可用", "ok") : chip("模型不可用：" + (llm.unavailable_reason || ""), "warn")}
          ${stats.calls ? chip(`已调用 ${stats.calls} 次`) : ""}
          ${stats.failures ? chip(`失败 ${stats.failures} 次`, "warn") : ""}
          ${stats.prompt_tokens ? chip(`输入 ${stats.prompt_tokens} tokens`) : ""}
          ${stats.completion_tokens ? chip(`输出 ${stats.completion_tokens} tokens`) : ""}
        </div>
        <p class="hint">密钥只写入本机 <code>${esc(data.secrets_path || "data/ai_secrets.json")}</code>，不会进入配置文件，也不会回传到页面。配置写入 <code>${esc(data.path || "app_settings.json")}</code>。</p>

        <div class="ev-actions">
          <button class="primary-btn" id="saveBtn" type="button">保存设置</button>
          <button class="ghost-btn" id="testBtn" type="button">测试连接</button>
          ${key.configured ? `<button class="ghost-btn" id="clearBtn" type="button">清除密钥</button>` : ""}
        </div>
        <div id="testResult" style="margin-top:12px"></div>
      </section>

      <section class="panel">
        <h3>备用接口</h3>
        <p class="hint">主接口额度用尽（429 / free-models-per-day 之类）时自动切到这里，第二天自动切回主接口。三个模块共用这套切换。</p>
        <div class="field-row">
          <div class="field"><label for="setFallbackEnabled">启用备用接口</label>
            <select id="setFallbackEnabled">
              <option value="1" ${fb.enabled ? "selected" : ""}>启用</option>
              <option value="0" ${fb.enabled ? "" : "selected"}>关闭</option>
            </select>
          </div>
          <div class="field"><label for="setFallbackProvider">接口类型</label>
            <select id="setFallbackProvider">
              <option value="openai_compatible" ${fb.provider === "openai_compatible" ? "selected" : ""}>OpenAI 兼容 / OpenRouter</option>
              <option value="ollama" ${fb.provider === "ollama" ? "selected" : ""}>本地 Ollama</option>
            </select>
          </div>
          <div class="field"><label for="setFallbackBaseUrl">接口地址</label>
            <input id="setFallbackBaseUrl" type="text" placeholder="https://openrouter.ai/api/v1" value="${esc(fb.base_url || "")}">
          </div>
          <div class="field"><label for="setFallbackModel">模型名</label>
            <input id="setFallbackModel" type="text" placeholder="例如 openai/gpt-4o-mini" value="${esc(fb.model || "")}">
          </div>
        </div>
        <div class="field-row" style="margin-top:12px">
          <div class="field"><label for="setFallbackKey">备用 API Key ${fbKey.configured ? `（已配置 ${esc(fbKey.masked)}）` : "（未配置）"}</label>
            <input id="setFallbackKey" type="password" autocomplete="off" placeholder="${fbKey.configured ? "留空表示不修改" : "粘贴备用接口的 key"}">
          </div>
          <div class="field"><label for="setTokenLimitFallback">备用接口每日 token 上限（0 = 不限）</label>
            <input id="setTokenLimitFallback" type="number" min="0" step="1000" value="${esc(limits.fallback || 0)}">
          </div>
        </div>
        <div class="ev-meta" style="margin-top:12px">
          ${slot.active === "fallback"
            ? `<span class="chip warn">当前使用备用接口（到 ${esc(switchState.fallback_until || "明天")} 自动切回）</span>`
            : `<span class="chip ok">当前使用主接口</span>`}
          ${fb.available ? chip("备用可用", "ok") : chip("备用未就绪：" + (fb.unavailable_reason || ""), "warn")}
          ${switchState.reason ? `<span class="chip">切换原因：${esc(switchState.reason.slice(0, 40))}</span>` : ""}
        </div>
        <div class="ev-actions">
          <button class="ghost-btn" id="testFallbackBtn" type="button">测试备用接口</button>
          ${slot.active === "fallback" ? `<button class="ghost-btn" id="resetSlotBtn" type="button">立即切回主接口</button>` : ""}
          ${fbKey.configured ? `<button class="ghost-btn" id="clearFallbackKeyBtn" type="button">清除备用密钥</button>` : ""}
        </div>
        <div id="fallbackTestResult" style="margin-top:12px"></div>
      </section>

      <section class="panel">
        <h3>Token 用量</h3>
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
          <div class="field"><label for="setBriefsPerRun">每天生成多少个</label>
            <input id="setBriefsPerRun" type="number" min="0" max="30" value="${esc(ai.briefs_per_run)}">
          </div>
          <div class="field"><label for="setConcurrency">并发数（免费模型建议 1）</label>
            <input id="setConcurrency" type="number" min="1" max="3" value="${esc(ai.concurrency)}">
          </div>
        </div>
        <p class="hint">生成日报时会为排名靠前的项目调用模型，结果按「仓库 + 模型」缓存，第二天不会重复消耗。未生成的项目仍显示规则版解读。</p>
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

  async function save() {
    const bodyData = {
      llm: {
        enabled: $("#setEnabled").value === "1",
        provider: $("#setProvider").value,
        base_url: $("#setBaseUrl").value.trim(),
        model: $("#setModel").value.trim(),
      },
      ai: {
        enable_briefs: $("#setBriefsEnabled").value === "1",
        briefs_per_run: Number($("#setBriefsPerRun").value),
        concurrency: Number($("#setConcurrency").value),
      },
      fallback: {
        enabled: $("#setFallbackEnabled").value === "1",
        provider: $("#setFallbackProvider").value,
        base_url: $("#setFallbackBaseUrl").value.trim(),
        model: $("#setFallbackModel").value.trim(),
      },
      token_limits: {
        primary: Number($("#setTokenLimitPrimary").value || 0),
        fallback: Number($("#setTokenLimitFallback").value || 0),
        window_hours: Number($("#setTokenWindowHours").value || 24),
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
