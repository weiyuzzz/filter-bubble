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

    $("#aiDot").className = "live-dot" + (llm.available ? "" : " live");
    $("#aiState").textContent = llm.available ? "模型可用" : "模型不可用";

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
    loadErrors();
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
      api_key: $("#setApiKey") ? $("#setApiKey").value.trim() : "",
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
