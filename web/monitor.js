/* Filter Bubble — 采集监控页
 *
 * 只读页面：所有数据来自本机 /api/monitor/*，页面本身不会触发采集、聚类、推送或写库。
 *
 * 安全约定（上游新闻标题、错误信息、日志内容都视为不可信数据）：
 *   1. 一律用 textContent / createElement 渲染，绝不把数据拼进 HTML 字符串；
 *   2. 只有 http/https 且带主机的地址才会渲染成链接（target=_blank + rel=noopener noreferrer）；
 *   3. 日志详情只展示接口返回的脱敏结果，前端不做任何“还原原文”的操作。
 */
(() => {
  "use strict";

  const API = "/api/monitor";
  const PAGE_POLL_MS = 10000;      // 总览/时间线/运行记录的轻量刷新间隔
  const UNKNOWN = "未知";
  const $ = (sel, scope = document) => scope.querySelector(sel);
  const $$ = (sel, scope = document) => Array.from(scope.querySelectorAll(sel));

  /* ------------------------------------------------------------------ 工具 */

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach((key) => {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : String(value));
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" || typeof child === "number"
        ? document.createTextNode(String(child)) : child);
    });
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  /** 事件委派用的安全 closest：target 可能是 document 或文本节点。 */
  function closest(event, selector) {
    const target = event && event.target;
    if (!target || typeof target.closest !== "function") return null;
    return target.closest(selector);
  }

  function show(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback === undefined ? UNKNOWN : fallback;
    return String(value);
  }

  function num(value) {
    if (value === null || value === undefined || value === "") return UNKNOWN;
    const n = Number(value);
    if (!Number.isFinite(n)) return UNKNOWN;
    return n.toLocaleString("zh-CN");
  }

  /** 只有 http/https 才允许当链接；其余（javascript:/data:/radar://…）一律不渲染成链接。 */
  function safeLink(url) {
    if (!url) return "";
    let parsed;
    try { parsed = new URL(String(url), window.location.origin); } catch (e) { return ""; }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
    if (!parsed.hostname) return "";
    return parsed.href;
  }

  function fmtTime(iso) {
    if (!iso) return UNKNOWN;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
           `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function fmtClock(iso) {
    if (!iso) return UNKNOWN;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function fmtDay(iso) {
    if (!iso) return UNKNOWN;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined || ms === "") return UNKNOWN;
    const value = Number(ms);
    if (!Number.isFinite(value)) return UNKNOWN;
    if (value < 1000) return `${Math.round(value)} ms`;
    const seconds = value / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)} s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
  }

  function fmtAge(minutes) {
    if (minutes === null || minutes === undefined) return UNKNOWN;
    const value = Number(minutes);
    if (!Number.isFinite(value)) return UNKNOWN;
    if (value < 1) return "刚刚";
    if (value < 60) return `${Math.round(value)} 分钟前`;
    if (value < 60 * 36) return `${(value / 60).toFixed(1)} 小时前`;
    return `${(value / 1440).toFixed(1)} 天前`;
  }

  const STATUS_KIND = {
    ok: "ok", partial: "warn", failed: "bad", running: "info",
    interrupted: "warn", unknown: "muted", skipped: "muted",
    pending: "muted", not_applicable: "muted", empty: "muted",
    error: "bad", paused: "warn", disabled: "muted", unavailable: "muted",
    new: "ok", updated: "info", duplicate: "muted", filtered: "warn",
    persisted: "info", DEBUG: "muted", INFO: "info", WARNING: "warn", ERROR: "bad",
  };

  function chip(label, kind, title) {
    return el("span", { class: `monitor-chip is-${kind || "muted"}`, text: label, title: title || null });
  }

  function statusChip(status, label) {
    return chip(label || show(status, UNKNOWN), STATUS_KIND[status] || "muted", `status=${show(status, "?")}`);
  }

  function linkOrText(url, label) {
    const href = safeLink(url);
    if (!href) return el("span", { class: "monitor-muted", text: label || url || "（没有链接）" });
    return el("a", { class: "monitor-link", href, target: "_blank", rel: "noopener noreferrer",
                     text: label || href });
  }

  /* ------------------------------------------------------------------ 状态 */

  const state = {
    meta: null,
    overview: null,
    timeline: null,
    runs: { rows: [], total: 0, page: 1, page_size: 25, sort: "started_at", order: "desc" },
    sources: { rows: [], summary: {} },
    logs: { rows: [], total: 0, page: 1, page_size: 50, seen: {} },
    drawer: null,
    poll: null,
    logPoll: null,
    busy: false,
  };

  // openRun / openSource 之前聚焦的那个元素：关抽屉后要还焦点给它
  let drawerOpener = null;

  function notice(message, kind) {
    const box = $("#monitorNotice");
    if (!box) return;
    if (!message) { box.hidden = true; box.textContent = ""; box.className = "monitor-notice"; return; }
    box.hidden = false;
    box.className = `monitor-notice is-${kind || "info"}`;
    clear(box).appendChild(el("span", { text: message }));
  }

  async function apiGet(path, params) {
    const query = new URLSearchParams();
    Object.keys(params || {}).forEach((key) => {
      const value = params[key];
      if (value === null || value === undefined || value === "") return;
      query.set(key, String(value));
    });
    const suffix = query.toString() ? `?${query.toString()}` : "";
    let response;
    try {
      response = await fetch(`${API}${path}${suffix}`, { headers: { Accept: "application/json" } });
    } catch (err) {
      throw new Error(`连不上本机监控接口：${err.message}`);
    }
    let payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok) {
      const detail = payload && payload.error ? payload.error : `HTTP ${response.status}`;
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    if (!payload || typeof payload !== "object") throw new Error("接口返回了无法解析的内容");
    return payload;
  }

  function setLive(kind, text) {
    const dot = $("#liveDot");
    const label = $("#liveText");
    if (dot) dot.className = `live-dot is-${kind}`;
    if (label) label.textContent = text;
  }

  function emptyState(title, detail) {
    return el("div", { class: "monitor-empty" }, [
      el("strong", { text: title }),
      detail ? el("p", { text: detail }) : null,
    ]);
  }

  function errorState(error, retryLabel) {
    return el("div", { class: "monitor-empty is-error" }, [
      el("strong", { text: "读取失败" }),
      el("p", { text: show(error && error.message, "接口没有返回内容") }),
      el("p", { class: "monitor-muted", text: retryLabel || "监控是只读旁路，采集本身不受影响。" }),
    ]);
  }

  /* ------------------------------------------------------------------ 看板总览 */

  function statCard(title, value, sub, extra, caliber) {
    const children = [
      el("div", { class: "monitor-card-head" }, [
        el("h3", { text: title }),
        caliber ? el("button", { class: "monitor-help", type: "button", title: caliber, text: "口径" }) : null,
      ]),
      el("p", { class: `monitor-card-value${value === UNKNOWN ? " is-unknown" : ""}`, text: value }),
      sub ? el("p", { class: "monitor-card-sub", text: sub }) : null,
    ];
    if (extra) children.push(el("div", { class: "monitor-card-extra" }, extra));
    return el("div", { class: "panel monitor-card" }, children);
  }

  function renderOverview(payload) {
    const box = clear($("#overviewCards"));
    const caliber = payload.caliber || {};
    const last = payload.last_run;
    const rate = payload.success_rate || {};
    const flow = payload.item_flow || {};
    const sources = payload.sources || {};
    const success = payload.last_success || {};
    // 接口没返回这一整段时是「未知」：退化成 0 会被读成「确实没有严重错误」
    const issues = payload.open_errors || null;
    const stale = payload.stale_sources || null;
    const issueItems = issues ? (issues.items || []) : [];
    const staleItems = stale ? (stale.items || []) : [];
    const numberOrUnknown = (value) => (value === null || value === undefined ? UNKNOWN : num(value));

    box.appendChild(statCard(
      "最近一次任务",
      last ? show(last.status_label) : UNKNOWN,
      last ? `${last.task_label} · 结束于 ${fmtTime(last.finished_at)}` : "还没有任何任务运行记录",
      last ? [
        chip(`耗时 ${fmtDuration(last.duration_ms)}`, "muted"),
        last.error_count ? chip(`错误 ${last.error_count}`, "bad") : null,
        el("button", { class: "monitor-link-button", type: "button", text: "查看详情",
                       dataset: { openRun: last.run_id } }),
      ] : null,
      caliber.last_run));

    box.appendChild(statCard(
      "过去 24 小时任务成功率",
      rate.unknown || rate.value === null || rate.value === undefined
        ? UNKNOWN : `${(rate.value * 100).toFixed(1)}%`,
      rate.unknown
        ? show(rate.unknown_reason, "窗口内没有已结束的运行")
        : `成功 ${num(rate.ok)} / 共 ${num(rate.total)} 次（部分成功 ${num(rate.partial)} · 失败 ${num(rate.failed)}）`,
      rate.unknown ? [] : [
        chip(`含部分成功 ${rate.value_including_partial === null || rate.value_including_partial === undefined ? UNKNOWN : (rate.value_including_partial * 100).toFixed(1) + "%"}`, "muted"),
        chip(`窗口内运行 ${num(rate.runs_in_window)} 次`, "muted"),
      ],
      caliber.success_rate));

    const covered = flow.coverage || {};
    box.appendChild(statCard(
      "过去 24 小时条目流转",
      flow.unknown ? UNKNOWN : `新增 ${num(flow.new)} / 过滤 ${num(flow.filtered)}`,
      flow.unknown
        ? "窗口内没有任何带条目计数的运行"
        : `获取 ${num(flow.fetched)} · 更新 ${num(flow.updated)} · 去重 ${num(flow.duplicate)}；` +
          `有记录新增数的运行 ${num(covered.items_new)}/${num(flow.runs_in_window)}`,
      [
        chip(`新增 ${num(flow.new)}`, "ok"),
        chip(`更新 ${num(flow.updated)}`, "info"),
        chip(`去重 ${num(flow.duplicate)}`, "muted"),
        chip(`过滤 ${num(flow.filtered)}`, "warn"),
      ],
      caliber.item_flow));

    box.appendChild(statCard(
      "信息源状态",
      `${num(sources.ok)} 正常 / ${num(sources.error)} 失败`,
      `配置 ${num(sources.configured)} 个 · 启用 ${num(sources.enabled)} 个 · 暂停 ${num(sources.paused)}` +
        ` · 不可用 ${num(sources.unavailable)}`,
      [
        chip(`正常 ${num(sources.ok)}`, "ok"),
        chip(`失败 ${num(sources.error)}`, "bad"),
        chip(`暂停 ${num(sources.paused)}`, "warn"),
        chip(`不可用 ${num(sources.unavailable)}`, "muted"),
        chip(`从未成功 ${num(sources.never_succeeded)}`, "muted"),
      ],
      caliber.sources));

    box.appendChild(statCard(
      "最近一次成功采集",
      success.unknown ? UNKNOWN : fmtAge(success.age_minutes),
      success.unknown
        ? show(success.unknown_reason, "没有成功记录") +
          (success.source_last_ok_at ? `；来源表最近成功 ${fmtTime(success.source_last_ok_at)}` : "")
        : `${success.task_label} · ${fmtTime(success.at)}`,
      success.unknown ? [] : [
        chip(success.status_label || "成功", STATUS_KIND[success.status] || "ok"),
        success.run_id ? el("button", { class: "monitor-link-button", type: "button", text: "查看详情",
                                        dataset: { openRun: success.run_id } }) : null,
      ],
      caliber.last_success));

    box.appendChild(statCard(
      "未解决的严重错误",
      issues ? numberOrUnknown(issues.count) : UNKNOWN,
      issues
        ? (issues.count ? "最新一条日志仍是错误级别的来源 / 任务" : "当前没有未解决的严重错误")
        : "接口没有返回这一项，暂时无法判断",
      issueItems.slice(0, 3).map((item) => el("button", {
        class: "monitor-link-button", type: "button",
        text: `${show(item.source_id, item.task_label)} · ${show(item.error_code_label, item.error_code || "错误")}`,
        dataset: { openLog: item.id },
      })),
      caliber.errors));

    box.appendChild(statCard(
      "长期未更新来源",
      stale ? numberOrUnknown(stale.count) : UNKNOWN,
      stale
        ? `启用中但超过 ${show(stale.threshold_hours)} 小时没有成功`
        : "接口没有返回这一项，暂时无法判断",
      staleItems.slice(0, 3).map((item) => el("button", {
        class: "monitor-link-button", type: "button",
        text: `${show(item.source_id)} · ${item.last_ok_at ? fmtAge(item.last_ok_age_minutes) : "从未成功"}`,
        dataset: { openSource: item.source_id },
      })),
      caliber.errors));

    // 统计口径 + 需要处理的问题
    const calibBox = clear($("#overviewCaliber"));
    calibBox.appendChild(el("h3", { text: "统计口径" }));
    const list = el("dl", { class: "monitor-caliber-list" });
    Object.keys(caliber).forEach((key) => {
      list.appendChild(el("dt", { text: caliberTitle(key, payload) }));
      list.appendChild(el("dd", { text: caliber[key] }));
    });
    calibBox.appendChild(list);
    if (payload.degraded && payload.degraded.length) {
      calibBox.appendChild(el("p", { class: "monitor-muted",
        text: `降级：${payload.degraded.map((item) => item.module + " " + item.reason).join("；")}` }));
    }

    const issueBox = clear($("#overviewIssues"));
    issueBox.appendChild(el("h3", { text: "需要处理的问题" }));
    if (!issues && !stale) {
      issueBox.appendChild(emptyState("接口没有返回问题清单",
        "监控接口没有给出未解决的严重错误与长期未更新来源，这里只能显示未知。"));
    } else if (!issueItems.length && !staleItems.length) {
      issueBox.appendChild(emptyState("没有待处理的严重问题",
        "未解决的严重错误与长期未更新来源都会列在这里。"));
    } else {
      const table = el("table", { class: "monitor-table" });
      table.appendChild(el("thead", null, [el("tr", null, [
        el("th", { text: "类型" }), el("th", { text: "对象" }), el("th", { text: "时间" }),
        el("th", { text: "说明" }), el("th", { text: "操作" }),
      ])]));
      const body = el("tbody");
      issueItems.forEach((item) => {
        body.appendChild(el("tr", null, [
          el("td", null, [chip(item.level_label || "错误", "bad")]),
          el("td", null, [el("code", { class: "monitor-mono", text: show(item.source_id, item.task_label) })]),
          el("td", { class: "monitor-mono", text: `${fmtTime(item.ts)}（${fmtAge(item.age_minutes)}）` }),
          el("td", null, [
            item.error_code ? chip(item.error_code_label || item.error_code, "warn") : null,
            el("span", { text: ` ${show(item.message)}` }),
          ]),
          el("td", null, [el("button", { class: "monitor-link-button", type: "button", text: "看日志",
                                        dataset: { openLog: item.id } })]),
        ]));
      });
      staleItems.forEach((item) => {
        body.appendChild(el("tr", null, [
          el("td", null, [chip("长期未更新", "warn")]),
          el("td", null, [el("code", { class: "monitor-mono", text: show(item.source_id) })]),
          el("td", { class: "monitor-mono",
                     text: item.last_ok_at ? `${fmtTime(item.last_ok_at)}（${fmtAge(item.last_ok_age_minutes)}）` : "从未成功" }),
          el("td", { text: show(item.status_reason, `状态 ${item.status_label}`) }),
          el("td", null, [el("button", { class: "monitor-link-button", type: "button", text: "看来源",
                                        dataset: { openSource: item.source_id } })]),
        ]));
      });
      table.appendChild(body);
      issueBox.appendChild(el("div", { class: "monitor-table-wrap" }, [table]));
    }

    const updated = $("#overviewUpdated");
    const win = payload.window || {};
    if (updated) updated.textContent = `数据时间 ${fmtTime(payload.generated_at)}` +
      (win.hours === null || win.hours === undefined ? "" : ` · 窗口 ${num(win.hours)} 小时`);
  }

  function caliberTitle(key, payload) {
    const titles = {
      last_run: "最近一次任务结果与结束时间",
      success_rate: "过去 24 小时成功率",
      item_flow: "过去 24 小时新增 / 更新 / 去重 / 过滤",
      sources: "信息源状态与数量",
      last_success: "最近一次成功采集",
      errors: "未解决的严重错误与长期未更新来源",
    };
    return titles[key] || key;
  }

  /* ------------------------------------------------------------------ 时间线 */

  function renderTimeline(payload) {
    const box = clear($("#timelineBody"));
    const legend = $("#timelineLegend");
    const win = payload.window || {};
    if (legend) {
      legend.textContent = `窗口 ${fmtTime(win.from)} → ${fmtTime(win.to)}；` +
        `共 ${num(payload.total)} 次运行（运行中 ${num(payload.running)}）。条块从左到右表示开始时间，宽度表示耗时` +
        `（最短按 2 分钟显示，避免短任务看不见）。`;
    }
    if (!payload.total) {
      box.appendChild(emptyState("窗口内没有任务运行",
        "换一个时间范围，或等下一次调度跑起来。没有数据时不会用 0 或假数据填充。"));
      return;
    }

    const from = new Date(win.from).getTime();
    const to = new Date(win.to).getTime();
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      box.appendChild(errorState(new Error("时间线缺少有效的窗口起止时间"), "接口返回的窗口不完整。"));
      return;
    }
    const span = Math.max(1, to - from);
    const unknownStart = payload.runs.filter((run) => !run.started_at);

    // 时间刻度：先按跨度选一个「整」步长（整小时 / 整天），再对齐到本地整点，
    // 这样刻度落在人看得懂的钟点上，而不是窗口起点的等分点。
    function tickStep(spanMs) {
      const hour = 3600000;
      const targets = [hour, 2 * hour, 3 * hour, 6 * hour, 12 * hour, 24 * hour,
                       2 * 86400000, 7 * 86400000];
      for (let index = 0; index < targets.length; index += 1) {
        if (spanMs / targets[index] <= 8) return targets[index];
      }
      return targets[targets.length - 1];
    }

    function alignedTicks(fromMs, toMs, stepMs) {
      const out = [];
      // 对齐到本地时区的整点：先把时间戳退到当地当天 00:00，再按 step 往上加
      const start = new Date(fromMs);
      const midnight = new Date(start.getFullYear(), start.getMonth(), start.getDate()).getTime();
      let at = midnight;
      while (at < fromMs) at += stepMs;
      for (; at <= toMs && out.length < 20; at += stepMs) out.push(at);
      if (!out.length) out.push(fromMs, toMs);
      return out;
    }

    const axis = el("div", { class: "monitor-timeline-axis" });
    const step = tickStep(span);
    const ticks = alignedTicks(from, to, step);
    const longWindow = span > 36 * 3600000;
    ticks.forEach((at) => {
      const ratio = Math.max(0, Math.min(1, (at - from) / span));
      const stamp = new Date(at).toISOString();
      axis.appendChild(el("span", {
        class: "monitor-tick", style: `left:${(ratio * 100).toFixed(3)}%`,
        text: longWindow ? fmtDay(stamp) : `${fmtClock(stamp)}`,
        title: fmtTime(stamp),
      }));
    });
    axis.appendChild(el("span", { class: "monitor-tick is-start", style: "left:0%",
                                  text: longWindow ? fmtDay(new Date(from).toISOString())
                                                   : fmtClock(new Date(from).toISOString()) }));
    axis.appendChild(el("span", { class: "monitor-tick is-end", style: "left:100%",
                                  text: longWindow ? fmtDay(new Date(to).toISOString())
                                                   : fmtClock(new Date(to).toISOString()) }));
    box.appendChild(axis);

    const MIN_WIDTH_PCT = span > 0 ? Math.min(6, (120000 / span) * 100) : 2;
    // 七类任务全部保留一行：某个类型这一窗口没跑过，本身就是「覆盖缺口」的信息
    payload.groups.forEach((group) => {
      const lane = el("div", { class: `monitor-lane${group.count ? "" : " is-empty"}` });
      lane.appendChild(el("div", { class: "monitor-lane-label" }, [
        el("strong", { text: show(group.label, group.task) }),
        el("span", { class: "monitor-muted",
                     text: group.count ? `${num(group.count)} 次` : "该窗口内没有运行" }),
      ]));
      const track = el("div", { class: "monitor-lane-track" });
      const memberRuns = group.runs || [];
      memberRuns.forEach((run) => {
        if (!run.started_at) return;
        const start = new Date(run.started_at).getTime();
        const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
        const left = Math.max(0, Math.min(100, ((start - from) / span) * 100));
        const width = Math.max(MIN_WIDTH_PCT, Math.min(100 - left, ((end - start) / span) * 100));
        const bar = el("button", {
          class: `monitor-bar is-${STATUS_KIND[run.status] || "muted"}${run.status === "running" ? " is-running" : ""}`,
          type: "button",
          style: `left:${left.toFixed(3)}%;width:${width.toFixed(3)}%`,
          title: `${run.task_label} · ${run.status_label}\n开始 ${fmtTime(run.started_at)}\n` +
                 `结束 ${run.finished_at ? fmtTime(run.finished_at) : "（尚未结束）"}\n` +
                 `耗时 ${fmtDuration(run.duration_ms)} · 错误 ${run.error_count}`,
          dataset: { run: run.run_id, status: run.status },
          "aria-label": `${group.label} ${run.status_label} ${fmtTime(run.started_at)}`,
        }, []);
        track.appendChild(bar);
      });
      lane.appendChild(track);
      box.appendChild(lane);
    });

    const statusBar = el("div", { class: "monitor-timeline-legend" });
    Object.keys(payload.by_status || {}).forEach((status) => {
      statusBar.appendChild(statusChip(status, `${statusLabel(status)} ${num(payload.by_status[status])}`));
    });
    box.appendChild(statusBar);

    if (unknownStart.length) {
      box.appendChild(el("p", { class: "monitor-muted", text: payload.unknown_start_note ||
        `${unknownStart.length} 条历史运行没有开始时间，未画进时间轴。` }));
      const list = el("div", { class: "monitor-timeline-unknown" });
      unknownStart.slice(0, 8).forEach((run) => {
        list.appendChild(el("button", { class: "monitor-link-button", type: "button",
                                        text: `${run.task_label} · 结束于 ${fmtTime(run.finished_at)}`,
                                        dataset: { openRun: run.run_id } }));
      });
      box.appendChild(list);
    }
  }

  function statusLabel(status) {
    // /meta 拉不到时，时间线接口自己带的 statuses 同样能给出中文标签，不要退回原始码
    const lists = [state.meta && state.meta.statuses, state.timeline && state.timeline.statuses];
    for (let index = 0; index < lists.length; index += 1) {
      const found = (lists[index] || []).find((item) => item.value === status);
      if (found) return found.label;
    }
    return status;
  }

  /* ------------------------------------------------------------------ 运行记录 */

  function renderRuns() {
    const data = state.runs;
    const count = $("#runsCount");
    if (count) {
      count.textContent = data.total
        ? `共 ${data.total} 条（第 ${data.page} 页，每页 ${data.page_size} 条）`
        : "没有符合条件的运行记录";
    }
    const table = clear($("#runsTable"));
    table.appendChild(el("caption", { class: "monitor-caption",
      text: "点击任意一行查看该次运行的阶段、来源、条目与日志" }));
    const headers = ["开始时间", "结束时间", "任务类型", "触发方式", "总耗时", "状态",
                     "尝试来源", "获取条目", "新增", "更新", "重复", "过滤", "错误"];
    table.appendChild(el("thead", null, [el("tr", null,
      headers.map((label) => el("th", { text: label })))]));
    const body = el("tbody");
    if (!data.rows.length) {
      const cell = el("td", { colspan: String(headers.length) });
      cell.appendChild(emptyState("没有符合条件的运行记录",
        "放宽时间范围 / 清掉筛选条件再试；空结果是真实状态，不会用假数据填充。"));
      body.appendChild(el("tr", null, [cell]));
    }
    data.rows.forEach((run) => {
      const row = el("tr", {
        class: `monitor-row${run.status === "failed" ? " is-bad" : ""}${run.status === "running" ? " is-running" : ""}`,
        tabindex: "0", dataset: { run: run.run_id, status: run.status },
        "aria-label": `${run.task_label} ${run.status_label} 开始 ${fmtTime(run.started_at)}`,
      }, [
        el("td", { class: "monitor-mono", text: run.started_at ? fmtTime(run.started_at) : UNKNOWN }),
        el("td", { class: "monitor-mono", text: run.finished_at ? fmtTime(run.finished_at) : UNKNOWN }),
        el("td", null, [el("span", { text: show(run.task_label, run.task) })]),
        el("td", null, [chip(show(run.trigger_label, run.trigger || UNKNOWN), "muted", `trigger=${show(run.trigger, "?")}`)]),
        el("td", { class: "monitor-mono", text: fmtDuration(run.duration_ms) }),
        el("td", null, [statusChip(run.status, run.status_label)]),
        el("td", { class: "monitor-num", text: num(run.sources_attempted) }),
        el("td", { class: "monitor-num", text: num(run.items_fetched) }),
        el("td", { class: "monitor-num", text: num(run.items_new) }),
        el("td", { class: "monitor-num", text: num(run.items_updated) }),
        el("td", { class: "monitor-num", text: num(run.items_duplicate) }),
        el("td", { class: "monitor-num", text: num(run.items_filtered) }),
        el("td", { class: "monitor-num" + (run.error_count ? " is-bad" : ""), text: num(run.error_count) }),
      ]);
      body.appendChild(row);
    });
    table.appendChild(body);

    const pager = clear($("#runsPager"));
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    pager.appendChild(el("button", { class: "icon-btn", type: "button", text: "上一页",
                                     disabled: data.page <= 1, dataset: { runsPage: String(data.page - 1) } }));
    pager.appendChild(el("span", { class: "monitor-muted", text: `第 ${data.page} / ${pages} 页` }));
    pager.appendChild(el("button", { class: "icon-btn", type: "button", text: "下一页",
                                     disabled: data.page >= pages, dataset: { runsPage: String(data.page + 1) } }));
  }

  function runsParams(page) {
    return {
      window: $("#runsWindow").value,
      task: $("#runsTask").value,
      status: $("#runsStatus").value,
      trigger: $("#runsTrigger").value,
      q: $("#runsQuery").value.trim(),
      sort: $("#runsSort").value,
      order: $("#runsOrder").value,
      page: page || 1,
      page_size: $("#runsPageSize").value,
    };
  }

  async function loadRuns(page) {
    const params = runsParams(page);
    const payload = await apiGet("/runs", params);
    state.runs = { rows: payload.runs, total: payload.total, page: payload.page,
                   page_size: payload.page_size, sort: payload.sort, order: payload.order,
                   window: payload.window };
    renderRuns();
    return payload;
  }

  /* ------------------------------------------------------------------ 信息源 */

  function renderSources(payload) {
    const summary = payload.summary || {};
    const count = $("#sourcesCount");
    if (count) {
      count.textContent = `共 ${summary.total || 0} 个来源：正常 ${summary.ok || 0} · 失败 ${summary.error || 0}` +
        ` · 暂停 ${summary.paused || 0} · 可用但未启用 ${summary.disabled || 0}` +
        ` · 无合规入口 ${summary.unavailable || 0}`;
    }
    const concepts = clear($("#sourcesConcepts"));
    const coverage = summary.coverage || {};
    if (coverage.note) {
      [
        ["配置存在", coverage.configured], ["当前启用", coverage.enabled],
        ["最近成功过", coverage.recently_succeeded], ["本次有新数据", coverage.with_new_data],
      ].forEach(([label, value]) => {
        concepts.appendChild(el("div", { class: "monitor-concept" }, [
          el("strong", { text: num(value) }),
          el("span", { text: label }),
        ]));
      });
      concepts.appendChild(el("p", { class: "monitor-muted", text: coverage.note }));
    }

    const table = clear($("#sourcesTable"));
    table.appendChild(el("caption", { class: "monitor-caption",
      text: "「配置存在 / 当前启用 / 最近成功 / 具有新数据」是四个独立概念；返回 0 条算成功，不等于失败" }));
    const headers = ["来源", "类型", "状态", "最近尝试", "最近成功", "最近耗时",
                     "最近获取", "最近新增", "连续失败", "熔断 / 暂停原因", "最近一次错误"];
    table.appendChild(el("thead", null, [el("tr", null, headers.map((label) => el("th", { text: label })))]));
    const body = el("tbody");
    if (!payload.sources.length) {
      const cell = el("td", { colspan: String(headers.length) });
      cell.appendChild(emptyState("没有符合条件的信息源", "清掉筛选条件再试。"));
      body.appendChild(el("tr", null, [cell]));
    }
    payload.sources.forEach((row) => {
      const kinds = [];
      kinds.push(chip("配置存在", "muted"));
      kinds.push(chip(row.enabled ? "已启用" : "未启用", row.enabled ? "info" : "muted"));
      kinds.push(chip(row.never_succeeded ? "从未成功" : "成功过", row.never_succeeded ? "warn" : "ok"));
      if (row.has_new_data !== null && row.has_new_data !== undefined) {
        kinds.push(chip(row.has_new_data ? "有新数据" : "本轮 0 条新增", row.has_new_data ? "ok" : "muted"));
      }
      const tr = el("tr", {
        class: `monitor-row${row.status === "error" ? " is-bad" : ""}`,
        tabindex: "0", dataset: { source: row.source_id },
        "aria-label": `来源 ${row.name} 状态 ${row.status_label}`,
      }, [
        el("td", null, [
          el("strong", { text: show(row.name, row.source_id) }),
          el("span", { class: "monitor-mono monitor-muted", text: ` ${row.source_id}` }),
          el("div", { class: "monitor-chips" }, kinds),
        ]),
        el("td", null, [chip(show(row.kind), "muted"), el("span", { class: "monitor-muted", text: ` ${show(row.platform, "")}` })]),
        el("td", null, [statusChip(row.status, row.status_label)]),
        el("td", { class: "monitor-mono", text: row.last_attempt_at ? fmtTime(row.last_attempt_at) : UNKNOWN }),
        el("td", { class: "monitor-mono",
                   text: row.last_ok_at ? `${fmtTime(row.last_ok_at)}（${fmtAge(row.last_ok_age_minutes)}）` : "从未成功" }),
        el("td", { class: "monitor-mono", text: row.last_latency_ms === null || row.last_latency_ms === undefined ? UNKNOWN : fmtDuration(row.last_latency_ms) }),
        el("td", { class: "monitor-num", text: num(row.last_fetched) }),
        el("td", { class: "monitor-num", text: num(row.last_new) }),
        el("td", { class: "monitor-num" + (row.consecutive_failures ? " is-bad" : ""), text: num(row.consecutive_failures) }),
        el("td", { text: show(row.paused_reason, row.status === "paused" ? "已熔断暂停" : "—") }),
        el("td", null, [
          row.last_result_label ? chip(row.last_result_label, STATUS_KIND[row.last_result_status] || "muted") : null,
          row.last_error_code ? chip(row.last_error_code_label || row.last_error_code, "warn") : null,
          el("span", { class: "monitor-error-text", text: show(row.last_error, "—") }),
        ]),
      ]);
      body.appendChild(tr);
    });
    table.appendChild(body);
    state.sources = { rows: payload.sources, summary: payload.summary || {} };
  }

  function sourcesParams() {
    return {
      q: $("#sourcesQuery").value.trim(),
      kind: $("#sourcesKind").value,
      status: $("#sourcesStatus").value,
      sort: $("#sourcesSort").value,
      order: $("#sourcesOrder").value,
      enabled: $("#sourcesOnlyEnabled").checked ? "1" : "",
    };
  }

  async function loadSources() {
    const payload = await apiGet("/sources", sourcesParams());
    renderSources(payload);
    const kinds = (payload.summary && payload.summary.kinds) || [];
    if (kinds.length) fillSelect($("#sourcesKind"), kinds, "全部类型");
    return payload;
  }

  async function openSource(sourceId) {
    drawerOpener = document.activeElement;
    const box = $("#sourceDetail");
    box.hidden = false;
    clear(box).appendChild(el("p", { class: "pending", text: `正在读取来源 ${sourceId}…` }));
    try {
      const payload = await apiGet("/source", { id: sourceId });
      renderSourceDetail(payload);
      box.scrollIntoView({ block: "nearest" });
    } catch (err) {
      clear(box).appendChild(errorState(err));
    }
  }

  function renderSourceDetail(payload) {
    const box = clear($("#sourceDetail"));
    const row = payload.source || {};
    const head = el("div", { class: "monitor-section-head" }, [
      el("h3", { text: `来源详情 · ${show(row.name, payload.source_id)}` }),
      el("button", { class: "icon-btn", type: "button", text: "收起",
                     onclick: () => { box.hidden = true; } }),
    ]);
    box.appendChild(head);
    const kv = el("dl", { class: "monitor-kv" });
    [
      ["source_id", payload.source_id], ["平台", row.platform], ["类型", row.kind],
      ["状态", row.status_label], ["已启用", row.enabled ? "是" : "否"],
      ["最近尝试", row.last_attempt_at ? fmtTime(row.last_attempt_at) : UNKNOWN],
      ["最近成功", row.last_ok_at ? fmtTime(row.last_ok_at) : "从未成功"],
      ["连续失败", num(row.consecutive_failures)],
      ["熔断 / 暂停原因", show(row.paused_reason, "—")],
      ["最近一次错误", show(row.last_error, "—")],
      ["累计尝试 / 失败（监控记录）", `${num(row.attempts_recorded)} / ${num(row.failures_recorded)}`],
      ["成功但 0 条", num(row.empty_successes)],
    ].forEach(([label, value]) => {
      kv.appendChild(el("dt", { text: label }));
      kv.appendChild(el("dd", { text: show(value, "—") }));
    });
    box.appendChild(kv);

    box.appendChild(el("h4", { text: "最近的来源结果" }));
    const table = el("table", { class: "monitor-table" });
    table.appendChild(el("thead", null, [el("tr", null, ["时间", "结果", "耗时", "HTTP", "获取", "新增", "重复", "过滤", "原因 / 错误"]
      .map((label) => el("th", { text: label })))]));
    const body = el("tbody");
    if (!payload.results.length) {
      body.appendChild(el("tr", null, [el("td", { colspan: "9" }, [
        emptyState("没有这个来源的运行结果记录", "来源从未被采集过，或者监控记录已过期。")])]));
    }
    payload.results.forEach((item) => {
      body.appendChild(el("tr", null, [
        el("td", { class: "monitor-mono", text: fmtTime(item.finished_at || item.started_at) }),
        el("td", null, [statusChip(item.status, item.status_label)]),
        el("td", { class: "monitor-mono", text: fmtDuration(item.latency_ms) }),
        el("td", { class: "monitor-mono", text: num(item.http_status) }),
        el("td", { class: "monitor-num", text: num(item.fetched) }),
        el("td", { class: "monitor-num", text: num(item.items_new) }),
        el("td", { class: "monitor-num", text: num(item.items_duplicate) }),
        el("td", { class: "monitor-num", text: num(item.items_filtered) }),
        el("td", null, [
          item.reason ? el("div", { class: "monitor-muted", text: item.reason }) : null,
          item.error ? el("div", { class: "monitor-error-text", text: item.error }) : null,
          item.error_code ? chip(item.error_code, "warn") : null,
        ]),
      ]));
    });
    table.appendChild(body);
    box.appendChild(el("div", { class: "monitor-table-wrap" }, [table]));

    box.appendChild(el("h4", { text: "最近的来源日志" }));
    box.appendChild(renderLogList(payload.logs, { compact: true, emptyText: "这个来源没有日志记录" }));
  }

  /* ------------------------------------------------------------------ 日志 */

  function logsParams(extra) {
    return Object.assign({
      window: $("#logsWindow").value,
      level: $("#logsLevel").value,
      task: $("#logsTask").value,
      run_id: $("#logsRunId").value.trim(),
      source_id: $("#logsSourceId").value.trim(),
      stage: $("#logsStage").value,
      error_code: $("#logsErrorCode").value,
      q: $("#logsQuery").value.trim(),
      page: 1,
      page_size: 50,
    }, extra || {});
  }

  // 真正滚动的是 .monitor-logwrap（有 max-height + overflow-y），#logsList 只是它里面的内容；
  // 读 #logsList.scrollTop 永远是 0，滚动位置就会失效。
  function logsScroller() {
    const list = $("#logsList");
    if (!list) return null;
    return list.closest(".monitor-logwrap") || list.parentElement || list;
  }

  function renderLogList(logs, options) {
    const opts = options || {};
    const wrap = el("div", { class: "monitor-loglist" });
    if (!logs.length) {
      wrap.appendChild(emptyState(opts.emptyText || "没有符合条件的日志",
        "换时间范围或清掉筛选条件再试；空结果就是真的没有日志。"));
      return wrap;
    }
    logs.forEach((log) => wrap.appendChild(logRow(log)));
    return wrap;
  }

  function logRow(log) {
    const row = el("div", { class: `monitor-log is-${STATUS_KIND[log.level] || "muted"}`, tabindex: "0",
                             dataset: { log: String(log.id) }, role: "button",
                             "aria-expanded": "false",
                             "aria-label": `${log.level_label} ${fmtTime(log.ts)} ${log.message}` }, [
      el("span", { class: "monitor-mono monitor-log-time", text: fmtTime(log.ts) }),
      el("span", { class: "monitor-log-level", text: log.level_label || log.level }),
      el("span", { class: "monitor-log-stage", text: show(log.stage_label, "—") }),
      el("span", { class: "monitor-log-owner monitor-mono",
                   text: show(log.source_id, log.task_label || log.run_id || "—") }),
      el("span", { class: "monitor-log-msg", text: show(log.message_short || log.message) }),
    ]);
    return row;
  }

  async function expandLog(node, logId) {
    const expanded = node.getAttribute("aria-expanded") === "true";
    const existing = node.nextElementSibling;
    if (expanded && existing && existing.dataset.logDetail === String(logId)) {
      node.setAttribute("aria-expanded", "false");
      existing.remove();
      return;
    }
    node.setAttribute("aria-expanded", "true");
    const detail = el("div", { class: "monitor-log-detail", dataset: { logDetail: String(logId) } });
    detail.appendChild(el("p", { class: "pending", text: "正在读取日志详情…" }));
    node.after(detail);
    try {
      const payload = await apiGet("/log", { id: logId });
      const log = payload.log;
      clear(detail);
      detail.appendChild(el("div", { class: "monitor-chips" }, [
        chip(log.level_label || log.level, STATUS_KIND[log.level] || "muted"),
        log.stage ? chip(`阶段 ${log.stage_label || log.stage}`, "muted") : null,
        log.task ? chip(`任务 ${log.task_label || log.task}`, "muted") : null,
        log.source_id ? chip(`来源 ${log.source_id}`, "muted") : null,
        log.error_code ? chip(log.error_code_label || log.error_code, "warn") : null,
      ]));
      const kv = el("dl", { class: "monitor-kv" });
      [["时间", fmtTime(log.ts)], ["run_id", show(log.run_id, "—")],
       ["来源", show(log.source_id, "—")], ["阶段", show(log.stage_label, "—")],
       ["错误码", log.error_code ? `${log.error_code}（${show(log.error_code_label, "")}）` : "—"]]
        .forEach(([label, value]) => {
          kv.appendChild(el("dt", { text: label }));
          kv.appendChild(el("dd", { text: value }));
        });
      detail.appendChild(kv);
      detail.appendChild(el("pre", { class: "monitor-pre", text: log.message }));
      detail.appendChild(el("p", { class: "monitor-muted",
        text: "details_json（已脱敏；接口在写入和出参两处都做过密钥替换，前端不会还原原文）" }));
      detail.appendChild(el("pre", { class: "monitor-pre", text: log.details_json || "{}" }));
      detail.appendChild(el("div", { class: "monitor-filter-actions" }, [
        el("button", { class: "icon-btn", type: "button", text: "复制脱敏详情",
                       onclick: () => copyLog(detail) }),
        el("button", { class: "icon-btn", type: "button", text: "收起",
                       onclick: () => { node.setAttribute("aria-expanded", "false"); detail.remove(); } }),
      ]));
    } catch (err) {
      clear(detail).appendChild(errorState(err, "日志详情读取失败；列表本身仍然可用。"));
    }
  }

  async function copyLog(detail) {
    const blocks = $$(".monitor-pre", detail).map((node) => node.textContent);
    const text = blocks.join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制（脱敏后的日志详情）");
    } catch (err) {
      // 剪贴板权限被拒时退回到“选中文本”的做法，不额外写入系统剪贴板历史
      const range = document.createRange();
      range.selectNodeContents(detail);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast("无法直接写剪贴板，已为你选中文本，按 Ctrl+C 复制（同样是脱敏内容）");
    }
  }

  function toast(message) {
    const box = $("#toasts");
    if (!box) return;
    const node = el("div", { class: "toast", text: message });
    box.appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  function renderLogCounts(payload) {
    const box = clear($("#logsCounts"));
    const counts = payload.counts_by_level;
    ["ERROR", "WARNING", "INFO", "DEBUG"].forEach((level) => {
      const value = counts ? (counts[level] || 0) : null;
      box.appendChild(chip(`${level} ${value === null ? UNKNOWN : num(value)}`,
                           STATUS_KIND[level] || "muted"));
    });
    box.appendChild(el("span", { class: "monitor-muted", text: `共 ${num(payload.total)} 条符合筛选` }));
  }

  function renderLogs(payload, options) {
    const opts = options || {};
    const list = $("#logsList");
    const scroller = logsScroller();
    if (opts.append) {
      // 「加载更早」：把更旧的一页接在列表底部，当前阅读位置不动
      if (!payload.logs.length) return;
      const container = list ? list.firstElementChild : null;
      if (container && container.classList && container.classList.contains("monitor-loglist")) {
        payload.logs.forEach((log) => container.appendChild(logRow(log)));
      } else {
        clear(list).appendChild(renderLogList(payload.logs));
      }
      state.logs.rows = (state.logs.rows || []).concat(payload.logs);
      state.logs.total = payload.total;
      state.logs.page = payload.page;
      state.logs.page_size = payload.page_size;
      state.logs.has_more = payload.has_more;
      state.logs.loaded = state.logs.rows.length;
    } else {
      clear(list).appendChild(renderLogList(payload.logs));
      if (scroller) scroller.scrollTop = 0;
      state.logs = { rows: payload.logs, total: payload.total, page: payload.page,
                     page_size: payload.page_size, has_more: payload.has_more,
                     window: payload.window, loaded: payload.logs.length };
    }
    renderLogCounts(payload);
    const pager = clear($("#logsPager"));
    const loaded = (state.logs.rows || []).length;
    pager.appendChild(el("span", { class: "monitor-muted",
                                   text: `已加载 ${num(loaded)} / ${num(payload.total)} 条 · 每页 ${num(payload.page_size)} 条` }));
    pager.appendChild(el("button", { class: "icon-btn", type: "button", text: "加载更早",
                                     disabled: !payload.has_more,
                                     dataset: { logsPage: String(payload.page + 1), logsAppend: "1" } }));
    const reset = el("button", { class: "icon-btn", type: "button", text: "回到最新",
                                 dataset: { logsPage: "1", logsAppend: "0" } });
    if (loaded <= payload.page_size && payload.page <= 1) reset.setAttribute("disabled", "");
    pager.appendChild(reset);
  }

  async function loadLogs(page, options) {
    const payload = await apiGet("/logs", logsParams({ page: page || 1 }));
    renderLogs(payload, options);
    return payload;
  }

  /** 自动刷新：只增量取比当前最新 id 更新的日志，并保持滚动位置。 */
  async function pollLogs() {
    if (document.hidden) return;
    const rows = state.logs.rows || [];
    const newest = rows.length ? Math.max(...rows.map((row) => row.id)) : 0;
    if (!newest) { await loadLogs(1); return; }
    const scroller = logsScroller();
    const anchor = scroller ? scroller.scrollTop : 0;
    const beforeHeight = scroller ? scroller.scrollHeight : 0;
    const payload = await apiGet("/logs", logsParams({ since_id: newest, page_size: 50 }));
    if (!payload.logs.length) return;
    clear($("#logsCounts"));
    renderLogCounts(payload);
    const fresh = payload.logs.filter((log) => !(state.logs.rows || []).some((row) => row.id === log.id));
    if (!fresh.length) return;
    state.logs.rows = fresh.concat(state.logs.rows);
    const list2 = $("#logsList");
    const container = list2.firstElementChild;
    if (container) {
      fresh.slice().reverse().forEach((log) => container.prepend(logRow(log)));
    } else {
      clear(list2).appendChild(renderLogList(fresh));
    }
    if (scroller) {
      const added = scroller.scrollHeight - beforeHeight;
      // 只有当用户本来就在顶部附近时才跟随新日志；否则保持原来的阅读位置
      scroller.scrollTop = anchor <= 8 ? 0 : anchor + added;
    }
  }

  /* ------------------------------------------------------------------ 详情抽屉 */

  async function openRun(runId) {
    const drawer = $("#runDrawer");
    const backdrop = $("#drawerBackdrop");
    // 键盘 Enter/Space 与鼠标点击都会先把焦点落在被激活的行/按钮上
    drawerOpener = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    requestAnimationFrame(() => { drawer.classList.add("open"); backdrop.classList.add("open"); });
    $("#drawerTitle").textContent = "任务详情";
    $("#drawerSubtitle").textContent = `run_id=${runId}`;
    showDrawerTab("overview");
    clear($("#drawerOverview")).appendChild(el("p", { class: "pending", text: "正在读取运行详情…" }));
    ["drawerStages", "drawerSources", "drawerItems", "drawerLogs"].forEach((id) => clear($("#" + id)));
    try {
      const payload = await apiGet("/run", { id: runId });
      state.drawer = { runId, data: payload,
                       items: { page: payload.items.page, action: "", q: "" },
                       logs: { page: payload.logs.page, level: "" },
                       source: null };
      renderDrawerOverview(payload);
      renderDrawerStages(payload);
      renderDrawerSources(payload);
      renderDrawerItems(payload.items);
      renderDrawerLogs(payload.logs);
      $("#drawerTitle").textContent = `${payload.task_label} · ${payload.status_label}`;
      $("#drawerSubtitle").textContent =
        `${runId} · 触发方式 ${payload.trigger_label} · 开始 ${fmtTime(payload.run.started_at)}` +
        ` · 结束 ${payload.run.finished_at ? fmtTime(payload.run.finished_at) : "（尚未结束）"}`;
      const focus = $("#drawerClose");
      if (focus) focus.focus();
    } catch (err) {
      clear($("#drawerOverview")).appendChild(errorState(err, "这条运行记录可能已被清理（默认保留 45 天）。"));
    }
  }

  function closeDrawer() {
    const drawer = $("#runDrawer");
    const backdrop = $("#drawerBackdrop");
    const opener = drawerOpener;
    drawer.classList.remove("open");
    backdrop.classList.remove("open");
    // 与 styles.css 里抽屉的 --fb-dur-slow（260ms）保持一致，否则动画会被硬切断
    setTimeout(() => { drawer.hidden = true; backdrop.hidden = true; }, 260);
    state.drawer = null;
    // 键盘用户关掉抽屉后要回到触发它的那一行，而不是掉回文档开头
    if (opener && typeof opener.focus === "function" && opener.isConnected) opener.focus();
  }

  function showDrawerTab(tab) {
    const panels = { overview: "#drawerOverview", stages: "#drawerStages",
                     sources: "#drawerSources", items: "#drawerItems", logs: "#drawerLogs" };
    Object.keys(panels).forEach((key) => { $("#" + panels[key].slice(1)).hidden = key !== tab; });
    $$("#drawerTabs [data-drawer-tab]").forEach((button) => {
      const active = button.dataset.drawerTab === tab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
      // roving tabindex：键盘上只有当前页签在 Tab 序列里，左右键在页签之间移动
      button.setAttribute("tabindex", active ? "0" : "-1");
    });
  }

  /** 页签的左右方向键：符合 role="tablist" 的键盘约定。 */
  function moveDrawerTab(step) {
    const buttons = $$("#drawerTabs [data-drawer-tab]");
    if (!buttons.length) return;
    const current = buttons.findIndex((button) => button.getAttribute("aria-selected") === "true");
    const next = (current + step + buttons.length) % buttons.length;
    showDrawerTab(buttons[next].dataset.drawerTab);
    buttons[next].focus();
  }

  function kvList(pairs) {
    const kv = el("dl", { class: "monitor-kv" });
    pairs.forEach(([label, value]) => {
      kv.appendChild(el("dt", { text: label }));
      kv.appendChild(el("dd", { text: value === null || value === undefined ? UNKNOWN : String(value) }));
    });
    return kv;
  }

  function renderDrawerOverview(payload) {
    const box = clear($("#drawerOverview"));
    const run = payload.run;
    box.appendChild(kvList([
      ["任务类型", `${run.task_label}（${run.task}）`],
      ["run_id", run.run_id],
      ["触发方式", `${run.trigger_label}（${run.trigger}）`],
      ["开始时间", run.started_at ? fmtTime(run.started_at) : "未知（历史记录没有开始时间）"],
      ["结束时间", run.finished_at ? fmtTime(run.finished_at) : "（尚未结束）"],
      ["总耗时", fmtDuration(run.duration_ms)],
      ["状态", `${run.status_label}（${run.status}）`],
      ["记录来源", run.origin === "legacy" ? "历史导入（旧记录投影）" : "实时埋点"],
      ["尝试来源数", num(run.sources_attempted)],
      ["获取条目", num(run.items_fetched)],
      ["新增 / 更新 / 重复 / 过滤",
        `${num(run.items_new)} / ${num(run.items_updated)} / ${num(run.items_duplicate)} / ${num(run.items_filtered)}`],
      ["错误数量", num(run.error_count)],
    ]));
    if (run.errors && run.errors.length) {
      box.appendChild(el("h4", { text: "错误记录" }));
      const list = el("ul", { class: "monitor-error-list" });
      run.errors.forEach((item) => {
        list.appendChild(el("li", null, [
          chip(item.error_code || "error", "bad"),
          el("span", { class: "monitor-mono", text: ` ${item.source_id ? item.source_id + " · " : ""}${fmtTime(item.at)}` }),
          el("div", { class: "monitor-error-text", text: item.message }),
        ]));
      });
      box.appendChild(list);
    }
    const stats = run.stats || {};
    const statKeys = Object.keys(stats);
    if (statKeys.length) {
      box.appendChild(el("h4", { text: "该次运行的统计字段" }));
      box.appendChild(el("pre", { class: "monitor-pre", text: JSON.stringify(stats, null, 2) }));
    }
    if ((run.stats || {}).counts_unknown && run.stats.counts_unknown.length) {
      box.appendChild(el("p", { class: "monitor-muted",
        text: `以下计数这次没有记录到，页面按「未知」显示：${run.stats.counts_unknown.join("、")}` }));
    }
  }

  function renderDrawerStages(payload) {
    const box = clear($("#drawerStages"));
    box.appendChild(el("p", { class: "monitor-muted",
      text: "按真实执行顺序列出该任务的阶段；没有执行的阶段标为「已跳过 / 不适用 / 未知」，不会显示成成功。" }));
    const list = el("ol", { class: "monitor-stages" });
    // 序号按返回顺序（= 任务的真实阶段顺序）编号：历史导入行的 seq 来自旧记录，
    // 直接显示会出现「1 2 3 4 2 3 7 8 4 5」这种看起来像乱序的编号。
    payload.stages.forEach((stage, index) => {
      const detail = [el("span", { class: "monitor-mono", text:
        `${stage.started_at ? fmtTime(stage.started_at) : "开始时间未记录"} → ` +
        `${stage.finished_at ? fmtTime(stage.finished_at) : "未结束"} · 耗时 ${fmtDuration(stage.duration_ms)}` })];
      if (stage.result && stage.result.note) {
        detail.push(el("div", { class: "monitor-muted", text: stage.result.note }));
      }
      if (stage.error) detail.push(el("div", { class: "monitor-error-text", text: stage.error }));
      const resultKeys = Object.keys(stage.result || {}).filter((key) => key !== "note");
      if (resultKeys.length) {
        detail.push(el("div", { class: "monitor-chips" }, resultKeys.map((key) => {
          const value = stage.result[key];
          if (value === null || value === undefined || typeof value === "object") return null;
          return chip(`${key}=${value}`, "muted");
        }).filter(Boolean)));
      }
      list.appendChild(el("li", { class: `monitor-stage is-${STATUS_KIND[stage.status] || "muted"}` }, [
        el("div", { class: "monitor-stage-head" }, [
          el("strong", { text: `${index + 1}. ${show(stage.label, stage.name)}`,
                       title: `阶段 ${show(stage.name)} · 记录序号 ${show(stage.seq)}` }),
          statusChip(stage.status, stage.status_label),
        ]),
        el("div", { class: "monitor-stage-body" }, detail),
      ]));
    });
    box.appendChild(list);
  }

  function renderDrawerSources(payload) {
    const box = clear($("#drawerSources"));
    if (!payload.sources.length) {
      box.appendChild(emptyState("本次运行没有来源结果",
        "例如只做推送或摘要的任务本来就没有抓取阶段。"));
      return;
    }
    const table = el("table", { class: "monitor-table" });
    table.appendChild(el("thead", null, [el("tr", null,
      ["来源", "结果", "耗时", "HTTP", "获取", "解析", "新增", "重复", "过滤", "原因 / 错误"]
        .map((label) => el("th", { text: label })))]));
    const body = el("tbody");
    payload.sources.forEach((source) => {
      const row = el("tr", { class: "monitor-row", tabindex: "0", dataset: { drawerSource: source.source_id },
                             "aria-label": `来源 ${show(source.name, source.source_id)} ${source.status_label}` }, [
        el("td", null, [el("strong", { text: show(source.name, source.source_id) }),
                        el("div", { class: "monitor-mono monitor-muted", text: source.source_id })]),
        el("td", null, [statusChip(source.status, source.status_label)]),
        el("td", { class: "monitor-mono", text: fmtDuration(source.latency_ms) }),
        el("td", { class: "monitor-mono", text: num(source.http_status) }),
        el("td", { class: "monitor-num", text: num(source.fetched) }),
        el("td", { class: "monitor-num", text: num(source.parsed) }),
        el("td", { class: "monitor-num", text: num(source.items_new) }),
        el("td", { class: "monitor-num", text: num(source.items_duplicate) }),
        el("td", { class: "monitor-num", text: num(source.items_filtered) }),
        el("td", null, [
          source.reason ? el("div", { class: "monitor-muted", text: source.reason }) : null,
          source.error ? el("div", { class: "monitor-error-text", text: source.error }) : null,
          source.error_code ? chip(source.error_code, "warn") : null,
        ]),
      ]);
      body.appendChild(row);
    });
    table.appendChild(body);
    box.appendChild(el("div", { class: "monitor-table-wrap" }, [table]));
    box.appendChild(el("div", { id: "drawerSourceDetail", class: "monitor-source-inline" }));
  }

  function renderDrawerItems(items) {
    const box = clear($("#drawerItems"));
    state.drawer.items = state.drawer.items || { page: 1, action: "", q: "" };
    const toolbar = el("div", { class: "monitor-inline-tools" }, [
      el("label", { class: "monitor-inline-field" }, [
        "动作 ",
        el("select", { id: "drawerItemAction", onchange: () => reloadDrawerItems(1) },
          [el("option", { value: "", text: "全部" })].concat(
            Object.keys(items.summary && items.summary.by_action || {}).map((action) =>
              el("option", { value: action, text: `${action}（${items.summary.by_action[action]}）` })))),
      ]),
      el("label", { class: "monitor-inline-field" }, [
        "搜索 ",
        el("input", { type: "search", id: "drawerItemQuery", placeholder: "标题 / 链接 / 原因",
                      onkeydown: (event) => { if (event.key === "Enter") reloadDrawerItems(1); } }),
      ]),
      el("button", { class: "icon-btn", type: "button", text: "查询", onclick: () => reloadDrawerItems(1) }),
      el("span", { class: "monitor-muted", text: `共 ${num(items.total)} 条（${fmtActions(items.summary)}）` }),
    ]);
    box.appendChild(toolbar);
    const controls = $("#drawerItemAction");
    if (controls) controls.value = state.drawer.items.action || "";
    const query = $("#drawerItemQuery");
    if (query) query.value = state.drawer.items.q || "";

    const table = el("table", { class: "monitor-table monitor-table-wide" });
    table.appendChild(el("thead", null, [el("tr", null,
      ["标题", "来源", "发布时间", "采集时间", "处理动作", "原因", "item_id", "event_id", "story_id", "仍在雷达"]
        .map((label) => el("th", { text: label })))]));
    const body = el("tbody");
    if (!items.items.length) {
      body.appendChild(el("tr", null, [el("td", { colspan: "10" }, [
        emptyState("这次运行没有逐条处理记录",
          "历史导入的运行只保留了合计值；新产生的运行会记录每条的新增 / 重复 / 过滤原因。")])]));
    }
    items.items.forEach((item) => {
      // 单条坏数据（null / 非对象）不能让整张表报错，也不能让详情面板显示「记录已被清理」
      if (!item || typeof item !== "object") {
        body.appendChild(el("tr", null, [el("td", { colspan: "10", class: "monitor-muted",
                                                    text: "（这条记录的内容无法解析，已跳过渲染）" })]));
        return;
      }
      body.appendChild(el("tr", { class: item.action === "filtered" ? "monitor-row" : "" }, [
        el("td", null, [item.title ? el("span", { text: item.title }) : el("span", { class: "monitor-muted", text: "（标题缺失）" }),
                        item.link ? el("div", null, [linkOrText(item.link, "打开原文")]) : null]),
        el("td", { class: "monitor-mono", text: show(item.source_id, "—") }),
        el("td", { class: "monitor-mono", text: item.published_at ? fmtTime(item.published_at) : UNKNOWN }),
        el("td", { class: "monitor-mono", text: item.collected_at ? fmtTime(item.collected_at) : UNKNOWN }),
        el("td", null, [chip(show(item.action_label || item.action, UNKNOWN), STATUS_KIND[item.action] || "muted")]),
        el("td", { text: show(item.reason, "—") }),
        el("td", { class: "monitor-mono", text: show(item.item_id, UNKNOWN) }),
        el("td", { class: "monitor-mono", text: show(item.event_id, "—") }),
        el("td", { class: "monitor-mono", text: show(item.story_id, "—") }),
        el("td", null, [item.in_radar === null || item.in_radar === undefined
          ? chip(item.kind === "paper" ? "不适用（论文）" : UNKNOWN, "muted")
          : chip(item.in_radar ? "可以找到" : "不在雷达库", item.in_radar ? "ok" : "muted")]),
      ]));
    });
    table.appendChild(body);
    box.appendChild(el("div", { class: "monitor-table-wrap" }, [table]));
    const pager = el("div", { class: "monitor-pager" }, [
      el("button", { class: "icon-btn", type: "button", text: "上一页",
                     disabled: items.page <= 1, onclick: () => reloadDrawerItems(items.page - 1) }),
      el("span", { class: "monitor-muted", text: `第 ${items.page} 页 · 每页 ${items.page_size} 条` }),
      el("button", { class: "icon-btn", type: "button", text: "下一页",
                     disabled: !items.has_more, onclick: () => reloadDrawerItems(items.page + 1) }),
    ]);
    box.appendChild(pager);
  }

  function fmtActions(summary) {
    const actions = (summary && summary.by_action) || {};
    const keys = Object.keys(actions);
    if (!keys.length) return "没有分类计数";
    return keys.map((key) => `${key} ${actions[key]}`).join(" · ");
  }

  async function reloadDrawerItems(page) {
    if (!state.drawer) return;
    const action = $("#drawerItemAction") ? $("#drawerItemAction").value : "";
    const q = $("#drawerItemQuery") ? $("#drawerItemQuery").value.trim() : "";
    state.drawer.items = { page: page || 1, action, q };
    const payload = await apiGet("/run/items", { id: state.drawer.runId, page: page || 1,
                                                page_size: 50, action, q });
    renderDrawerItems(payload);
  }

  function renderDrawerLogs(payload) {
    const box = clear($("#drawerLogs"));
    const toolbar = el("div", { class: "monitor-inline-tools" }, [
      el("label", { class: "monitor-inline-field" }, [
        "等级 ",
        el("select", { id: "drawerLogLevel", onchange: () => reloadDrawerLogs(1) },
          [el("option", { value: "", text: "全部" })].concat(
            (state.meta && state.meta.levels || ["DEBUG", "INFO", "WARNING", "ERROR"]).map((level) =>
              el("option", { value: level, text: level })))),
      ]),
      el("span", { class: "monitor-muted", text: `共 ${payload.total} 条` }),
    ]);
    box.appendChild(toolbar);
    const level = $("#drawerLogLevel");
    if (level) level.value = (state.drawer.logs && state.drawer.logs.level) || "";
    box.appendChild(renderLogList(payload.logs, { emptyText: "这次运行没有日志记录" }));
    box.appendChild(el("div", { class: "monitor-pager" }, [
      el("button", { class: "icon-btn", type: "button", text: "上一页",
                     disabled: payload.page <= 1, onclick: () => reloadDrawerLogs(payload.page - 1) }),
      el("span", { class: "monitor-muted", text: `第 ${payload.page} 页 · 每页 ${payload.page_size} 条` }),
      el("button", { class: "icon-btn", type: "button", text: "加载更早",
                     disabled: !payload.has_more, onclick: () => reloadDrawerLogs(payload.page + 1) }),
    ]));
  }

  async function reloadDrawerLogs(page) {
    if (!state.drawer) return;
    const level = $("#drawerLogLevel") ? $("#drawerLogLevel").value : "";
    state.drawer.logs = { page: page || 1, level };
    const payload = await apiGet("/run/logs", { id: state.drawer.runId, page: page || 1,
                                               page_size: 25, level });
    renderDrawerLogs(payload);
  }

  async function openDrawerSource(sourceId) {
    if (!state.drawer) return;
    const box = $("#drawerSourceDetail");
    if (!box) return;
    clear(box).appendChild(el("p", { class: "pending", text: `正在读取来源 ${sourceId} 本次的明细…` }));
    try {
      const [items, logs] = await Promise.all([
        apiGet("/run/items", { id: state.drawer.runId, source_id: sourceId, page_size: 50 }),
        apiGet("/run/logs", { id: state.drawer.runId, source_id: sourceId, page_size: 20 }),
      ]);
      clear(box);
      box.appendChild(el("div", { class: "monitor-section-head" }, [
        el("h4", { text: `来源 ${sourceId}：本次候选条目与来源日志` }),
        el("button", { class: "icon-btn", type: "button", text: "收起",
                       onclick: () => clear(box) }),
      ]));
      box.appendChild(el("p", { class: "monitor-muted",
        text: `本次记录到 ${num(items.total)} 条处理结果（含未入库的过滤 / 重复条目及原因）。` }));
      const table = el("table", { class: "monitor-table" });
      table.appendChild(el("thead", null, [el("tr", null,
        ["标题", "处理动作", "原因", "item_id", "仍在雷达"].map((label) => el("th", { text: label })))]));
      const body = el("tbody");
      (items.items.length ? items.items : [null]).forEach((item) => {
        if (!item) {
          body.appendChild(el("tr", null, [el("td", { colspan: "5" }, [
            emptyState("这次运行没有记录这个来源的逐条结果", "可能是历史导入的运行，或该来源本轮没有返回条目。")])]));
          return;
        }
        body.appendChild(el("tr", null, [
          el("td", null, [item.title ? el("span", { text: item.title }) : el("span", { class: "monitor-muted", text: "（标题缺失）" }),
                          item.link ? el("div", null, [linkOrText(item.link, "打开原文")]) : null]),
          el("td", null, [chip(show(item.action_label || item.action, UNKNOWN), STATUS_KIND[item.action] || "muted")]),
          el("td", { text: show(item.reason, "—") }),
          el("td", { class: "monitor-mono", text: show(item.item_id, UNKNOWN) }),
          el("td", null, [item.in_radar === null || item.in_radar === undefined ? chip(UNKNOWN, "muted")
            : chip(item.in_radar ? "可以找到" : "不在雷达库", item.in_radar ? "ok" : "muted")]),
        ]));
      });
      table.appendChild(body);
      box.appendChild(el("div", { class: "monitor-table-wrap" }, [table]));
      box.appendChild(el("h4", { text: "来源日志" }));
      box.appendChild(renderLogList(logs.logs, { emptyText: "这个来源在本次运行里没有日志" }));
      box.scrollIntoView({ block: "nearest" });
    } catch (err) {
      clear(box).appendChild(errorState(err));
    }
  }

  /* ------------------------------------------------------------------ 事件绑定 */

  function fillSelect(select, items, placeholder) {
    if (!select) return;
    const current = select.value;
    clear(select);
    if (placeholder) select.appendChild(el("option", { value: "", text: placeholder }));
    (items || []).forEach((item) => {
      const value = typeof item === "string" ? item : item.value;
      const label = typeof item === "string" ? item : item.label;
      select.appendChild(el("option", { value, text: label }));
    });
    if (current) select.value = current;
  }

  function applyMeta(meta) {
    state.meta = meta;
    fillSelect($("#runsTask"), meta.tasks, "全部任务");
    fillSelect($("#runsStatus"), meta.statuses, "全部状态");
    fillSelect($("#runsTrigger"), meta.triggers, "全部");
    fillSelect($("#sourcesStatus"), meta.source_statuses, "全部状态");
    fillSelect($("#logsLevel"), (meta.levels || []).map((level) => ({ value: level, label: level })), "全部等级");
    fillSelect($("#logsTask"), meta.tasks, "全部任务");
    fillSelect($("#logsStage"), meta.stages, "全部阶段");
    fillSelect($("#logsErrorCode"), (meta.live_log_error_codes || []).map((item) => ({ value: item.code, label: `${item.label}（${item.count}）` })), "全部错误码");
    const kinds = (state.sources.summary && state.sources.summary.kinds) || [];
    if (kinds.length) fillSelect($("#sourcesKind"), kinds, "全部类型");
  }

  function bind() {
    $("#refreshBtn").addEventListener("click", () => reloadAll(true));
    $("#timelineRefresh").addEventListener("click", () => loadTimeline());
    $("#timelineWindow").addEventListener("change", () => loadTimeline());
    $("#runsApply").addEventListener("click", () => loadRuns(1));
    $("#runsReset").addEventListener("click", () => {
      $("#runsWindow").value = "24h"; $("#runsTask").value = ""; $("#runsStatus").value = "";
      $("#runsTrigger").value = ""; $("#runsQuery").value = ""; $("#runsSort").value = "started_at";
      $("#runsOrder").value = "desc"; $("#runsPageSize").value = "25";
      loadRuns(1);
    });
    $("#runsQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") loadRuns(1); });
    ["runsWindow", "runsTask", "runsStatus", "runsTrigger", "runsSort", "runsOrder", "runsPageSize"]
      .forEach((id) => $("#" + id).addEventListener("change", () => loadRuns(1)));

    $("#sourcesApply").addEventListener("click", () => loadSources());
    $("#sourcesReset").addEventListener("click", () => {
      $("#sourcesQuery").value = ""; $("#sourcesKind").value = ""; $("#sourcesStatus").value = "";
      $("#sourcesSort").value = "failures"; $("#sourcesOrder").value = "desc";
      $("#sourcesOnlyEnabled").checked = false;
      loadSources();
    });
    $("#sourcesQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") loadSources(); });
    ["sourcesKind", "sourcesStatus", "sourcesSort", "sourcesOrder", "sourcesOnlyEnabled"]
      .forEach((id) => $("#" + id).addEventListener("change", () => loadSources()));

    $("#logsApply").addEventListener("click", () => loadLogs(1));
    $("#logsReset").addEventListener("click", () => {
      $("#logsWindow").value = "7d"; $("#logsLevel").value = ""; $("#logsTask").value = "";
      $("#logsRunId").value = ""; $("#logsSourceId").value = ""; $("#logsStage").value = "";
      $("#logsErrorCode").value = ""; $("#logsQuery").value = "";
      loadLogs(1);
    });
    $("#logsQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") loadLogs(1); });
    ["logsWindow", "logsLevel", "logsTask", "logsStage", "logsErrorCode", "logsRunId", "logsSourceId"]
      .forEach((id) => $("#" + id).addEventListener("change", () => loadLogs(1)));
    $("#logsAuto").addEventListener("change", () => {
      $("#logsAutoState").textContent = $("#logsAuto").checked
        ? `每 ${$("#logsInterval").value} 秒增量刷新；页面不可见时暂停`
        : "自动刷新关闭（只在你点时读取）";
      restartLogPoll();
    });
    $("#logsInterval").addEventListener("change", () => { if ($("#logsAuto").checked) restartLogPoll(); });

    $("#drawerClose").addEventListener("click", closeDrawer);
    $("#drawerBackdrop").addEventListener("click", closeDrawer);
    document.addEventListener("fb:escape", closeDrawer);
    $("#drawerTabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-drawer-tab]");
      if (button) showDrawerTab(button.dataset.drawerTab);
    });
    $("#drawerTabs").addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight") { event.preventDefault(); moveDrawerTab(1); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); moveDrawerTab(-1); }
    });

    $("#sectionNav").addEventListener("click", (event) => {
      const button = event.target.closest("[data-section]");
      if (!button) return;
      setSection(button.dataset.section);
    });

    // 表格 / 日志 / 概念按钮：统一用事件委派，避免给每一行单独绑事件
    document.addEventListener("click", (event) => {
      const openRunBtn = closest(event, "[data-open-run]");
      if (openRunBtn) { openRun(openRunBtn.dataset.openRun); return; }
      const sourceBtn = closest(event, "[data-open-source]");
      if (sourceBtn) { setSection("sec-sources"); openSource(sourceBtn.dataset.openSource); return; }
      const drawerSource = closest(event, "[data-drawer-source]");
      if (drawerSource) { openDrawerSource(drawerSource.dataset.drawerSource); return; }
      const logBtn = closest(event, "[data-open-log]");
      if (logBtn) {
        setSection("sec-logs");
        const row = $(`.monitor-log[data-log="${logBtn.dataset.openLog}"]`);
        if (row) { row.scrollIntoView({ block: "center" }); expandLog(row, Number(logBtn.dataset.openLog)); }
        return;
      }
      const runsPage = closest(event, "[data-runs-page]");
      if (runsPage && !runsPage.disabled) { loadRuns(Number(runsPage.dataset.runsPage)); return; }
      const logsPage = closest(event, "[data-logs-page]");
      if (logsPage && !logsPage.disabled) {
        loadLogs(Number(logsPage.dataset.logsPage),
                 { append: logsPage.dataset.logsAppend === "1" });
        return;
      }
      const bar = closest(event, ".monitor-bar");
      if (bar) { openRun(bar.dataset.run); return; }
      const runRow = closest(event, "tr[data-run]");
      if (runRow) { openRun(runRow.dataset.run); return; }
      const logRow = closest(event, "[data-log]");
      if (logRow) { expandLog(logRow, Number(logRow.dataset.log)); return; }
      const sourceRow = closest(event, "tr[data-source]");
      if (sourceRow) { openSource(sourceRow.dataset.source); }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = closest(event, "tr[data-run], tr[data-source], [data-log], [data-drawer-source]");
      if (!row) return;
      event.preventDefault();
      if (row.dataset.run) openRun(row.dataset.run);
      else if (row.dataset.source) openSource(row.dataset.source);
      else if (row.dataset.log) expandLog(row, Number(row.dataset.log));
      else if (row.dataset.drawerSource) openDrawerSource(row.dataset.drawerSource);
    });

    document.addEventListener("visibilitychange", () => {
      const label = $("#liveText");
      if (document.hidden) {
        if (label) label.textContent = "页面不可见，已暂停高频刷新";
      } else {
        // 回到前台只补总览/时间线/运行记录；日志面板保持当前阅读位置，不用整页重取把它顶回顶部
        refreshLight();
        if ($("#logsAuto").checked) {
          pollLogs().catch((err) => notice(`恢复刷新失败：${err.message}`, "bad"));
        }
      }
    });
  }

  function setSection(id) {
    $$("#sectionNav [data-section]").forEach((button) => {
      const active = button.dataset.section === id;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const section = $("#" + id);
    if (section) section.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  /* ------------------------------------------------------------------ 轮询 */

  function restartLogPoll() {
    if (state.logPoll) { clearInterval(state.logPoll); state.logPoll = null; }
    if (!$("#logsAuto").checked) return;
    const seconds = Number($("#logsInterval").value) || 15;
    state.logPoll = setInterval(() => {
      if (document.hidden) return;
      pollLogs().catch((err) => notice(`日志自动刷新失败：${err.message}`, "bad"));
    }, seconds * 1000);
  }

  function startPoll() {
    if (state.poll) clearInterval(state.poll);
    state.poll = setInterval(() => {
      if (document.hidden) return;    // 页面不可见时暂停高频轮询
      refreshLight();
    }, PAGE_POLL_MS);
  }

  async function refreshLight() {
    if (state.busy || document.hidden) return;
    state.busy = true;
    try {
      const [overview, runs, timeline] = await Promise.all([
        apiGet("/overview", {}),
        apiGet("/runs", runsParams(state.runs.page)),
        apiGet("/timeline", { window: $("#timelineWindow").value }),
      ]);
      state.overview = overview;
      state.timeline = timeline;
      state.runs = { rows: runs.runs, total: runs.total, page: runs.page,
                     page_size: runs.page_size, sort: runs.sort, order: runs.order };
      renderOverview(overview);
      renderRuns();
      renderTimeline(timeline);
      setLive(timeline.running ? "info" : "ok",
              timeline.running ? `有 ${timeline.running} 个任务在运行 · ${fmtTime(overview.generated_at)}`
                               : `已更新 ${fmtTime(overview.generated_at)}`);
    } catch (err) {
      setLive("bad", "读取失败");
      notice(`自动刷新失败：${err.message}`, "bad");
    } finally {
      state.busy = false;
    }
  }

  async function loadTimeline() {
    const box = $("#timelineBody");
    clear(box).appendChild(el("p", { class: "pending", text: "正在读取时间线…" }));
    try {
      const payload = await apiGet("/timeline", { window: $("#timelineWindow").value });
      state.timeline = payload;
      renderTimeline(payload);
    } catch (err) {
      clear(box).appendChild(errorState(err));
    }
  }

  async function reloadAll(showNotice) {
    setLive("info", "正在读取…");
    try {
      const [overview, timeline, runs, sources, logs] = await Promise.all([
        apiGet("/overview", {}),
        apiGet("/timeline", { window: $("#timelineWindow").value }),
        apiGet("/runs", runsParams(state.runs.page)),
        apiGet("/sources", sourcesParams()),
        apiGet("/logs", logsParams({ page: state.logs.page || 1 })),
      ]);
      state.overview = overview;
      state.timeline = timeline;
      state.runs = { rows: runs.runs, total: runs.total, page: runs.page,
                     page_size: runs.page_size, sort: runs.sort, order: runs.order };
      renderOverview(overview);
      renderTimeline(timeline);
      renderRuns();
      renderSources(sources);
      renderLogs(logs);
      setLive("ok", `已更新 ${fmtTime(overview.generated_at)}`);
      if (showNotice) notice("");
      const kinds = (sources.summary && sources.summary.kinds) || [];
      if (kinds.length) fillSelect($("#sourcesKind"), kinds, "全部类型");
    } catch (err) {
      setLive("bad", "读取失败");
      notice(`读取失败：${err.message}（监控接口是只读的，采集本身不受影响）`, "bad");
      if (!state.overview) {
        clear($("#overviewCards")).appendChild(errorState(err));
      }
      if (!state.timeline) clear($("#timelineBody")).appendChild(errorState(err));
      if (!state.runs.rows.length) {
        const table = clear($("#runsTable"));
        table.appendChild(el("tbody", null, [el("tr", null, [el("td", null, [errorState(err)])])]));
      }
      if (!state.sources.rows.length) {
        const table = clear($("#sourcesTable"));
        table.appendChild(el("tbody", null, [el("tr", null, [el("td", null, [errorState(err)])])]));
      }
      if (!state.logs.rows.length) clear($("#logsList")).appendChild(errorState(err));
    }
  }

  async function init() {
    bind();
    try {
      const meta = await apiGet("/meta", {});
      applyMeta(meta);
      $("#logsAutoState").textContent = "自动刷新关闭（只在你点时读取）";
    } catch (err) {
      notice(`监控接口不可用：${err.message}`, "bad");
    }
    await reloadAll(false);
    await loadSources().catch(() => {});
    startPoll();
  }

  // 供浏览器测试与调试使用（页面交互本身不依赖它）
  window.FBMonitor = {
    state,
    reload: reloadAll,
    openRun,
    openSource,
    closeDrawer,
    loadRuns,
    loadLogs,
    loadSources,
    pollLogs,
    expandLog,
    apiGet,
    safeLink,
    setSection,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();