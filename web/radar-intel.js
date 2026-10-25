/* 全球态势监控工作台（零依赖，原生 JS）。
 *
 * 数据来源：GET /api/radar/intel（主契约）+ /intel/live（增量轮询）。
 * 所有面板（地图 / 时间线 / 战略风险 / 国家 / 情报流 / 实时新闻）都读同一份
 * state.data，用 story_id / event_id 做关联，避免各面板各算一套导致数字打架。
 *
 * 地图是**离线 SVG 矢量**：底图来自 /world-110m.js（Natural Earth，本地生成），
 * 投影公式与 core/geo/project.py 完全一致，不依赖任何瓦片服务器或 CDN。
 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const MAP_W = 3600, MAP_H = 1800, LAT_LIMIT = 85.05112878;
  const THEME_LABELS = {
    conflict: '武装冲突', unrest: '社会动荡', politics: '政治外交', security: '公共安全',
    cyber: '网络安全', disaster: '自然灾害', infrastructure: '基础设施',
    technology: '科技动态', general_news: '综合新闻', unknown: '未分类',
  };
  const COMPASS_LABELS = {
    high: '来源给出坐标', medium: '地名匹配', low: '国家级近似', none: '未定位',
  };
  const HAZARD_LABELS = {
    earthquake: '地震', volcano: '火山', wildfire: '野火', flood: '洪水',
    storm: '风暴', drought: '干旱', other: '其他',
  };
  const PRECISION_LABELS = { point: '来源点坐标', area: '面范围（非精确中心）', approximate: '近似位置' };
  const LIVE_POLL_MS = 60000;
  // 超过这个缩放级别就不再聚合，所有事件展开为独立点位
  const THEME_UNCLUSTER_ZOOM = 2.4;

  const state = {
    data: null,
    loading: false,
    error: null,
    filters: { days: 7, theme: '', country: '', minScore: 0, precision: 'all', day: '' },
    layers: { news_events: true, usgs_quake: true },
    hazards: { earthquake: true, volcano: true, wildfire: true, flood: true, storm: true, other: true },
    selectedPoint: null,
    map: { zoom: 1, x: 0, y: 0, selected: null, dragging: false, moved: false },
    live: { cursor: null, items: [], before: null, hasOlder: true, timer: null },
    seen: Object.create(null),
  };

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16).replace('T', ' ');
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function toInt(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) ? n : fallback;
  }

  /* ---------------------------------------------------------------- 投影 */
  // 与 core/geo/project.py 同一组公式：输入经纬度，输出 SVG 画布坐标
  function project(lon, lat) {
    const clamped = Math.max(-LAT_LIMIT, Math.min(LAT_LIMIT, Number(lat) || 0));
    const x = ((Number(lon) || 0) + 180) / 360 * MAP_W;
    const rad = clamped * Math.PI / 180;
    const y = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * MAP_H;
    return { x, y };
  }

  function validCoord(lat, lon) {
    return Number.isFinite(lat) && Number.isFinite(lon)
      && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
      && !(Math.abs(lat) < 0.0005 && Math.abs(lon) < 0.0005);
  }

  function hazardColor(hazard) {
    switch (hazard) {
      case 'earthquake': return 'var(--fb-info)';
      case 'volcano': return 'var(--fb-danger)';
      case 'wildfire': return 'var(--fb-warn)';
      case 'flood': return '#1f7a8c';
      case 'storm': return 'var(--fb-accent)';
      case 'drought': return '#8a6d3b';
      default: return 'var(--fb-text-3)';
    }
  }

  // 面几何：按真实外环投影成 SVG path（绝对坐标，外层不加 translate）
  function polygonPath(coords) {
    if (!Array.isArray(coords)) return '';
    const rings = Array.isArray(coords[0]) ? coords : [coords];
    const parts = [];
    for (const ring of rings) {
      if (!Array.isArray(ring) || ring.length < 3) continue;
      const vertices = ring
        .filter((pair) => Array.isArray(pair) && pair.length >= 2)
        .map((pair) => {
          const point = project(pair[0], pair[1]);
          return `${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
        });
      if (vertices.length >= 3) parts.push(`M${vertices.join('L')}Z`);
    }
    return parts.join('');
  }

  /* ---------------------------------------------------------------- 取数 */
  async function api(path) {
    const response = await fetch(path, { headers: { Accept: 'application/json' } });
    let payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok) {
      const message = (payload && (payload.error || payload.hint)) || `HTTP ${response.status}`;
      throw new Error(message);
    }
    return payload || {};
  }

  function queryString() {
    const params = new URLSearchParams();
    params.set('days', String(state.filters.days));
    if (state.filters.theme) params.set('theme', state.filters.theme);
    if (state.filters.country) params.set('country', state.filters.country);
    if (state.filters.minScore > 0) params.set('min_heat', String(state.filters.minScore));
    const layers = Object.keys(state.layers).filter((key) => state.layers[key]);
    if (layers.length) params.set('layers', layers.join(','));
    return params.toString();
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    setBusy(true);
    try {
      state.data = await api(`/api/radar/intel?${queryString()}`);
      state.error = null;
      const live = state.data.live_news || {};
      if (live.items && live.items.length) {
        state.live.items = live.items.slice();
        state.live.cursor = (live.cursor && live.cursor.since_id) || null;
        state.live.before = (live.cursor && live.cursor.before_id) || null;
        state.live.hasOlder = Boolean(live.cursor && live.cursor.has_more_older);
      }
      renderAll();
    } catch (err) {
      state.error = err.message || '加载失败';
      renderError();
    } finally {
      state.loading = false;
      setBusy(false);
    }
  }

  function setBusy(busy) {
    const button = $('#intelRefresh');
    if (button) {
      button.disabled = busy;
      button.textContent = busy ? '刷新中…' : '刷新';
    }
  }

  /* ------------------------------------------------------------ 客户端筛选 */
  // 所有面板都基于同一份 state.data，再叠加同一组客户端筛选，保证口径一致
  function filteredEvents() {
    const map = (state.data && state.data.map) || {};
    const events = map.events || [];
    return events.filter((event) => {
      if (state.filters.day && event.date !== state.filters.day) return false;
      if (state.filters.theme && event.theme !== state.filters.theme) return false;
      if (state.filters.country
        && String(event.country_code || '').toUpperCase() !== state.filters.country.toUpperCase()) {
        return false;
      }
      if (state.filters.minScore > 0 && (Number(event.severity) || 0) < state.filters.minScore) {
        return false;
      }
      const precision = state.filters.precision || 'all';
      if (precision === 'reliable' && !['high', 'medium'].includes(event.geo_confidence)) return false;
      if (precision === 'approx' && event.geo_confidence !== 'low') return false;
      return true;
    });
  }

  function filteredFeed() {
    const feed = (state.data && state.data.intelligence_feed) || {};
    return (feed.items || []).filter((item) => {
      if (state.filters.day && item.date !== state.filters.day) return false;
      if (state.filters.theme && item.theme !== state.filters.theme) return false;
      if (state.filters.country
        && String(item.country_code || '').toUpperCase() !== state.filters.country.toUpperCase()) {
        return false;
      }
      if (state.filters.minScore > 0 && (Number(item.heat) || 0) < state.filters.minScore) {
        return false;
      }
      return true;
    });
  }

  function filteredPoints() {
    const map = (state.data && state.data.map) || {};
    return (map.geo_points || []).filter((point) => {
      const hazard = point.hazard || 'other';
      if (state.hazards[hazard] === false) return false;
      if (state.filters.day
        && String(point.observed_at || '').slice(0, 10) !== state.filters.day) {
        return false;
      }
      return true;
    });
  }

  /* ------------------------------------------------------------ 顶部指标 */
  function renderMetrics() {
    const box = $('#intelMetrics');
    if (!box || !state.data) return;
    const strategic = state.data.strategic || {};
    const activity = (strategic.observed_activity || {}).score;
    const today = state.data.today || {};
    const events = filteredEvents();
    const highPriority = events.filter((event) => (Number(event.severity) || 0) >= 70).length;
    const located = events.length;
    const coverage = (state.data.coverage || {});
    const sources = (strategic.monitoring || {}).sources || {};
    const healthy = Number.isFinite(sources.ok) && Number.isFinite(sources.total)
      ? `${sources.ok}/${sources.total}` : '暂不可用';

    const metric = (label, value, hint) => `
      <div class="intel-metric">
        <span class="intel-metric-k">${esc(label)}</span>
        <b class="intel-metric-v">${value}</b>
        <span class="intel-metric-h">${esc(hint || '')}</span>
      </div>`;

    box.innerHTML = [
      metric('观测到的威胁活动',
        Number.isFinite(activity) ? activity.toFixed(1) : '数据不足',
        activity == null ? '缺少强度输入' : (today.caliber_version === 0 ? '反推口径，仅供参考' : '固定口径')),
      metric('高优先级事件', String(highPriority), '严重度 ≥ 70'),
      metric('已定位事件', String(located), `窗口内共 ${((state.data.window) || {}).days || state.filters.days} 天`),
      metric('来源健康', healthy, sources.paused ? `${sources.paused} 个源已停用` : '无停用源'),
      metric('数据更新', fmtTime(coverage.last_collect_at || state.data.generated_at), '本地采集时间'),
    ].join('');
  }

  /* ------------------------------------------------------------ 地图 */
  function mapTransform() {
    const zoom = state.map.zoom;
    const cx = MAP_W / 2, cy = MAP_H / 2;
    const tx = cx - zoom * cx + state.map.x;
    const ty = cy - zoom * cy + state.map.y;
    return `translate(${tx.toFixed(2)},${ty.toFixed(2)}) scale(${zoom.toFixed(3)})`;
  }

  // 把视野限制在地图范围内，避免拖到"什么都没有"的地方
  function clampPan() {
    const zoom = state.map.zoom;
    const limitX = Math.max(0, (zoom - 1) * MAP_W / 2);
    const limitY = Math.max(0, (zoom - 1) * MAP_H / 2);
    state.map.x = Math.max(-limitX, Math.min(limitX, state.map.x));
    state.map.y = Math.max(-limitY, Math.min(limitY, state.map.y));
  }

  // 把某个投影坐标点移到视野正中（情报卡/聚合气泡的"定位"都用它）
  function centerOn(x, y, zoom) {
    const next = Math.max(1, Math.min(8, Number(zoom) || state.map.zoom));
    state.map.zoom = next;
    state.map.x = next * (MAP_W / 2 - Number(x));
    state.map.y = next * (MAP_H / 2 - Number(y));
    clampPan();
  }

  // 放大到聚合气泡覆盖的区域：缩放级别跨过聚类阈值后，成员会展开成独立点位
  function zoomInto(x, y) {
    centerOn(x, y, Math.max(2.6, state.map.zoom * 1.8));
    renderMap();
  }

  function themeColor(theme) {
    switch (theme) {
      case 'conflict': return 'var(--fb-danger)';
      case 'unrest': return 'var(--fb-warn)';
      case 'security': return 'var(--fb-danger)';
      case 'disaster': return 'var(--fb-info)';
      case 'politics': return 'var(--fb-accent)';
      case 'cyber': return '#7c5cff';
      case 'technology': return 'var(--fb-success)';
      case 'infrastructure': return 'var(--fb-warn)';
      default: return 'var(--fb-text-3, #8792a2)';
    }
  }

  function clusterEvents(events, zoom) {
    // 缩小时按网格聚合；放大后逐个显示。始终保留真实 event_id 列表。
    const cell = zoom < 1.6 ? 140 : (zoom < THEME_UNCLUSTER_ZOOM ? 70 : 0);
    if (!cell) {
      return events.map((event) => ({ members: [event] }));
    }
    const buckets = new Map();
    for (const event of events) {
      const point = project(event.lon, event.lat);
      const key = `${Math.floor(point.x / cell)}:${Math.floor(point.y / cell)}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(event);
    }
    return Array.from(buckets.values()).map((members) => ({ members }));
  }

  function renderMap() {
    const host = $('#intelMap');
    if (!host) return;
    const world = window.FB_WORLD || null;
    const events = filteredEvents();
    const points = filteredPoints();
    const selected = state.map.selected;

    const dots = [];
    for (const cluster of clusterEvents(events, state.map.zoom)) {
      const members = cluster.members;
      const lead = members[0];
      const pos = project(lead.lon, lead.lat);
      if (members.length === 1) {
        const severity = Number(lead.severity) || 0;
        const radius = 5 + Math.min(1, severity / 100) * 9;
        const active = selected && (selected.event_id === lead.event_id);
        const conf = lead.geo_confidence || 'none';
        const strokeColor = active ? '#fff' : 'rgba(0,0,0,.35)';
        const strokeWidth = active ? 2.5 : 1;
        let shape;
        if (conf === 'low') {
          // 国家级近似：菱形 + 虚线 + 半透明 —— 形状本身就在提示"这里不精确"
          shape = `<rect x="${(-radius).toFixed(1)}" y="${(-radius).toFixed(1)}" `
            + `width="${(radius * 2).toFixed(1)}" height="${(radius * 2).toFixed(1)}" `
            + `transform="rotate(45)" fill="${themeColor(lead.theme)}" fill-opacity="0.5" `
            + `stroke="${strokeColor}" stroke-width="${strokeWidth}" stroke-dasharray="4 3"></rect>`;
        } else if (conf === 'high') {
          // 来源坐标：实心圆 + 外环
          shape = `<circle r="${radius.toFixed(1)}" fill="${themeColor(lead.theme)}" `
            + `stroke="${strokeColor}" stroke-width="${strokeWidth}"></circle>`
            + `<circle r="${(radius + 3.5).toFixed(1)}" fill="none" `
            + `stroke="${themeColor(lead.theme)}" stroke-width="1.2" opacity="0.85"></circle>`;
        } else {
          // 地名匹配（中置信）：实心圆
          shape = `<circle r="${radius.toFixed(1)}" fill="${themeColor(lead.theme)}" `
            + `stroke="${strokeColor}" stroke-width="${strokeWidth}"></circle>`;
        }
        dots.push(
          `<g class="intel-dot is-${esc(conf)}${active ? ' is-active' : ''}" `
          + `data-event="${esc(lead.event_id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})" `
          + `tabindex="0" role="button" aria-label="${esc(lead.title)}">`
          + shape
          + `<title>${esc(lead.title)}｜${esc(THEME_LABELS[lead.theme] || lead.theme)}｜`
          + `${esc(lead.geo_label || '')}（${esc(COMPASS_LABELS[conf] || '')}）</title>`
          + `</g>`);
      } else {
        const top = Math.max(...members.map((m) => Number(m.severity) || 0));
        const radius = 12 + Math.min(1, top / 100) * 8 + Math.min(10, members.length);
        // 聚合气泡必须可交互：点击放大到成员展开为止，否则就是一片死区
        dots.push(
          `<g class="intel-cluster" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})" `
          + `data-cx="${pos.x.toFixed(1)}" data-cy="${pos.y.toFixed(1)}" `
          + `data-count="${members.length}" tabindex="0" role="button" `
          + `aria-label="${members.length} 个事件，点击放大">`
          + `<circle r="${radius.toFixed(1)}" fill="var(--fb-surface)" stroke="${themeColor(lead.theme)}" `
          + `stroke-width="2" opacity="0.92"></circle>`
          + `<text text-anchor="middle" dy="4" font-size="${Math.max(11, radius * 0.9).toFixed(0)}" `
          + `fill="currentColor">${members.length}</text>`
          + `<title>${members.length} 个事件（点击放大查看）</title>`
          + `</g>`);
      }
    }

    const quakes = [];
    for (const point of points) {
      if (!validCoord(Number(point.latitude), Number(point.longitude))) continue;
      const hazard = point.hazard || 'other';
      const color = hazardColor(hazard);
      const isPolygon = String(point.geometry_type || '').toLowerCase() === 'polygon'
        && Array.isArray(point.geometry_json);
      let shape = '';
      if (isPolygon) {
        // 面几何：按真实范围绘制，**不**用中心点冒充精确位置
        const d = polygonPath(point.geometry_json);
        if (d) {
          shape = `<path class="intel-hazard-area" d="${d}" fill="${color}" fill-opacity="0.22" `
            + `stroke="${color}" stroke-width="1.2" stroke-opacity="0.9"></path>`;
        }
      }
      if (!shape) {
        const mag = Number(point.magnitude) || 0;
        const radius = (3 + Math.max(0, Math.min(1, mag / 7)) * 8).toFixed(1);
        shape = `<circle r="${radius}" fill="none" stroke="${color}" stroke-width="1.6" `
          + `opacity="0.9"></circle><circle r="1.6" fill="${color}"></circle>`;
      }
      const pos = project(point.longitude, point.latitude);
      // 面几何已经是绝对坐标，不能再套 translate
      const transform = isPolygon ? '' : `transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})"`;
      quakes.push(
        `<g class="intel-quake is-${esc(hazard)}" data-geo="${esc(point.layer)}|${esc(point.external_id)}" `
        + `${transform} tabindex="0" role="button" `
        + `aria-label="${esc(point.title || '')}">${shape}`
        + `<title>${esc(point.title || '')}｜${esc(HAZARD_LABELS[hazard] || hazard)}｜`
        + `${esc(String(point.observed_at || '').slice(0, 16).replace('T', ' '))}`
        + `${isPolygon ? '｜面范围（非精确中心）' : ''}</title></g>`);
    }

    host.innerHTML = `
      <svg class="intel-map-svg" viewBox="0 0 ${MAP_W} ${MAP_H}" role="img"
           aria-label="全球事件地图" preserveAspectRatio="xMidYMid meet">
        <rect x="0" y="0" width="${MAP_W}" height="${MAP_H}" class="intel-ocean"></rect>
        <g class="intel-viewport" transform="${mapTransform()}">
          ${world ? `<path class="intel-land" d="${world.paths}"></path>` : ''}
          ${world ? `<path class="intel-borders" d="${world.paths}"></path>` : ''}
          <g class="intel-layer-quakes">${quakes.join('')}</g>
          <g class="intel-layer-news">${dots.join('')}</g>
        </g>
      </svg>
      ${world ? '' : '<p class="intel-map-warn">底图未加载（/world-110m.js 缺失）：仍可显示点位，但缺少陆地轮廓。</p>'}
    `;

    const count = $('#intelMapCount');
    if (count) {
      const hazards = {};
      for (const point of points) {
        const key = point.hazard || 'other';
        hazards[key] = (hazards[key] || 0) + 1;
      }
      const parts = Object.keys(hazards).map((key) => `${HAZARD_LABELS[key] || key} ${hazards[key]}`);
      count.textContent = `新闻 ${events.length} ｜ 灾害 ${points.length}`
        + (parts.length ? `（${parts.join('、')}）` : '') + ' ｜ 未定位事件不显示';
    }

    bindMapEvents();
  }

  function bindMapEvents() {
    const host = $('#intelMap');
    if (!host) return;
    host.querySelectorAll('[data-event]').forEach((node) => {
      const open = () => selectEvent(node.getAttribute('data-event'));
      node.addEventListener('click', open);
      node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
      });
    });

    host.querySelectorAll('[data-geo]').forEach((node) => {
      const open = () => {
        const raw = node.getAttribute('data-geo') || '';
        const [layer, externalId] = raw.split('|');
        selectGeoPoint(layer, externalId);
      };
      node.addEventListener('click', open);
      node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
      });
    });

    host.querySelectorAll('.intel-cluster').forEach((node) => {
      const zoom = () => zoomInto(Number(node.getAttribute('data-cx')),
                                  Number(node.getAttribute('data-cy')));
      node.addEventListener('click', zoom);
      node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); zoom(); }
      });
    });

    const svg = host.querySelector('svg');
    if (!svg) return;
    let start = null;
    svg.addEventListener('pointerdown', (event) => {
      if (event.target.closest('[data-event]')) return;
      start = { x: event.clientX, y: event.clientY, px: state.map.x, py: state.map.y };
      state.map.dragging = true;
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener('pointermove', (event) => {
      if (!start || !state.map.dragging) return;
      const scale = MAP_W / svg.getBoundingClientRect().width;
      state.map.x = start.px + (event.clientX - start.x) * scale;
      state.map.y = start.py + (event.clientY - start.y) * scale;
      clampPan();
      const viewport = svg.querySelector('.intel-viewport');
      if (viewport) viewport.setAttribute('transform', mapTransform());
    });
    const stop = (event) => {
      if (!state.map.dragging) return;
      state.map.dragging = false;
      start = null;
      if (event && event.pointerId != null && svg.hasPointerCapture(event.pointerId)) {
        svg.releasePointerCapture(event.pointerId);
      }
      renderMap();
    };
    svg.addEventListener('pointerup', stop);
    svg.addEventListener('pointercancel', stop);
    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.25 : 0.8);
    }, { passive: false });
    svg.addEventListener('dblclick', () => zoomBy(1.6));
  }

  function resetZoom() {
    state.map.zoom = 1;
    state.map.x = 0;
    state.map.y = 0;
    renderMap();
  }

  function zoomBy(factor) {
    const next = Math.max(1, Math.min(8, state.map.zoom * factor));
    if (next !== state.map.zoom) {
      state.map.zoom = next;
      if (next === 1) { state.map.x = 0; state.map.y = 0; }
      renderMap();
    }
  }

  function selectEvent(eventId) {
    const events = filteredEvents();
    const event = events.find((item) => item.event_id === eventId) || null;
    state.map.selected = event;
    state.selectedPoint = null;
    if (event && validCoord(Number(event.lat), Number(event.lon))) {
      // "定位"必须真的把点带进视野：缩放到成员展开、并居中，
      // 否则选中的点可能藏在聚合气泡里，看起来像没反应。
      const point = project(event.lon, event.lat);
      const zoom = Math.max(state.map.zoom, THEME_UNCLUSTER_ZOOM);
      centerOn(point.x, point.y, zoom);
    }
    renderMap();
    renderDetail();
    if (event) {
      const card = document.querySelector(`[data-feed-event="${event.event_id}"]`);
      if (card) {
        card.classList.add('is-linked');
        card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        setTimeout(() => card.classList.remove('is-linked'), 2200);
      }
    }
  }

  function selectGeoPoint(layer, externalId) {
    const point = filteredPoints().find(
      (item) => item.layer === layer && String(item.external_id) === String(externalId)) || null;
    state.selectedPoint = point;
    state.map.selected = null;
    if (point && validCoord(Number(point.latitude), Number(point.longitude))) {
      const projected = project(point.longitude, point.latitude);
      centerOn(projected.x, projected.y, Math.max(state.map.zoom, THEME_UNCLUSTER_ZOOM));
    }
    renderMap();
    renderDetail();
  }

  function renderDetail() {
    const box = $('#intelDetail');
    if (!box) return;

    const point = state.selectedPoint;
    if (point) {
      const hazard = point.hazard || 'other';
      const layer = (((state.data || {}).map || {}).layers || [])
        .find((item) => item.key === point.layer) || {};
      const precision = point.position_precision || 'point';
      const related = Array.isArray(point.related_sources) ? point.related_sources : [];
      const relatedHtml = related.length
        ? `<p class="intel-warn">疑似同一事件（未自动合并）：`
          + related.map((item) => `${esc(item.layer)} · ${esc(item.title || item.external_id)}`
            + `（相距约 ${esc(item.distance_km)} km）`).join('；')
          + '</p>'
        : '';
      box.innerHTML = `
        <div class="intel-detail-head">
          <h4>${esc(point.title || '未命名事件')}</h4>
          <button type="button" class="intel-close" id="intelDetailClose" aria-label="关闭">×</button>
        </div>
        <dl class="intel-kv">
          <dt>灾害类型</dt><dd>${esc(HAZARD_LABELS[hazard] || hazard)}</dd>
          <dt>事件时间</dt><dd>${esc(String(point.observed_at || '—').slice(0, 16).replace('T', ' '))}</dd>
          <dt>数据来源</dt><dd>${esc(layer.label || point.layer)}
            <small>${esc(layer.source || point.source_id || '')}</small></dd>
          <dt>位置精度</dt><dd>${esc(PRECISION_LABELS[precision] || precision)}
            <small>${esc(point.geometry_type || '')}</small></dd>
          <dt>数量级</dt><dd>${point.magnitude == null ? '—'
            : `${Number(point.magnitude).toFixed(1)} ${esc(point.magnitude_unit || '')}`}</dd>
          <dt>等级</dt><dd>${esc(point.severity || '—')}</dd>
          <dt>最后更新</dt><dd>${esc(String(point.updated_at || point.collected_at || '—').slice(0, 16).replace('T', ' '))}</dd>
          ${point.closed_at ? `<dt>结束时间</dt><dd>${esc(String(point.closed_at).slice(0, 16).replace('T', ' '))}</dd>` : ''}
        </dl>
        ${precision === 'area'
          ? '<p class="intel-warn">这是**面范围**数据：地图按多边形绘制，中心点仅用于定位视野，'
            + '不代表精确事发点。</p>'
          : ''}
        ${relatedHtml}
        ${point.url ? `<p class="intel-small"><a href="${esc(point.url)}" target="_blank" `
          + `rel="noopener noreferrer">查看原始来源</a></p>` : ''}
        <p class="intel-small">采集于 ${esc(String(point.collected_at || '').slice(0, 16).replace('T', ' '))}</p>
      `;
      const close = $('#intelDetailClose');
      if (close) close.addEventListener('click', () => {
        state.selectedPoint = null;
        renderMap();
        renderDetail();
      });
      return;
    }

    const event = state.map.selected;
    if (!event) {
      box.innerHTML = '<p class="intel-empty">点击地图点位或情报卡片查看事件详情。</p>';
      return;
    }
    const geoNote = event.geo_confidence === 'low'
      ? '<p class="intel-warn">国家级近似位置：只确认了国家，坐标取国家质心，不是事件精确地点。</p>'
      : '';
    box.innerHTML = `
      <div class="intel-detail-head">
        <h4>${esc(event.title)}</h4>
        <button type="button" class="intel-close" id="intelDetailClose" aria-label="关闭">×</button>
      </div>
      <dl class="intel-kv">
        <dt>主题</dt><dd>${esc(THEME_LABELS[event.theme] || event.theme)}</dd>
        <dt>位置</dt><dd>${esc(event.geo_label || '—')}
          ${event.country_name ? '· ' + esc(event.country_name) : ''}</dd>
        <dt>定位方式</dt><dd>${esc(COMPASS_LABELS[event.geo_confidence] || '')}
          <small>${esc(event.geo_method || '')}</small></dd>
        <dt>严重度</dt><dd>${Number.isFinite(Number(event.severity)) ? Number(event.severity).toFixed(1) : '—'}</dd>
        <dt>可信度</dt><dd>${esc(event.confidence || '—')}</dd>
        <dt>材料</dt><dd>${event.item_count || 0} 条 · ${event.platform_count || 0} 个平台</dd>
        <dt>时间</dt><dd>${esc(fmtTime(event.first_seen_at))}</dd>
        <dt>依据</dt><dd><small>${esc(event.geo_source || '—')}</small></dd>
      </dl>
      ${geoNote}
      <button type="button" class="intel-action" id="intelDetailInvestigate"
              data-event="${esc(event.event_id)}">深入调查</button>
    `;
    const close = $('#intelDetailClose');
    if (close) close.addEventListener('click', () => { state.map.selected = null; renderMap(); renderDetail(); });
    const investigate = $('#intelDetailInvestigate');
    if (investigate) {
      investigate.addEventListener('click', () => {
        const input = $('#investigateInput');
        const form = $('#investigateForm');
        if (input && form) {
          input.value = event.title;
          form.dispatchEvent(new Event('submit', { cancelable: true }));
          document.querySelector('.radar-tab[data-view="investigate"]')?.click();
        }
      });
    }
  }

  function worldSource() {
    const world = window.FB_WORLD || {};
    return world.source ? `${world.source} / ${world.license || '公共领域'}` : 'Natural Earth（公共领域）';
  }

  function renderLegend() {
    const box = $('#intelLegend');
    if (!box) return;
    const layers = ((state.data && state.data.map) || {}).layers || [];
    const hazardGroups = ((state.data && state.data.map) || {}).hazard_groups || [];
    const hazardCounts = (((state.data || {}).map || {}).counts || {}).by_hazard || {};
    const items = Object.keys(THEME_LABELS)
      .filter((key) => key !== 'unknown')
      .map((key) => `<span class="intel-legend-item"><i style="background:${themeColor(key)}"></i>${esc(THEME_LABELS[key])}</span>`)
      .join('');
    // 灾害按**类型**开关（地震/火山/野火/洪水/风暴/其他），不是按数据源开关；
    // 只有真正接入且计数 > 0 的类型才可勾选，未接入的显示原因。
    const hazardRows = hazardGroups.map((group) => {
      const count = Number(hazardCounts[group.key] || 0);
      const usable = count > 0;
      return `
      <label class="intel-layer${usable ? '' : ' is-off'}">
        <input type="checkbox" data-hazard="${esc(group.key)}"
               ${state.hazards[group.key] !== false ? 'checked' : ''} ${usable ? '' : 'disabled'}>
        <i class="intel-hazard-dot" style="background:${hazardColor(group.key)}"></i>
        <span>${esc(HAZARD_LABELS[group.key] || group.label)}</span>
        <b>${usable ? count : '无数据'}</b>
      </label>`;
    }).join('');
    const sourceRows = layers.map((layer) => `
      <span class="intel-source-item${layer.connected ? '' : ' is-off'}"
            title="${esc(layer.note || '')}">
        ${esc(layer.label)} · ${layer.connected ? `已接入 ${layer.count}` : '未接入'}
        <small>${esc(layer.license || '')}</small>
      </span>`).join('');
    box.innerHTML = `
      <div class="intel-legend-row">
        <span class="intel-legend-item"><i class="intel-shape-high"></i>来源坐标（高）</span>
        <span class="intel-legend-item"><i class="intel-shape-medium"></i>地名匹配（中）</span>
        <span class="intel-legend-item"><i class="intel-shape-low"></i>国家级近似（低）</span>
      </div>
      <div class="intel-legend-row">${items}</div>
      <div class="intel-layers">${hazardRows}</div>
      <div class="intel-sources">${sourceRows}</div>
      <p class="intel-attribution">
        底图：${esc(worldSource())}（公共领域）· 灾害数据：USGS / NASA EONET / GDACS（各自条款见文档）·
        点位仅表示"报道中的位置或影响范围"，菱形为国家级近似，多边形为面范围而非精确中心。
      </p>
    `;
    box.querySelectorAll('input[data-hazard]').forEach((input) => {
      input.addEventListener('change', () => {
        state.hazards[input.getAttribute('data-hazard')] = input.checked;
        renderMap();
        renderLegend();
      });
    });
  }

  /* ------------------------------------------------------------ 威胁时间线 */
  function renderTimeline() {
    const box = $('#intelTimeline');
    if (!box || !state.data) return;
    const timeline = state.data.threat_timeline || {};
    const points = timeline.points || [];
    const width = 560, height = 150, pad = 26;
    const active = timeline.latest || {};
    const dayKey = state.filters.day;

    const step = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
    const yOf = (value) => height - pad - (Math.max(0, Math.min(100, value)) / 100) * (height - pad * 2);

    const activityPath = [];
    const credibilityPath = [];
    let activitySeen = false, credibilitySeen = false;
    const dots = [];
    points.forEach((point, index) => {
      const x = pad + step * index;
      if (point.available && point.activity != null) {
        activityPath.push(`${activitySeen ? 'L' : 'M'}${x.toFixed(1)},${yOf(point.activity).toFixed(1)}`);
        activitySeen = true;
      }
      if (point.available && point.credibility != null) {
        credibilityPath.push(`${credibilitySeen ? 'L' : 'M'}${x.toFixed(1)},${yOf(point.credibility).toFixed(1)}`);
        credibilitySeen = true;
      }
      if (point.available) {
        dots.push(`<g class="intel-tl-dot" data-day="${esc(point.date)}">`
          + `<circle cx="${x.toFixed(1)}" cy="${yOf(point.activity || 0).toFixed(1)}" r="4" `
          + `class="${point.caliber_version === 0 ? 'is-legacy' : ''}"></circle>`
          + `<title>${esc(point.date)}｜活动 ${point.activity == null ? '—' : point.activity}`
          + `｜可信度 ${point.credibility == null ? '—' : point.credibility}`
          + `｜口径 ${point.caliber_version}｜事件 ${point.event_count || 0}</title></g>`);
      }
      dots.push(`<text class="intel-tl-x" x="${x.toFixed(1)}" y="${height - 6}" `
        + `text-anchor="middle">${esc(String(point.date).slice(5))}</text>`);
    });

    const chart = `
      <svg viewBox="0 0 ${width} ${height}" class="intel-tl-svg" role="img" aria-label="过去七天威胁趋势">
        <line x1="${pad}" y1="${yOf(0)}" x2="${width - pad}" y2="${yOf(0)}" class="intel-tl-axis"></line>
        <line x1="${pad}" y1="${yOf(50)}" x2="${width - pad}" y2="${yOf(50)}" class="intel-tl-grid"></line>
        <line x1="${pad}" y1="${yOf(100)}" x2="${width - pad}" y2="${yOf(100)}" class="intel-tl-grid"></line>
        <path d="${activityPath.join(' ')}" class="intel-tl-activity"></path>
        <path d="${credibilityPath.join(' ')}" class="intel-tl-credibility"></path>
        ${dots.join('')}
      </svg>`;

    const confidence = timeline.history_confidence || 'none';
    const notes = (timeline.notes || []).map((note) => `<li>${esc(note)}</li>`).join('');
    const confidenceText = {
      none: '暂无可用历史', insufficient: '历史深度不足', fair: '历史偏短', ok: '历史完整',
    }[confidence] || confidence;

    box.innerHTML = `
      <div class="intel-panel-head">
        <h3>Threat Timeline</h3>
        <span class="intel-badge is-${esc(confidence)}">${esc(confidenceText)}</span>
      </div>
      <div class="intel-tl-body">
        <div class="intel-tl-side">
          <div class="intel-big">${active.activity == null ? '数据不足' : Number(active.activity).toFixed(1)}</div>
          <div class="intel-k">威胁活动（0–100）</div>
          <div class="intel-small">可信度 ${active.credibility == null ? '数据不足' : Number(active.credibility).toFixed(1)}</div>
          <div class="intel-small">可用 ${timeline.history_days || 0}/${timeline.requested_days || state.filters.days} 天</div>
          <div class="intel-small">可比性：${esc({uniform: '统一口径', mixed: '混合口径', legacy_only: '仅反推口径', none: '无'}[timeline.comparability] || '—')}</div>
        </div>
        <div class="intel-tl-chart">${chart}</div>
      </div>
      <div class="intel-legend-row">
        <span class="intel-legend-item"><i class="intel-line-activity"></i>威胁活动</span>
        <span class="intel-legend-item"><i class="intel-line-credibility"></i>情报可信度</span>
        <span class="intel-legend-item"><i class="intel-swatch-dashed"></i>反推口径</span>
      </div>
      ${notes ? `<ul class="intel-notes">${notes}</ul>` : ''}
      ${dayKey ? `<p class="intel-small">已按 ${esc(dayKey)} 筛选地图与情报流
        <button type="button" class="intel-link" id="intelClearDay">清除</button></p>` : ''}
    `;

    box.querySelectorAll('.intel-tl-dot').forEach((node) => {
      node.addEventListener('click', () => {
        const day = node.getAttribute('data-day');
        state.filters.day = state.filters.day === day ? '' : day;
        renderAll();
      });
    });
    const clear = $('#intelClearDay');
    if (clear) clear.addEventListener('click', () => { state.filters.day = ''; renderAll(); });
  }

  /* ------------------------------------------------------------ 战略风险 */
  function renderStrategic() {
    const box = $('#intelStrategic');
    if (!box || !state.data) return;
    const strategic = state.data.strategic || {};
    const estimate = strategic.estimate || {};
    const coverage = strategic.monitoring || {};
    const rows = [];

    const bar = (label, value, weights, extra) => {
      const pct = value == null ? 0 : Math.max(0, Math.min(100, Number(value) * (value <= 1 ? 100 : 1)));
      return `<div class="intel-bar-row">
        <span class="intel-bar-k">${esc(label)}</span>
        <span class="intel-bar"><i style="width:${pct.toFixed(1)}%"></i></span>
        <span class="intel-bar-v">${value == null ? '—' : Number(value).toFixed(2)}</span>
        <span class="intel-bar-w">${weights == null ? '' : '权重 ' + Number(weights).toFixed(2)}</span>
        ${extra ? `<span class="intel-bar-x">${esc(extra)}</span>` : ''}
      </div>`;
    };

    const components = estimate.components || {};
    const weights = estimate.weights || {};
    for (const key of Object.keys(components)) {
      const labelMap = {
        observed_activity: '观测到的活动', evidence_uncertainty: '证据不确定',
        monitoring_blind_spot: '监控盲区',
      };
      rows.push(bar(labelMap[key] || key, components[key], weights[key]));
    }

    const missing = (estimate.missing_inputs || []).map((item) => `<li>${esc(item)}</li>`).join('');
    box.innerHTML = `
      <div class="intel-panel-head">
        <h3>Strategic Risk</h3>
        <span class="intel-badge is-${estimate.is_estimate ? 'fair' : 'ok'}">估算值</span>
      </div>
      <div class="intel-big">${estimate.score == null ? '数据不足' : Number(estimate.score).toFixed(1)}</div>
      <p class="intel-small">${esc(estimate.label || '')}</p>
      <div class="intel-bars">${rows.join('') || '<p class="intel-empty">没有可用分项</p>'}</div>
      <dl class="intel-kv">
        <dt>观测活动</dt><dd>${(strategic.observed_activity || {}).score == null ? '数据不足'
          : Number(strategic.observed_activity.score).toFixed(1)}
          <small>${esc((strategic.observed_activity || {}).basis || '')}</small></dd>
        <dt>情报可信度</dt><dd>${(strategic.credibility || {}).score == null ? '数据不足'
          : Number(strategic.credibility.score).toFixed(1)}</dd>
        <dt>监控覆盖</dt><dd>${coverage.coverage_score == null ? '数据不足'
          : Number(coverage.coverage_score).toFixed(1)}
          <small>源 ${((coverage.sources || {}).ok || 0)}/${((coverage.sources || {}).total || 0)}</small></dd>
        <dt>生成时间</dt><dd>${esc(fmtTime(state.data.generated_at))}</dd>
      </dl>
      ${missing ? `<ul class="intel-notes">缺失输入：${missing}</ul>` : ''}
      <ul class="intel-notes">${(strategic.notes || []).slice(0, 3).map((n) => `<li>${esc(n)}</li>`).join('')}</ul>
    `;
  }

  /* ------------------------------------------------------------ 国家面板 */
  function renderCountries() {
    const box = $('#intelCountries');
    if (!box || !state.data) return;
    const block = state.data.country_index || {};
    if (!block.available) {
      box.innerHTML = `<div class="intel-panel-head"><h3>Country Index</h3>
        <span class="intel-badge is-insufficient">不可用</span></div>
        <p class="intel-empty">${esc(block.reason || '没有足够的地理数据')}</p>
        <ul class="intel-notes">${(block.missing_inputs || []).map((m) => `<li>缺失输入：${esc(m)}</li>`).join('')}</ul>`;
      return;
    }
    const rows = (block.countries || []).map((row) => `
      <tr data-country="${esc(row.code)}" class="${row.low_sample ? 'is-low-sample' : ''}">
        <td>${esc(row.name)}</td>
        <td>${row.score == null ? '<span class="intel-muted">样本不足</span>' : Number(row.score).toFixed(1)}</td>
        <td>${row.counts.conflict}</td>
        <td>${row.counts.unrest}</td>
        <td>${row.counts.disaster}</td>
        <td>${row.event_count}</td>
      </tr>`).join('');
    box.innerHTML = `
      <div class="intel-panel-head"><h3>Country Index</h3>
        <span class="intel-badge is-fair">部分可用</span></div>
      <p class="intel-small">${esc(block.semantics || '')}</p>
      <div class="intel-table-wrap">
        <table class="intel-table">
          <thead><tr><th>国家</th><th>紧张度</th><th>冲突</th><th>动荡</th><th>灾害</th><th>事件</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6" class="intel-empty">窗口内没有已定位事件</td></tr>'}</tbody>
        </table>
      </div>
      <p class="intel-small">${esc(block.baseline_note || '')}
        地理编码覆盖 ${(block.geocode_coverage == null ? '—' : (block.geocode_coverage * 100).toFixed(1) + '%')}</p>
    `;
    box.querySelectorAll('tr[data-country]').forEach((row) => {
      row.addEventListener('click', () => {
        const code = row.getAttribute('data-country');
        state.filters.country = state.filters.country === code ? '' : code;
        const select = $('#intelCountry');
        if (select) select.value = state.filters.country;
        renderAll();
      });
    });
  }

  /* ------------------------------------------------------------ 情报流 */
  function renderFeed() {
    const box = $('#intelFeed');
    if (!box || !state.data) return;
    const feed = state.data.intelligence_feed || {};
    if (feed.pending) {
      box.innerHTML = `<div class="intel-panel-head"><h3>Intelligence Feed</h3></div>
        <p class="intel-empty">还没有日报数据，先运行一次采集。</p>`;
      return;
    }
    const items = filteredFeed();
    const cards = items.map((item) => {
      const geo = item.geo || null;
      const located = geo && geo.lat != null;
      return `
      <article class="intel-card${item.is_new ? ' is-new' : ''}" data-feed-event="${esc(item.event_id || '')}">
        <header>
          <span class="intel-chip">${esc(THEME_LABELS[item.theme] || item.theme || '综合')}</span>
          ${item.is_new ? '<span class="intel-chip is-new">NEW</span>' : ''}
          <span class="intel-chip is-ghost">${esc(item.region === 'cn' ? '国内' : (item.region === 'intl' ? '国际' : '地区未知'))}</span>
          <span class="intel-card-heat">${item.heat == null ? '' : Number(item.heat).toFixed(0)}</span>
        </header>
        <h4>${esc(item.headline || item.title || '')}</h4>
        <p>${esc((item.summary || '').slice(0, 220))}</p>
        <footer>
          <span>${esc(item.platforms ? item.platforms.slice(0, 3).join(' · ') : '')}</span>
          <span>${item.item_count || 0} 条材料</span>
          <span>${esc(item.confidence_label || item.confidence || '')}</span>
          <span>${esc(fmtTime(item.date))}</span>
          ${located ? `<span class="intel-muted">${esc(geo.label || '')}（${esc(COMPASS_LABELS[geo.confidence] || '')}）</span>` : '<span class="intel-muted">未定位</span>'}
        </footer>
        <div class="intel-card-actions">
          ${item.sources && item.sources[0] && item.sources[0].url
            ? `<a href="${esc(item.sources[0].url)}" target="_blank" rel="noopener noreferrer">原文</a>` : ''}
          <button type="button" class="intel-link" data-investigate="${esc(item.event_id || '')}"
                  data-title="${esc(item.title || '')}">深入调查</button>
          ${located ? `<button type="button" class="intel-link" data-locate="${esc(item.event_id || '')}">地图定位</button>` : ''}
        </div>
      </article>`;
    }).join('');

    box.innerHTML = `
      <div class="intel-panel-head">
        <h3>Intelligence Feed</h3>
        <span class="intel-small">${items.length} 条 · 报告 ${esc(feed.report_date || '—')}</span>
      </div>
      <div class="intel-cards">${cards || '<p class="intel-empty">当前筛选下没有情报条目</p>'}</div>
    `;

    box.querySelectorAll('[data-locate]').forEach((button) => {
      button.addEventListener('click', () => {
        selectEvent(button.getAttribute('data-locate'));
        const map = $('#intelMap');
        if (map) map.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
    });
    box.querySelectorAll('[data-investigate]').forEach((button) => {
      button.addEventListener('click', () => {
        const input = $('#investigateInput');
        const form = $('#investigateForm');
        if (input && form) {
          input.value = button.getAttribute('data-title') || '';
          form.dispatchEvent(new Event('submit', { cancelable: true }));
          document.querySelector('.radar-tab[data-view="investigate"]')?.click();
        }
      });
    });
  }

  /* ------------------------------------------------------------ 实时新闻 */
  function renderLive() {
    const box = $('#intelLive');
    if (!box) return;
    const items = state.live.items || [];
    const rows = items.map((item) => `
      <li class="intel-live-row${state.seen[item.id] ? ' is-seen' : ''}" data-live="${item.id}">
        <a href="${esc(item.url || '#')}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>
        <div class="intel-live-meta">
          <span>${esc(item.platform || '')}</span>
          <span>${item.published_at_missing ? '无发布时间' : esc(fmtTime(item.published_at))}</span>
          <span class="intel-muted">采集 ${esc(fmtTime(item.collected_at))}</span>
          ${item.is_trend_only ? '<span class="intel-chip is-ghost">热榜发现·无正文</span>' : ''}
        </div>
      </li>`).join('');
    box.innerHTML = `
      <div class="intel-panel-head">
        <h3>Live News</h3>
        <span class="intel-small" id="intelLiveState">${state.live.cursor ? '已同步' : '等待数据'}</span>
      </div>
      <ul class="intel-live">${rows || '<li class="intel-empty">暂无实时新闻</li>'}</ul>
      <div class="intel-live-actions">
        <button type="button" class="intel-link" id="intelLiveMore"
                ${state.live.hasOlder ? '' : 'disabled'}>加载更早</button>
        <span class="intel-small">每 ${LIVE_POLL_MS / 1000} 秒增量拉取；页面不可见时暂停</span>
      </div>
    `;
    box.querySelectorAll('[data-live]').forEach((row) => {
      row.addEventListener('click', () => {
        state.seen[row.getAttribute('data-live')] = true;
        row.classList.add('is-seen');
      });
    });
    const more = $('#intelLiveMore');
    if (more) more.addEventListener('click', loadOlder);
  }

  async function pollLive() {
    if (document.hidden || !state.live.cursor) return;
    try {
      const data = await api(`/api/radar/intel/live?since_id=${encodeURIComponent(state.live.cursor)}&limit=20`);
      const fresh = (data.items || []).filter((item) => !state.live.items.some((old) => old.id === item.id));
      if (fresh.length) {
        state.live.items = fresh.concat(state.live.items).slice(0, 200);
        renderLive();
        const state_ = $('#intelLiveState');
        if (state_) state_.textContent = `新增 ${fresh.length} 条`;
      }
      if (data.cursor && data.cursor.since_id) state.live.cursor = data.cursor.since_id;
    } catch (err) {
      const el = $('#intelLiveState');
      if (el) el.textContent = '增量拉取失败';
    }
  }

  async function loadOlder() {
    if (!state.live.before) return;
    try {
      const data = await api(`/api/radar/intel/live?before_id=${encodeURIComponent(state.live.before)}&limit=30`);
      const older = (data.items || []).filter((item) => !state.live.items.some((old) => old.id === item.id));
      state.live.items = state.live.items.concat(older);
      state.live.hasOlder = Boolean(data.cursor && data.cursor.has_more_older);
      if (data.cursor && data.cursor.before_id) state.live.before = data.cursor.before_id;
      renderLive();
    } catch (err) {
      const el = $('#intelLiveState');
      if (el) el.textContent = '加载更早失败';
    }
  }

  /* ------------------------------------------------------------ 提示与筛选条 */
  function renderNotice() {
    const box = $('#intelNotice');
    if (!box) return;
    if (state.error) {
      box.className = 'intel-notice is-error';
      box.innerHTML = `加载失败：${esc(state.error)} <button type="button" class="intel-link" id="intelRetry">重试</button>`;
      const retry = $('#intelRetry');
      if (retry) retry.addEventListener('click', () => load());
      return;
    }
    const degraded = (state.data && state.data.degraded) || [];
    if (!degraded.length) {
      box.className = 'intel-notice is-ok';
      box.textContent = `数据已同步 · ${fmtTime(state.data && state.data.generated_at)}`;
      return;
    }
    box.className = 'intel-notice is-warn';
    box.innerHTML = '部分模块处于降级状态：' + degraded
      .map((item) => `<b>${esc(item.module)}</b> ${esc(item.reason || '')}`).join('；');
  }

  function renderFilters() {
    const theme = $('#intelTheme');
    if (theme) {
      // 静态 HTML 里已经有一个"全部主题"选项，所以不能用 options.length 判断是否已填充。
      // 每次都重建（保留当前值），否则下拉里永远只有"全部主题"。
      const current = theme.value || state.filters.theme || '';
      theme.innerHTML = '<option value="">全部主题</option>' + Object.keys(THEME_LABELS)
        .filter((key) => key !== 'unknown')
        .map((key) => `<option value="${esc(key)}">${esc(THEME_LABELS[key])}</option>`).join('');
      theme.value = current;
    }
    const countries = $('#intelCountry');
    if (countries) {
      const block = (state.data && state.data.country_index) || {};
      const options = (block.countries || []).map((row) => [row.code, row.name]);
      const current = state.filters.country;
      countries.innerHTML = '<option value="">全部国家</option>'
        + options.map(([code, name]) => `<option value="${esc(code)}"${code === current ? ' selected' : ''}>${esc(name)}</option>`).join('');
      if (current && !options.some(([code]) => code === current)) {
        countries.innerHTML += `<option value="${esc(current)}" selected>${esc(current)}</option>`;
      }
    }
    const days = $('#intelDays');
    if (days) days.value = String(state.filters.days);
    const minScore = $('#intelMinScore');
    if (minScore) minScore.value = String(state.filters.minScore);
    const precision = $('#intelPrecision');
    if (precision) precision.value = state.filters.precision || 'all';
  }

  // 导出当前视图：CSV 里保留位置精度与定位方法，避免导出后"精度信息丢失"
  function exportView() {
    const events = filteredEvents();
    if (!events.length) {
      const notice = $('#intelNotice');
      if (notice) notice.textContent = '当前筛选下没有可导出的已定位事件';
      return;
    }
    const header = [
      'event_id', 'story_id', 'title', 'theme', 'region', 'country_code', 'country_name',
      'geo_label', 'geo_confidence', 'geo_method', 'severity', 'heat', 'item_count',
      'date', 'first_seen_at',
    ];
    const rows = events.map((event) => [
      event.event_id, event.story_id || '', event.title, event.theme, event.region,
      event.country_code || '', event.country_name || '', event.geo_label || '',
      event.geo_confidence, event.geo_method, event.severity, event.heat,
      event.item_count, event.date, event.first_seen_at,
    ].map((value) => `"${String(value == null ? '' : value).replace(/"/g, '""')}"`).join(','));
    const note = [
      '# 位置精度说明：high=来源坐标；medium=地名匹配；low=国家质心近似（不是精确事发点）；',
      `# 底图来源：${worldSource()}；导出时间：${new Date().toISOString()}；`,
      `# 筛选：${JSON.stringify(state.filters)}`,
    ].join('\n');
    const csv = `${note}\n${header.join(',')}\n${rows.join('\n')}\n`;
    const blob = new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `global-intel-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function bindFilters() {
    const days = $('#intelDays');
    if (days) days.addEventListener('change', () => {
      state.filters.days = toInt(days.value, 7);
      load();
    });
    const theme = $('#intelTheme');
    if (theme) theme.addEventListener('change', () => {
      state.filters.theme = theme.value;
      renderAll();
    });
    const country = $('#intelCountry');
    if (country) country.addEventListener('change', () => {
      state.filters.country = country.value;
      renderAll();
    });
    const minScore = $('#intelMinScore');
    if (minScore) minScore.addEventListener('change', () => {
      state.filters.minScore = toInt(minScore.value, 0);
      renderAll();
    });
    const reset = $('#intelReset');
    if (reset) reset.addEventListener('click', () => {
      state.filters = { days: 7, theme: '', country: '', minScore: 0, precision: 'all', day: '' };
      state.map.selected = null;
      renderFilters();
      load();
    });
    const precision = $('#intelPrecision');
    if (precision) precision.addEventListener('change', () => {
      state.filters.precision = precision.value;
      renderAll();
    });
    const exportBtn = $('#intelExport');
    if (exportBtn) exportBtn.addEventListener('click', exportView);
    const refresh = $('#intelRefresh');
    if (refresh) refresh.addEventListener('click', () => load());
    const zoomIn = $('#intelZoomIn');
    if (zoomIn) zoomIn.addEventListener('click', () => zoomBy(1.4));
    const zoomOut = $('#intelZoomOut');
    if (zoomOut) zoomOut.addEventListener('click', () => zoomBy(0.7));
    const zoomReset = $('#intelZoomReset');
    if (zoomReset) zoomReset.addEventListener('click', resetZoom);
  }

  /* ------------------------------------------------------------ 总装 */
  function renderAll() {
    if (!state.data) return;
    renderNotice();
    renderFilters();
    renderMetrics();
    renderMap();
    renderLegend();
    renderDetail();
    renderTimeline();
    renderStrategic();
    renderCountries();
    renderFeed();
    renderLive();
  }

  function renderError() {
    renderNotice();
    ['#intelMetrics', '#intelTimeline', '#intelStrategic', '#intelCountries', '#intelFeed'].forEach((sel) => {
      const node = $(sel);
      if (node) node.innerHTML = `<p class="intel-empty">${esc(state.error || '加载失败')}</p>`;
    });
  }

  function ensureLoaded() {
    if (!state.data && !state.loading) {
      load();
    } else if (state.data) {
      renderAll();
    }
    if (!state.live.timer) {
      state.live.timer = setInterval(() => {
        if (document.hidden) return;
        if (!$('#view-intel') || $('#view-intel').hidden) return;
        pollLive();
      }, LIVE_POLL_MS);
    }
  }

  function init() {
    bindFilters();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && $('#view-intel') && !$('#view-intel').hidden) pollLive();
    });
  }

  // 暴露给页面内测试使用（_pollLive 便于在真实浏览器里验证增量轮询的游标语义）
  window.RBIntel = {
    ensureLoaded, refresh: load, state,
    _project: project, _validCoord: validCoord, _pollLive: pollLive, _resetZoom: resetZoom,
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();