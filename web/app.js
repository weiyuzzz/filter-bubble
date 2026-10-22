/* GitPulse — front-end controller */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const cardList = $("#cardList");
  const toasts = $("#toasts");

  const state = {
    digest: null,
    status: null,
    tab: localStorage.getItem("gp.tab") || "recommend",
    busy: new Set(),
    poll: null,
    firstPaint: true,
    resetArmed: false,
    resetTimer: null,
  };

  const ICONS = {
    up: '<svg viewBox="0 0 24 24" fill="none"><path d="M7 10v11M7 11l4.2-7.2a1.6 1.6 0 0 1 2.9 1.2L13 10h5.2a2 2 0 0 1 1.9 2.6l-2 6.4a2.5 2.5 0 0 1-2.4 1.7H7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none"><path d="M17 14V3m0 10-4.2 7.2a1.6 1.6 0 0 1-2.9-1.2L11 14H5.8a2 2 0 0 1-1.9-2.6l2-6.4A2.5 2.5 0 0 1 8.3 3H17" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    mark: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 4h12v17l-6-4.2L6 21V4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    markOn: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h12v17l-6-4.2L6 21V4Z"/></svg>',
    open: '<svg viewBox="0 0 24 24" fill="none"><path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 3l1.7 5.1L19 10l-5.3 1.9L12 17l-1.7-5.1L5 10l5.3-1.9L12 3Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z" fill="currentColor"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none"><path d="m5 12.5 4.2 4.2L19 7" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5m0 3h.01M10.3 4.2 2.7 18a1.4 1.4 0 0 0 1.3 2h16a1.4 1.4 0 0 0 1.3-2L13.7 4.2a1.4 1.4 0 0 0-2.4 0Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  const nf = new Intl.NumberFormat("zh-CN");

  function compact(n) {
    n = Number(n || 0);
    if (n >= 10000) {
      const v = n / 10000;
      return (v >= 10 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, "")) + "万";
    }
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return nf.format(n);
  }
  const exact = (n) => nf.format(Number(n || 0));

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function timeAgo(iso) {
    if (!iso) return "—";
    const then = new Date(iso);
    if (Number.isNaN(then.getTime())) return "—";
    const sec = Math.max(0, (Date.now() - then.getTime()) / 1000);
    if (sec < 90) return "刚刚";
    if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
    if (sec < 86400) return Math.round(sec / 3600) + " 小时前";
    if (sec < 86400 * 7) return Math.round(sec / 86400) + " 天前";
    return `${then.getMonth() + 1}/${then.getDate()}`;
  }

  function longDate(iso) {
    const d = iso ? new Date(iso) : new Date();
    try {
      return new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(d);
    } catch (_e) {
      return `${d.getMonth() + 1}月${d.getDate()}日`;
    }
  }

  function dotDate(iso) {
    const d = iso ? new Date(iso) : new Date();
    return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
  }

  function nextRunLabel(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  async function api(path, options = {}) {
    const opts = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (_e) { data = {}; }
    if (!res.ok && res.status !== 202) {
      throw new Error(data.error || `请求失败 (${res.status})`);
    }
    return { status: res.status, data };
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

  /* ---------------- data helpers ---------------- */
  const filters = {
    recommend: () => true,
    surge: (i) => i.surge >= 70 || i.badge === "增速冠军",
    fresh: (i) => Number(i.age_days) <= 45,
    sustained: (i) => (i.sources || []).includes("weekly"),
    saved: (i) => !!i.bookmarked,
  };

  function filtered() {
    const items = (state.digest && state.digest.items) || [];
    return items.filter(filters[state.tab] || (() => true));
  }

  function counts() {
    const items = (state.digest && state.digest.items) || [];
    const out = {};
    Object.keys(filters).forEach((key) => { out[key] = items.filter(filters[key]).length; });
    return out;
  }

  function badgeClass(badge) {
    return {
      "日榜第一": "badge-ember",
      "增速冠军": "badge-lime",
      "新星崛起": "badge-blue",
      "日榜前三": "badge-lime",
      "上升中": "badge-violet",
      "持续走热": "badge-violet",
    }[badge] || "badge-muted";
  }

  function heatColor(heat) {
    if (heat >= 85) return "var(--ember)";
    if (heat >= 65) return "var(--lime)";
    return "var(--blue)";
  }

  function heatLabel(heat) {
    if (heat >= 85) return "沸腾";
    if (heat >= 70) return "高热";
    if (heat >= 55) return "升温";
    return "观察";
  }

  function ageLabel(item) {
    if (item.created_at && item.age_days != null) {
      const d = Number(item.age_days);
      if (d <= 0) return "今天创建";
      if (d <= 60) return `${d} 天前创建`;
      if (d <= 730) return `${Math.round(d / 30)} 个月前创建`;
      return `${Math.round(d / 365)} 年前创建`;
    }
    return null;
  }

  function hueFor(text) {
    let h = 0;
    for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  /* ---------------- rendering ---------------- */
  function renderCard(item, index) {
    const featured = state.tab === "recommend" && index === 0;
    const owner = item.owner || item.full_name.split("/")[0];
    const zh = item.summary_zh || "";
    const description = zh || item.description || "这个项目还没有填写简介，点开看看 README。";
    const english = zh && item.description ? item.description : "";
    const topics = (item.topics || []).slice(0, 3);
    const cats = item.categories || [];
    const extraTopics = Math.max(0, (item.topics || []).length - topics.length);
    const today = Number(item.stars_today || 0);
    const week = Number(item.stars_week || 0);
    const readme = item.readme_zh || "";
    const distance = ageLabel(item);
    const reasonText = item.reason || "热度优先";
    const matchChip = item.match != null ? `<span class="match-chip">${item.match}% 匹配</span>` : "";
    const actionBusy = state.busy.has(item.full_name);
    const initials = owner.replace(/[^a-zA-Z0-9]/g, "").slice(0, 2).toUpperCase() || "GH";
    const hue = hueFor(owner);

    const subBits = [];
    if (item.language) {
      subBits.push(`<span><i class="lang-dot" style="background:${esc(item.language_color || "#8b949e")}"></i>${esc(item.language)}</span>`);
    }
    if (distance) subBits.push(`<span>${esc(distance)}</span>`);
    subBits.push(`<span>★ ${compact(item.stars)}</span>`);

    const tags = [];
    cats.slice(0, 2).forEach((c) => tags.push(`<span class="tag cat">${esc(c)}</span>`));
    topics.forEach((t) => tags.push(`<span class="tag">${esc(t)}</span>`));
    if (extraTopics > 0) tags.push(`<span class="tag more">+${extraTopics}</span>`);

    const statToday = today > 0
      ? `<div class="stat hot"><strong>+${exact(today)}</strong><span>今日 star</span></div>`
      : week > 0
        ? `<div class="stat hot"><strong>+${compact(week)}</strong><span>本周 star</span></div>`
        : "";

    return `
      <article class="card${featured ? " is-featured" : ""}" data-repo="${esc(item.full_name)}" style="--i:${index}">
        ${featured ? `<span class="rank-ghost" aria-hidden="true">01</span>` : ""}
        <div class="card-top">
          <div class="rank-line">
            <span class="rank-num">#${String(item.rank || index + 1).padStart(2, "0")}</span>
            <span class="badge ${badgeClass(item.badge)}">${esc(item.badge || "值得一看")}</span>
          </div>
          <div class="heat-wrap" title="热度 ${item.heat}/100">
            <div class="heat" style="--v:${item.heat};--c:${heatColor(item.heat)}"><b>${item.heat}</b></div>
            <span class="heat-label">${heatLabel(item.heat)}</span>
          </div>
        </div>

        <div class="repo-head">
          <div class="avatar" style="--h:${hue}" aria-hidden="true">
            <span>${esc(initials)}</span>
            <img src="${esc(item.avatar || `https://github.com/${owner}.png?size=96`)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">
          </div>
          <div class="repo-id">
            <a class="repo-name" href="${esc(item.url)}" target="_blank" rel="noreferrer">
              <span class="owner">${esc(owner)} /</span> ${esc(item.name || item.full_name.split("/")[1])}
            </a>
            <div class="repo-sub">${subBits.join('<span class="dot-sep">·</span>')}</div>
          </div>
        </div>

        <p class="desc">${esc(description)}</p>
        ${english ? `<p class="desc-en"><span class="en-tag">EN</span>${esc(english)}</p>` : ""}
        ${readme ? `<div class="readme"><b>README</b><span>${esc(readme)}</span></div>` : ""}

        <div class="tags">${tags.join("")}</div>

        <div class="card-stats">
          ${statToday}
          <div class="stat"><strong>${compact(item.stars)}</strong><span>总计 star</span></div>
          <div class="stat"><strong>${compact(item.forks)}</strong><span>Fork</span></div>
          <div class="insight">${esc(item.insight || "")}</div>
        </div>

        <div class="card-foot">
          <div class="reason">${ICONS.spark}<span class="text">${esc(reasonText)}</span>${matchChip}</div>
          <div class="actions">
            <button class="act like${item.feedback === 1 ? " is-on" : ""}" data-act="like" data-repo="${esc(item.full_name)}" aria-pressed="${item.feedback === 1}" title="喜欢，教它多推类似项目" ${actionBusy ? "disabled" : ""}>${ICONS.up}<span>喜欢</span></button>
            <button class="act dislike${item.feedback === -1 ? " is-on" : ""}" data-act="dislike" data-repo="${esc(item.full_name)}" aria-pressed="${item.feedback === -1}" title="不感兴趣，减少类似推荐" ${actionBusy ? "disabled" : ""}>${ICONS.down}<span>不喜欢</span></button>
            <button class="act bookmark${item.bookmarked ? " is-on" : ""}" data-act="bookmark" data-repo="${esc(item.full_name)}" aria-pressed="${item.bookmarked}" title="收藏" ${actionBusy ? "disabled" : ""}>${item.bookmarked ? ICONS.markOn : ICONS.mark}</button>
            <a class="act ext" href="${esc(item.url)}" target="_blank" rel="noreferrer" title="在 GitHub 打开" aria-label="在 GitHub 打开 ${esc(item.full_name)}">${ICONS.open}</a>
          </div>
        </div>
      </article>`;
  }

  function emptyHTML() {
    if (state.tab === "saved") {
      return `<div class="empty-mark">◎</div><h3>还没有收藏</h3><p>在卡片上点书签图标，把想回看的项目留在这里。</p><button class="ghost-btn" data-goto="recommend">回到推荐</button>`;
    }
    return `<div class="empty-mark">◎</div><h3>这个筛选下暂时没有项目</h3><p>今天的信号里没有符合这一类的项目，换个视角看看。</p><button class="ghost-btn" data-goto="recommend">回到推荐</button>`;
  }

  function animateFlip(previous) {
    $$(".card", cardList).forEach((el) => {
      const oldTop = previous.get(el.dataset.repo);
      if (oldTop == null) return;
      const delta = oldTop - el.getBoundingClientRect().top;
      if (Math.abs(delta) < 2) return;
      el.style.transition = "none";
      el.style.transform = `translateY(${delta}px)`;
      requestAnimationFrame(() => {
        el.style.transition = "transform .48s cubic-bezier(.22,1,.36,1)";
        el.style.transform = "";
      });
    });
  }

  function renderCards({ flip = false, learnRepo = null } = {}) {
    if (!state.digest) return;
    const items = filtered();
    const previous = flip ? new Map($$(".card", cardList).map((el) => [el.dataset.repo, el.getBoundingClientRect().top])) : null;
    const count = $("#resultCount");

    if (!items.length) {
      cardList.innerHTML = "";
      cardList.classList.remove("animate");
      $("#empty").classList.remove("hidden");
      $("#empty").innerHTML = emptyHTML();
      const total = state.digest.items.length;
      count.textContent = `0 / ${total} 个项目 · ${{ recommend: "按综合信号", surge: "按增长动量", fresh: "45 天内", sustained: "周榜续航", saved: "你的收藏" }[state.tab] || ""}`;
      return;
    }
    $("#empty").classList.add("hidden");
    cardList.innerHTML = items.map(renderCard).join("");
    const total = state.digest.items.length;
    count.textContent = `${items.length} 个项目 · ${state.tab === "recommend" ? (state.digest.profile.ready ? "按你的口味排序" : "按今日热度排序") : "筛选视图"}`;
    cardList.classList.toggle("animate", state.firstPaint);
    if (state.firstPaint) {
      state.firstPaint = false;
      setTimeout(() => cardList.classList.remove("animate"), 900);
    }
    if (previous) animateFlip(previous);
    if (learnRepo) {
      const el = cardList.querySelector(`[data-repo="${CSS.escape(learnRepo)}"]`);
      if (el) {
        el.classList.add("just-learned");
        setTimeout(() => el.classList.remove("just-learned"), 1000);
      }
    }
  }

  function renderTabs() {
    const c = counts();
    $$(".tab").forEach((tab) => {
      const key = tab.dataset.tab;
      tab.classList.toggle("is-active", key === state.tab);
      tab.setAttribute("aria-selected", key === state.tab ? "true" : "false");
      const pill = tab.querySelector(".tab-count");
      if (pill) pill.textContent = c[key] ?? 0;
    });
  }

  function renderHero() {
    const d = state.digest;
    if (!d) return;
    const s = d.stats || {};
    $("#topDate").textContent = longDate(d.generated_at);
    $("#boardDate").textContent = dotDate(d.generated_at);
    $("#eyebrow").textContent = `DAILY GITHUB INTELLIGENCE · ${String(d.items.length).padStart(2, "0")} SIGNALS`;
    $("#mTotal").textContent = String(s.total || d.items.length).padStart(2, "0");
    $("#mStars").textContent = s.stars_today ? "+" + exact(s.stars_today) : "—";
    $("#mStars").title = exact(s.stars_today || 0) + " stars today";
    if (s.fastest) {
      $("#mFast").textContent = s.fastest.stars_today ? "+" + exact(s.fastest.stars_today) : "—";
      $("#mFastRepo").textContent = s.fastest.full_name.split("/")[1].slice(0, 22);
      $("#mFastRepo").title = s.fastest.full_name;
    }
    const matches = d.items.map((i) => i.match).filter((m) => m != null);
    const ready = d.profile && d.profile.ready;
    if (ready && matches.length) {
      const avg = Math.round(matches.reduce((a, b) => a + b, 0) / matches.length);
      $("#mMatch").textContent = avg + "%";
      $("#mMatch").classList.add("lime");
      $("#mMatchLabel").textContent = "taste match";
    } else {
      $("#mMatch").textContent = "—";
      $("#mMatch").classList.remove("lime");
      $("#mMatchLabel").textContent = "待训练";
    }
    $("#boardUpdated").textContent = timeAgo(d.generated_at);
    const samples = (d.profile && d.profile.samples) || 0;
    $("#boardStatus").textContent = ready
      ? `口味模型已激活 · 已学习 ${samples} 次评价`
      : "模型待训练 · 点 👍 教我你的口味";
    $("#statusLight").classList.toggle("training", !ready);
    $("#tasteCount").textContent = samples;
  }

  function renderOnboard() {
    const d = state.digest;
    if (!d) return;
    const samples = (d.profile && d.profile.samples) || 0;
    const el = $("#onboard");
    if (d.profile && d.profile.ready) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    $("#onboardCount").textContent = Math.min(samples, 3);
    $$("#onboardDots i").forEach((dot, i) => dot.classList.toggle("on", i < Math.min(samples, 3)));
  }

  function barsHTML(items, maxWeight) {
    return items.map((item) => {
      const width = Math.max(5, Math.round((Math.max(item.weight, 0.001) / maxWeight) * 100));
      const evidence = item.pos || item.neg ? `<span class="evidence" title="来自 ${(item.pos || 0) + (item.neg || 0)} 次评价">${(item.pos || 0) + (item.neg || 0)}×</span>` : "";
      return `<div class="bar-row">
        <div class="bar-label"><span>${esc(item.label)}${evidence}</span><b>${item.weight.toFixed(2)}</b></div>
        <div class="bar-track"><i style="width:${width}%"></i></div>
      </div>`;
    }).join("");
  }

  function renderRail() {
    const d = state.digest;
    if (!d) return;
    const p = d.profile || { samples: 0, ready: false };
    const rail = $("#railTaste");
    if (!p.ready) {
      const pct = Math.min(100, (p.samples / 3) * 100);
      rail.innerHTML = `<div class="taste-empty">
        <p>还差 <b style="color:var(--lime)">${Math.max(0, 3 - p.samples)}</b> 次评价，我就能开始按你的偏好重排今日榜单。</p>
        <div class="taste-progress"><span>训练进度</span><b>${p.samples}/3</b></div>
        <div class="progress-track"><i style="width:${pct}%"></i></div>
        <p style="margin-top:13px">不知道怎么开始？挑一个你一看就想点开的项目，点 👍 就行。</p>
      </div>`;
      return;
    }
    const combined = [...(p.categories || []).slice(0, 3), ...(p.languages || []).slice(0, 2)];
    const max = Math.max(...combined.map((x) => x.weight), 0.01);
    rail.innerHTML = `<div class="taste-ready-head"><span>实时学习 · ${p.samples} 次评价</span><b>LIVE</b></div>
      ${combined.length ? barsHTML(combined, max) : '<p class="muted-note">再评价几个项目，偏好就会显形。</p>'}
      <p class="rail-note">每次 👍 / 👎 都会即时调整今日排序。</p>`;
  }

  function renderDrawer() {
    const d = state.digest;
    if (!d) return;
    const p = d.profile || { samples: 0, ready: false };
    const need = Math.max(0, 3 - p.samples);
    $("#modelStatus").innerHTML = `
      <div class="model-top"><span class="model-big">${p.samples}</span><p>次有效评价<br><span style="color:var(--muted-2);font-size:9px">MODEL SAMPLES</span></p></div>
      <span class="model-state ${p.ready ? "" : "training"}">${p.ready ? "● 已激活个性化" : `○ 还需要 ${need} 次评价`}</span>
      <p class="model-desc">${p.ready ? "模型已经学会在热度与你的口味之间做取舍。评价越多，排序越贴近你。" : "完成 3 次评价后，模型会开始影响排序；它同时保留热度信号，避免只给你看同一种项目。"}</p>
      <div class="model-meter"><div class="progress-track"><i style="width:${Math.min(100, (p.samples / 3) * 100)}%"></i></div></div>`;

    const groups = [
      ["领域", p.categories || []],
      ["语言", p.languages || []],
      ["话题", p.topics || []],
      ["关键词", p.keywords || []],
    ].filter(([, items]) => items.length);
    if (!p.ready) {
      $("#tasteGroups").innerHTML = '<p class="muted-note">模型还在等待第一批样本。给它 3 个项目，它就会画出你的偏好图谱。</p>';
    } else if (!groups.length) {
      $("#tasteGroups").innerHTML = '<p class="muted-note">目前的评价还没有形成明显偏好，再试几个不同类型的项目。</p>';
    } else {
      $("#tasteGroups").innerHTML = groups.map(([title, items]) => {
        const max = Math.max(...items.map((x) => x.weight), 0.01);
        return `<div class="taste-group"><h4>${title}</h4>${barsHTML(items, max)}</div>`;
      }).join("");
    }

    const avoid = p.avoid || [];
    $("#avoidList").innerHTML = avoid.length
      ? avoid.map((x) => `<span class="avoid-chip">${esc(x.label)}<b>${x.weight.toFixed(2)}</b></span>`).join("")
      : '<p class="muted-note">还没有明确的负向偏好。点 👎 的项目越多，这里越具体。</p>';
  }

  function renderAll() {
    renderTabs();
    renderHero();
    renderRail();
    renderOnboard();
    renderDrawer();
    renderCards();
  }

  /* ---------------- loading / pending ---------------- */
  function renderPending(stage) {
    const label = stage || "正在连接 GitHub";
    cardList.innerHTML = `
      <div class="pending" style="grid-column:1/-1">
        <div class="radar"><i></i></div>
        <h3>正在扫描今天的 GitHub 信号</h3>
        <p>趋势榜 · 新项目 · README · 翻译，大约需要半分钟。</p>
        <span class="stage">${esc(label)}</span>
      </div>
      <div class="skel-card wide"></div><div class="skel-card"></div><div class="skel-card"></div>`;
    $("#resultCount").textContent = "正在扫描 GitHub…";
    $("#empty").classList.add("hidden");
    document.body.classList.add("is-loading");
  }

  function stopPolling() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  function pollUntilReady() {
    stopPolling();
    const started = Date.now();
    const tick = async () => {
      try {
        const { data } = await api("/api/status");
        state.status = data;
        updateSchedule(data);
        if (data.state && data.state.stage && !state.digest) renderPending(data.state.stage);
        if (!data.state.running) {
          if (data.today_ready) {
            const digest = await api("/api/digest");
            if (!digest.data.pending) {
              stopPolling();
              document.body.classList.remove("is-loading");
              $("#refreshBtn").classList.remove("loading");
              $("#refreshBtn").disabled = false;
              state.firstPaint = true;
              state.digest = digest.data;
              renderAll();
              toast("今日日报已更新 · 共 " + digest.data.items.length + " 个项目");
              return;
            }
          }
          if (data.state.last_error) {
            stopPolling();
            document.body.classList.remove("is-loading");
            $("#refreshBtn").classList.remove("loading");
            $("#refreshBtn").disabled = false;
            toast(data.state.last_error, "error", 6000);
            if (!state.digest) renderPending("生成失败，请稍后重试");
            return;
          }
        }
        if (Date.now() - started > 300000) {
          stopPolling();
          document.body.classList.remove("is-loading");
          $("#refreshBtn").classList.remove("loading");
          $("#refreshBtn").disabled = false;
          toast("生成时间较长，请稍后刷新页面查看", "error");
        }
      } catch (err) {
        stopPolling();
        document.body.classList.remove("is-loading");
        toast(err.message, "error");
      }
    };
    tick();
    state.poll = setInterval(tick, 2600);
  }

  function updateSchedule(status) {
    if (!status) return;
    if (status.schedule) {
      $("#scheduleTime").innerHTML = `${esc(status.schedule.time)}<small>${esc((status.schedule.timezone || "").replace("Asia/", ""))}</small>`;
      $("#nextRun").textContent = nextRunLabel(status.schedule.next_run);
    }
    $("#lastGenerated").textContent = status.latest_generated_at ? timeAgo(status.latest_generated_at) : "—";
  }

  /* ---------------- interactions ---------------- */
  async function sendFeedback(repo, signal) {
    if (state.busy.has(repo)) return;
    state.busy.add(repo);
    const beforeSamples = (state.digest.profile && state.digest.profile.samples) || 0;
    try {
      const { data } = await api("/api/feedback", {
        method: "POST",
        body: JSON.stringify({ repo, signal }),
      });
      state.digest = data;
      state.firstPaint = false;
      renderTabs();
      renderHero();
      renderRail();
      renderOnboard();
      renderDrawer();
      renderCards({ flip: true, learnRepo: repo });
      const samples = data.profile.samples || 0;
      if (signal === 0) toast("已撤销评价");
      else if (beforeSamples < 3 && samples >= 3) toast("口味模型已激活 · 接下来的排序会越来越像你", "ok", 5000);
      else if (signal === 1) toast("收到 👍 · 相似项目会获得更高匹配");
      else toast("收到 👎 · 会减少这类推荐");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.busy.delete(repo);
      $$(`[data-repo="${CSS.escape(repo)}"] .act`, cardList).forEach((b) => { b.disabled = false; });
    }
  }

  async function toggleBookmark(repo) {
    if (state.busy.has(repo)) return;
    state.busy.add(repo);
    try {
      const { data } = await api("/api/bookmark", {
        method: "POST",
        body: JSON.stringify({ repo }),
      });
      const item = state.digest.items.find((i) => i.full_name === repo);
      if (item) item.bookmarked = data.bookmarked;
      toast(data.bookmarked ? "已加入收藏" : "已从收藏移除");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.busy.delete(repo);
      renderTabs();
      renderCards();
    }
  }

  async function refresh() {
    if ($("#refreshBtn").disabled) return;
    $("#refreshBtn").disabled = true;
    $("#refreshBtn").classList.add("loading");
    document.body.classList.add("is-loading");
    try {
      const { data } = await api("/api/refresh", { method: "POST" });
      toast(data.started ? "开始抓取今天的 GitHub 数据…" : "已有任务在运行，稍等一下");
      pollUntilReady();
    } catch (err) {
      document.body.classList.remove("is-loading");
      $("#refreshBtn").classList.remove("loading");
      $("#refreshBtn").disabled = false;
      toast(err.message, "error");
    }
  }

  function openDrawer() {
    const drawer = $("#drawer");
    const backdrop = $("#drawerBackdrop");
    backdrop.hidden = false;
    drawer.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => drawer.classList.add("open"));
    document.body.style.overflow = "hidden";
    renderDrawer();
  }

  function closeDrawer() {
    const drawer = $("#drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    setTimeout(() => { $("#drawerBackdrop").hidden = true; }, 380);
  }

  async function resetTaste() {
    const btn = $("#resetBtn");
    if (!state.resetArmed) {
      state.resetArmed = true;
      btn.classList.add("confirming");
      btn.textContent = "再点一次，确认清空";
      $("#resetHint").textContent = "将删除全部评价与权重";
      state.resetTimer = setTimeout(() => {
        state.resetArmed = false;
        btn.classList.remove("confirming");
        btn.textContent = "清空口味记忆";
        $("#resetHint").textContent = "此操作不可撤销";
      }, 3500);
      return;
    }
    clearTimeout(state.resetTimer);
    state.resetArmed = false;
    btn.disabled = true;
    try {
      const { data } = await api("/api/reset", { method: "POST", body: "{}" });
      state.digest = data;
      state.firstPaint = true;
      renderAll();
      closeDrawer();
      toast("口味记忆已清空，模型重新开始");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.classList.remove("confirming");
      btn.textContent = "清空口味记忆";
      $("#resetHint").textContent = "此操作不可撤销";
    }
  }

  function bind() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.tab = tab.dataset.tab;
        localStorage.setItem("gp.tab", state.tab);
        state.firstPaint = true;
        renderTabs();
        renderCards();
        const active = $(".tab.is-active");
        if (active) active.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
      });
    });

    cardList.addEventListener("click", (event) => {
      const action = event.target.closest("[data-act]");
      if (action) {
        event.preventDefault();
        const repo = action.dataset.repo;
        const act = action.dataset.act;
        if (act === "bookmark") return toggleBookmark(repo);
        const item = state.digest.items.find((i) => i.full_name === repo);
        if (!item) return;
        const target = act === "like" ? 1 : -1;
        const next = item.feedback === target ? 0 : target;
        return sendFeedback(repo, next);
      }
      const goto = event.target.closest("[data-goto]");
      if (goto) {
        state.tab = goto.dataset.goto;
        localStorage.setItem("gp.tab", state.tab);
        renderTabs();
        renderCards();
      }
    });

    $("#refreshBtn").addEventListener("click", refresh);
    const toTop = $("#toTop");
    window.addEventListener("scroll", () => {
      toTop.classList.toggle("show", window.scrollY > 900);
    }, { passive: true });
    toTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    $("#tasteBtn").addEventListener("click", openDrawer);
    $("#openTaste").addEventListener("click", openDrawer);
    $("#closeDrawer").addEventListener("click", closeDrawer);
    $("#drawerBackdrop").addEventListener("click", closeDrawer);
    $("#resetBtn").addEventListener("click", resetTaste);
    $("#empty").addEventListener("click", (event) => {
      const goto = event.target.closest("[data-goto]");
      if (!goto) return;
      state.tab = goto.dataset.goto;
      localStorage.setItem("gp.tab", state.tab);
      renderTabs();
      renderCards();
    });
    $("#howBtn").addEventListener("click", () => {
      openDrawer();
      setTimeout(() => {
        const el = $(".taste-section.how");
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 460);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
      if (event.target.matches("input, textarea")) return;
      if (event.key === "t" && !event.metaKey && !event.ctrlKey) openDrawer();
      if (event.key === "r" && !event.metaKey && !event.ctrlKey) refresh();
    });
  }

  async function init() {
    $("#topDate").textContent = longDate();
    $("#boardDate").textContent = dotDate();
    bind();
    try {
      const [digestRes, statusRes] = await Promise.all([api("/api/digest"), api("/api/status")]);
      state.status = statusRes.data;
      updateSchedule(statusRes.data);
      if (digestRes.status === 202 || digestRes.data.pending) {
        renderPending((statusRes.data.state && statusRes.data.state.stage) || "正在生成今天的日报");
        $("#refreshBtn").disabled = true;
        pollUntilReady();
        return;
      }
      state.digest = digestRes.data;
      renderAll();
      $("#refreshBtn").disabled = false;
      if (location.hash === "#taste") openDrawer();
      
      if (statusRes.data.state && statusRes.data.state.running) {
        document.body.classList.add("is-loading");
        $("#refreshBtn").classList.add("loading");
        pollUntilReady();
      }
    } catch (err) {
      renderPending("无法连接到本地服务");
      toast(err.message, "error", 6000);
    }
  }

  init();
})();
