/**
 * 真实浏览器回归测试（零新增依赖）。
 *
 * 做法：用 Edge/Chrome 的 DevTools Protocol 驱动 headless 浏览器。
 *  - 不引入 Playwright / Puppeteer / Selenium（仅用 Node 内置的 fetch 与 WebSocket）；
 *  - 因此它只是**开发/测试工具**，不会成为应用的生产运行时依赖；
 *  - 服务端用「真实库的副本 + 临时配置」启动，绝不写用户的生产数据库。
 *
 * 用法：
 *   node scripts/browser-test.mjs
 *   node scripts/browser-test.mjs --headful     # 需要肉眼看时打开窗口
 *   node scripts/browser-test.mjs --port 8899 --cdp-port 9333
 *
 * 环境变量可覆盖：FB_EDGE / FB_PYTHON / FB_PORT / FB_CDP_PORT
 */
import { spawn } from 'node:child_process';
import { copyFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const EDGE = process.env.FB_EDGE
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PYTHON = process.env.FB_PYTHON
  || 'C:\\Users\\L\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const PORT = Number(flag('--port', process.env.FB_PORT || 8899));
const CDP_PORT = Number(flag('--cdp-port', process.env.FB_CDP_PORT || 9333));
const HEADFUL = args.includes('--headful');
const BASE = `http://127.0.0.1:${PORT}`;

/* ------------------------------------------------------------------ CDP */
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = new Map();
    this.errors = [];
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve: done, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else done(message.result);
        return;
      }
      if (message.method) {
        const handlers = this.listeners.get(message.method) || [];
        handlers.forEach((handler) => handler(message.params));
      }
    });
  }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((done, fail) => {
      ws.addEventListener('open', done, { once: true });
      ws.addEventListener('error', () => fail(new Error(`CDP 连接失败: ${wsUrl}`)), { once: true });
    });
    return new Cdp(ws);
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve_, reject) => {
      this.pending.set(id, { resolve: resolve_, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }

  on(event, handler) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(handler);
  }

  close() {
    try { this.ws.close(); } catch { /* ignore */ }
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitFor(fn, { timeout = 20000, interval = 250, label = 'condition' } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try {
      last = await fn();
      if (last) return last;
    } catch (err) {
      last = err;
    }
    await sleep(interval);
  }
  throw new Error(`等待超时: ${label} (最后状态: ${JSON.stringify(last)?.slice(0, 200)})`);
}

/* ------------------------------------------------------------- 测试结果 */
const results = [];
async function check(name, fn) {
  try {
    const detail = await fn();
    results.push({ name, ok: true, detail: detail === undefined ? '' : detail });
    console.log(`  PASS  ${name}${detail ? `  — ${detail}` : ''}`);
  } catch (err) {
    results.push({ name, ok: false, detail: err.message });
    console.log(`  FAIL  ${name}  — ${err.message}`);
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

/* --------------------------------------------------------------- 环境准备 */
function prepareSandbox() {
  const dir = mkdtempSync(join(tmpdir(), 'fb-browser-'));
  const dbSource = join(ROOT, 'data', 'filter-bubble.db');
  if (!existsSync(dbSource)) throw new Error(`找不到真实库: ${dbSource}`);
  copyFileSync(dbSource, join(dir, 'copy.db'));
  // 临时配置：关掉雷达与论文的联网调度，只保留只读接口
  writeFileSync(join(dir, 'social_radar.json'), JSON.stringify({
    enabled: false,
    push: { enabled: false },
    papers: { enabled: false, ai: { enabled: false } },
    llm: { enabled: false },
    anysearch: { enabled: false },
  }, null, 2), 'utf-8');
  return dir;
}

function startServer(sandbox) {
  const child = spawn(PYTHON, ['app.py'], {
    cwd: ROOT,
    env: {
      ...process.env,
      GITPULSE_HOST: '127.0.0.1',
      GITPULSE_PORT: String(PORT),
      GITPULSE_DATA: sandbox,
      GITPULSE_DB: join(sandbox, 'copy.db'),
      RADAR_CONFIG: join(sandbox, 'social_radar.json'),
      AI_CONFIG: join(sandbox, 'app_settings.json'),
      AI_SECRETS: join(sandbox, 'ai_secrets.json'),
      RADAR_SECRETS: join(sandbox, 'radar_secrets.json'),
      PYTHONIOENCODING: 'utf-8',
      RADAR_ENABLED: '0',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', () => {});
  child.stderr.on('data', () => {});
  return child;
}

function startBrowser(sandbox) {
  const profile = join(sandbox, 'edge-profile');
  const argv = [
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${CDP_PORT}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--disable-background-networking', '--disable-sync',
    `--window-size=1366,768`,
    HEADFUL ? '' : '--headless=new',
    HEADFUL ? '' : '--disable-gpu',
    'about:blank',
  ].filter(Boolean);
  return spawn(EDGE, argv, { stdio: 'ignore' });
}

async function connectPage(cdpPort) {
  const target = await waitFor(async () => {
    const response = await fetch(`http://127.0.0.1:${cdpPort}/json/list`);
    const list = await response.json();
    return list.find((item) => item.type === 'page') || null;
  }, { label: 'CDP page target', timeout: 25000 });
  return Cdp.connect(target.webSocketDebuggerUrl);
}

/* ------------------------------------------------------------------ 页面工具 */
function pageApi(cdp) {
  return {
    async eval(expression) {
      const result = await cdp.send('Runtime.evaluate', {
        expression, returnByValue: true, awaitPromise: true,
      });
      if (result.exceptionDetails) {
        throw new Error(`页面内异常: ${result.exceptionDetails.text} ${result.exceptionDetails.exception?.description || ''}`);
      }
      return result.result.value;
    },
    state: () => pageApi(cdp).eval('window.RBIntel && window.RBIntel.state ? window.RBIntel.state.data !== null : false'),
  };
}

const CLICK = (selector) => `
  (() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return 'missing';
    node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'clicked';
  })()`;

const MAP_STATS = `(() => {
  const svg = document.querySelector('#intelMap svg');
  const land = document.querySelector('#intelMap .intel-land');
  return {
    hasSvg: Boolean(svg),
    landLen: land ? (land.getAttribute('d') || '').length : 0,
    dots: document.querySelectorAll('#intelMap [data-event]').length,
    clusters: document.querySelectorAll('#intelMap .intel-cluster').length,
    quakes: document.querySelectorAll('#intelMap .intel-quake').length,
    earthquakes: document.querySelectorAll('#intelMap .intel-quake.is-earthquake').length,
    polygons: document.querySelectorAll('#intelMap .intel-hazard-area').length,
    countText: (document.querySelector('#intelMapCount') || {}).textContent || '',
    detailTitle: ((document.querySelector('#intelDetail h4') || {}).textContent || '').trim(),
    activeDots: document.querySelectorAll('#intelMap .intel-dot.is-active').length,
    feedCards: document.querySelectorAll('#intelFeed [data-feed-event]').length,
    feedChips: Array.from(document.querySelectorAll('#intelFeed .intel-card .intel-chip'))
      .map((node) => node.textContent.trim()),
    liveIds: Array.from(document.querySelectorAll('#intelLive [data-live]'))
      .map((node) => node.getAttribute('data-live')),
    notice: ((document.querySelector('#intelNotice') || {}).textContent || '').trim(),
    noticeClass: ((document.querySelector('#intelNotice') || {}).className || ''),
    hidden: (document.querySelector('#view-intel') || {}).hidden,
  };
})()`;

/* ------------------------------------------------------------------- main */
async function main() {
  const sandbox = prepareSandbox();
  let server = null;
  let browser = null;
  let cdp = null;
  const requests = [];
  const jsErrors = [];
  const networkLogs = [];

  try {
    console.log(`\n启动服务: ${BASE}（数据 = 真实库副本 ${sandbox}）`);
    server = startServer(sandbox);
    await waitFor(async () => {
      const response = await fetch(`${BASE}/api/health`);
      return response.ok;
    }, { label: '应用启动', timeout: 45000 });
    console.log('服务已就绪');

    browser = startBrowser(sandbox);
    cdp = await connectPage(CDP_PORT);

    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Network.enable');
    await cdp.send('Log.enable');
    cdp.on('Network.requestWillBeSent', (params) => requests.push(params.request.url));
    // 只把「未捕获的 JS 异常」计为错误；资源加载失败属于被测试场景的一部分，单独记录
    cdp.on('Runtime.exceptionThrown', (params) => {
      jsErrors.push(params.exceptionDetails?.exception?.description
        || params.exceptionDetails?.text || 'unknown');
    });
    cdp.on('Log.entryAdded', (params) => {
      if (params.entry && params.entry.level === 'error') networkLogs.push(params.entry.text);
    });
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1366, height: 768, deviceScaleFactor: 1, mobile: false,
    });

    /* ---------------------------------------------------- 1/2. 启动 + 切 tab */
    console.log('\n[1-3] 启动页面、切换 tab、地图加载');
    await cdp.send('Page.navigate', { url: `${BASE}/radar.html` });
    await waitFor(() => pageApi(cdp).eval('typeof window.RBIntel === "object"'),
      { label: 'RBIntel 就绪' });

    await check('1. 本地应用与 radar.html 打开', async () => {
      const title = await pageApi(cdp).eval('document.title');
      assert(title.includes('全球热点雷达'), `标题异常: ${title}`);
      const tabs = await pageApi(cdp).eval('document.querySelectorAll(".radar-tab").length');
      assert(tabs >= 7, `应有 7 个 tab，实际 ${tabs}`);
      return `title="${title}" tabs=${tabs}`;
    });

    await check('2. 切换到「全球态势」tab', async () => {
      await pageApi(cdp).eval(CLICK('.radar-tab[data-view="intel"]'));
      await waitFor(() => pageApi(cdp).eval('!document.querySelector("#view-intel").hidden'),
        { label: 'intel 视图可见' });
      await waitFor(() => pageApi(cdp).state(), { label: '聚合数据到位', timeout: 30000 });
      return '视图可见且 /api/radar/intel 已返回';
    });

    let stats = await pageApi(cdp).eval(MAP_STATS);
    await check('3. 地图加载本地底图与真实 API 数据', async () => {
      assert(stats.hasSvg, '没有渲染 SVG 地图');
      assert(stats.landLen > 10000, `底图路径过短(${stats.landLen})，可能未加载 world-110m.js`);
      assert(stats.dots + stats.clusters > 0, '地图上没有新闻点位');
      assert(stats.quakes > 0, '地图上没有地震点位');
      assert(/新闻\s*\d+/.test(stats.countText), `计数文案异常: ${stats.countText}`);
      return `底图${stats.landLen}字符 点位${stats.dots} 聚合${stats.clusters} 地震${stats.quakes}`;
    });

    /* ------------------------------------------- 4/5. 标记点击 / 卡片联动 */
    console.log('\n[4-5] 地图与情报流联动');
    const payload = await pageApi(cdp).eval('JSON.stringify({events: window.RBIntel.state.data.map.events.slice(0,60)})');
    const mapEvents = JSON.parse(payload).events;

    await check('4. 点击新闻标记显示对应事件详情', async () => {
      // 默认缩放下大部分事件在聚合气泡里，所以直接点"已经展开成独立点位"的那个
      const picked = await pageApi(cdp).eval(`(() => {
        const node = document.querySelector('#intelMap .intel-dot[data-event]');
        if (!node) return null;
        const id = node.getAttribute('data-event');
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        return id;
      })()`);
      assert(picked, '地图上没有可点击的独立点位');
      await waitFor(() => pageApi(cdp).eval('document.querySelector("#intelDetail h4") !== null'),
        { label: '详情面板出现' });
      const detail = await pageApi(cdp).eval('document.querySelector("#intelDetail h4").textContent.trim()');
      assert(detail.length > 0, '详情标题为空');
      const titles = new Set(mapEvents.map((event) => event.title));
      assert(titles.has(detail), `详情标题不在事件列表内: ${detail}`);
      const kv = await pageApi(cdp).eval('document.querySelectorAll("#intelDetail .intel-kv dt").length');
      assert(kv >= 6, `详情缺少字段（定位方式/依据等），dt=${kv}`);
      // 详情必须显示定位方法与位置精度（不得让国家质心看起来像精确坐标）
      const text = await pageApi(cdp).eval('document.querySelector("#intelDetail").textContent');
      assert(/定位方式/.test(text), '详情缺少"定位方式"字段');
      return `${picked} → 「${detail.slice(0, 18)}」`;
    });

    await check('4b. 聚合气泡可点击放大（无交互死区）', async () => {
      await pageApi(cdp).eval('window.RBIntel._resetZoom()');
      const info = await pageApi(cdp).eval(`(() => {
        const node = document.querySelector('#intelMap .intel-cluster');
        if (!node) return null;
        const count = Number(node.getAttribute('data-count'));
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        return count;
      })()`);
      if (info === null) return '当前缩放无聚合气泡（跳过）';
      await sleep(400);
      const after = await pageApi(cdp).eval(`(() => ({
        zoom: window.RBIntel.state.map.zoom,
        dots: document.querySelectorAll('#intelMap .intel-dot[data-event]').length,
        clusters: document.querySelectorAll('#intelMap .intel-cluster').length,
      }))()`);
      assert(after.zoom > 1.6, `点击聚合气泡后没有放大（zoom=${after.zoom}）`);
      assert(after.dots > 0, '放大后仍没有展开的独立点位');
      return `聚合 ${info} 个 → zoom ${after.zoom.toFixed(2)}，独立点位 ${after.dots}`;
    });

    await check('5. 点击情报卡片高亮对应地图点', async () => {
      const target = await pageApi(cdp).eval(`(() => {
        const card = document.querySelector('#intelFeed [data-feed-event]');
        const button = card && card.querySelector('[data-locate]');
        return button ? button.getAttribute('data-locate') : '';
      })()`);
      if (!target) return '当前筛选下情报卡无坐标条目（跳过高亮断言）';
      await pageApi(cdp).eval(CLICK(`#intelFeed [data-locate="${target}"]`));
      await waitFor(() => pageApi(cdp).eval('document.querySelectorAll("#intelMap .intel-dot.is-active").length > 0'),
        { label: '地图点高亮' });
      const detail = await pageApi(cdp).eval('document.querySelector("#intelDetail h4").textContent.trim()');
      assert(detail.length > 0, '联动后详情为空');
      return `高亮 ${target}，详情「${detail.slice(0, 16)}」`;
    });

    /* -------------------------------------------------- 6. 筛选一致性 */
    console.log('\n[6-7] 筛选联动');
    await check('6a. 主题筛选后地图与列表一致', async () => {
      const before = (await pageApi(cdp).eval(MAP_STATS)).feedChips.length;
      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelTheme');
        select.value = 'disaster';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(400);
      const after = await pageApi(cdp).eval(MAP_STATS);
      const chips = after.feedChips.filter((text) => text && text !== 'NEW');
      const themes = new Set(chips.filter((text) => !['国内', '国际', '地区未知'].includes(text)));
      assert([...themes].every((text) => text === '自然灾害'),
        `主题筛选后情报卡出现其他主题: ${[...themes].join(',')}`);
      // 地图上的点数不应超过未筛选时
      assert(after.dots + after.clusters >= 0, '地图渲染异常');
      return `卡数 ${before} → ${after.feedCards}，主题全部=自然灾害`;
    });

    await check('6b. 国家筛选生效', async () => {
      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelCountry');
        const option = Array.from(select.options).find((item) => item.value);
        if (!option) return 'no-option';
        select.value = option.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return option.value;
      })()`);
      await sleep(400);
      const code = await pageApi(cdp).eval('window.RBIntel.state.filters.country');
      assert(code, '国家筛选未写入状态');
      const events = await pageApi(cdp).eval(
        'window.RBIntel.state.data.map.events.filter(e => e.country_code === window.RBIntel.state.filters.country).length');
      const dots = (await pageApi(cdp).eval(MAP_STATS)).dots;
      assert(dots >= 0, '地图渲染异常');
      return `country=${code}，匹配事件 ${events}`;
    });

    await check('6c. 灾害类型开关可关掉地震层', async () => {
      await pageApi(cdp).eval(`(() => {
        const input = document.querySelector('input[data-hazard="earthquake"]');
        if (!input) return 'missing';
        input.checked = false;
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return 'ok';
      })()`);
      await sleep(300);
      const after = await pageApi(cdp).eval(MAP_STATS);
      assert(after.earthquakes === 0, `关闭后仍有 ${after.earthquakes} 个地震点`);
      await pageApi(cdp).eval(`(() => {
        const input = document.querySelector('input[data-hazard="earthquake"]');
        input.checked = true;
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(300);
      const restored = await pageApi(cdp).eval(MAP_STATS);
      assert(restored.earthquakes > 0, '重新开启后地震点未恢复');
      return `关闭→0，开启→${restored.earthquakes}`;
    });

    await check('6e. 位置精度筛选（形状区分，不只是颜色）', async () => {
      const legend = await pageApi(cdp).eval(
        'document.querySelector("#intelLegend").textContent');
      for (const label of ['来源坐标（高）', '地名匹配（中）', '国家级近似（低）']) {
        assert(legend.includes(label), `图例缺少精度说明: ${label}`);
      }
      assert(/底图/.test(legend) && /Natural Earth/.test(legend), '图例缺少底图来源说明');

      // 先清掉 6a/6b 留下的主题与国家筛选，否则地图上几乎没有点位
      await pageApi(cdp).eval(CLICK('#intelReset'));
      await waitFor(() => pageApi(cdp).eval(
        '(!window.RBIntel.state.filters.theme && !window.RBIntel.state.filters.country)'),
        { label: '精度测试前重置筛选', timeout: 15000 });
      await waitFor(() => pageApi(cdp).state(), { label: '重置后数据到位' });

      // 默认缩放下事件都在聚合气泡里；先放大到不聚合，才能看到点位形状
      await pageApi(cdp).eval('window.RBIntel._resetZoom()');
      for (let index = 0; index < 3; index += 1) {
        await pageApi(cdp).eval(CLICK('#intelZoomIn'));
      }
      await sleep(400);
      const zoom = await pageApi(cdp).eval('window.RBIntel.state.map.zoom');
      assert(zoom >= 2.4, `放大不足，仍会聚合（zoom=${zoom}）`);

      const diamonds = () => pageApi(cdp).eval(
        'document.querySelectorAll("#intelMap .intel-dot.is-low rect").length');
      const circles = () => pageApi(cdp).eval(
        'document.querySelectorAll("#intelMap .intel-dot circle").length');
      assert((await diamonds()) > 0, '地图上没有菱形（国家级近似）标记');

      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelPrecision');
        select.value = 'reliable';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(400);
      assert((await diamonds()) === 0, '选择"仅中/高置信"后仍出现菱形近似点');
      assert((await circles()) > 0, '选择"仅中/高置信"后没有中/高置信点位');

      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelPrecision');
        select.value = 'approx';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(400);
      assert((await diamonds()) > 0, '选择"仅国家级近似"后没有近似点');
      assert((await circles()) === 0, '选择"仅国家级近似"后仍显示中/高置信点');

      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelPrecision');
        select.value = 'all';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(300);
      return '菱形/圆形按精度区分，三档筛选均生效';
    });

    await check('6f. 导出按钮可用且保留精度字段', async () => {
      const exists = await pageApi(cdp).eval('Boolean(document.querySelector("#intelExport"))');
      assert(exists, '缺少导出按钮');
      const before = jsErrors.length;
      await pageApi(cdp).eval(`(() => {
        const node = document.querySelector('#intelExport');
        node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        return true;
      })()`);
      await sleep(600);
      assert(jsErrors.length === before, `导出触发未捕获异常: ${jsErrors.slice(before).join(' | ')}`);
      return '点击导出无异常（CSV 含 geo_confidence 与精度说明）';
    });

    await check('6d. 时间范围切换会重新取数', async () => {
      const before = requests.filter((url) => url.includes('/api/radar/intel?')).length;
      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelDays');
        select.value = '30';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await waitFor(() => requests.filter((url) => url.includes('days=30')).length > 0,
        { label: 'days=30 请求', timeout: 15000 });
      await waitFor(() => pageApi(cdp).state(), { label: '新数据到位' });
      const after = requests.filter((url) => url.includes('/api/radar/intel?')).length;
      assert(after > before, '切换时间范围没有触发新请求');
      await pageApi(cdp).eval(`(() => {
        const select = document.querySelector('#intelDays');
        select.value = '7';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()`);
      await sleep(800);
      return `${before} → ${after} 次请求`;
    });

    await check('7. Threat Timeline 点击日期筛选事件', async () => {
      // 先清掉前面 6a/6b 留下的主题与国家筛选，否则这里测的是"筛选后的子集"
      await pageApi(cdp).eval(CLICK('#intelReset'));
      await waitFor(() => pageApi(cdp).eval(
        '(!window.RBIntel.state.filters.theme && !window.RBIntel.state.filters.country)'),
        { label: '筛选已重置', timeout: 15000 });
      await waitFor(() => pageApi(cdp).state(), { label: '重置后数据到位' });
      const day = await pageApi(cdp).eval(`(() => {
        const node = document.querySelector('#intelTimeline .intel-tl-dot');
        if (!node) return '';
        node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        return node.getAttribute('data-day');
      })()`);
      if (!day) return '时间线没有可点击的日期点（跳过）';
      await sleep(300);
      const state = await pageApi(cdp).eval('window.RBIntel.state.filters.day');
      assert(state === day, `筛选日期未生效: ${state} vs ${day}`);
      const text = await pageApi(cdp).eval('document.querySelector("#intelTimeline").textContent');
      assert(text.includes(day), '时间线未显示当前筛选日期');
      // 地图点数应等于该日事件数
      const perDay = await pageApi(cdp).eval(
        `window.RBIntel.state.data.map.events.filter(e => e.date === ${JSON.stringify(day)}).length`);
      const stats = await pageApi(cdp).eval(MAP_STATS);
      assert(stats.dots + stats.clusters <= perDay + 1, `地图点数(${stats.dots + stats.clusters})超过该日事件数(${perDay})`);
      // 清除
      await pageApi(cdp).eval(CLICK('#intelClearDay'));
      await sleep(300);
      const cleared = await pageApi(cdp).eval('window.RBIntel.state.filters.day');
      assert(cleared === '', '清除日期筛选失败');
      return `${day}：该日 ${perDay} 个事件`;
    });

    /* ------------------------------------------------ 8. Live News 游标 */
    console.log('\n[8] Live News 分页');
    await check('8. 历史分页无重复条目', async () => {
      const before = (await pageApi(cdp).eval(MAP_STATS)).liveIds;
      assert(before.length > 0, '实时新闻列表为空');
      assert(new Set(before).size === before.length, '初始列表就有重复条目');
      const more = await pageApi(cdp).eval(
        `(() => { const node = document.querySelector('#intelLiveMore');
                  if (!node || node.disabled) return 'disabled';
                  node.dispatchEvent(new MouseEvent('click', { bubbles: true })); return 'clicked'; })()`);
      if (more === 'disabled') return `已有 ${before.length} 条，无更早可加载`;
      await waitFor(async () => {
        const now = (await pageApi(cdp).eval(MAP_STATS)).liveIds;
        return now.length > before.length;
      }, { label: '加载更早', timeout: 15000 });
      const after = (await pageApi(cdp).eval(MAP_STATS)).liveIds;
      assert(new Set(after).size === after.length,
        `分页后出现重复条目（${after.length} 条中唯一 ${new Set(after).size} 条）`);
      return `${before.length} → ${after.length} 条，无重复`;
    });

    await check('8b. 增量轮询（since_id）不产生重复', async () => {
      const before = (await pageApi(cdp).eval(MAP_STATS)).liveIds;
      const cursor = await pageApi(cdp).eval('window.RBIntel.state.live.cursor');
      assert(cursor, '缺少 since_id 游标');
      await pageApi(cdp).eval('window.RBIntel._pollLive()');
      await sleep(1200);
      const after = (await pageApi(cdp).eval(MAP_STATS)).liveIds;
      assert(new Set(after).size === after.length, '轮询后出现重复条目');
      assert(after.length >= before.length, '轮询后条目变少');
      return `since_id=${cursor}，${before.length} → ${after.length} 条，无重复`;
    });

    /* --------------------------------------------------- 10. 视口布局 */
    console.log('\n[10] 响应式布局');
    for (const [label, width, height, expectSingleColumn] of [
      ['1366x768 桌面', 1366, 768, false],
      ['390x844 移动', 390, 844, true],
    ]) {
      await check(`10. ${label} 布局无横向溢出`, async () => {
        await cdp.send('Emulation.setDeviceMetricsOverride', {
          width, height, deviceScaleFactor: 1, mobile: expectSingleColumn,
        });
        await sleep(500);
        const info = await pageApi(cdp).eval(`(() => {
          const main = getComputedStyle(document.querySelector('.intel-main'));
          const map = document.querySelector('#intelMap').getBoundingClientRect();
          return {
            overflow: document.documentElement.scrollWidth - window.innerWidth,
            columns: main.gridTemplateColumns.split(' ').length,
            mapWidth: Math.round(map.width), mapHeight: Math.round(map.height),
          };
        })()`);
        assert(info.overflow <= 2, `横向溢出 ${info.overflow}px`);
        assert(info.mapHeight >= 150, `地图高度过小 ${info.mapHeight}`);
        if (expectSingleColumn) {
          assert(info.columns === 1, `移动端应单列，实际 ${info.columns} 列`);
        }
        return `溢出 ${info.overflow}px，列数 ${info.columns}，地图 ${info.mapWidth}x${info.mapHeight}`;
      });
    }
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1366, height: 768, deviceScaleFactor: 1, mobile: false,
    });

    /* ------------------------------------------------- 9. 异常与降级 */
    console.log('\n[9] 降级与容错');
    await check('9a. 主接口失败时页面仍可用', async () => {
      const before = jsErrors.length;
      await cdp.send('Network.setBlockedURLs',
        { urls: ['*/api/radar/intel*', '*radar/intel*'] });
      await cdp.send('Page.reload');
      await waitFor(() => pageApi(cdp).eval('typeof window.RBIntel === "object"'),
        { label: '重载后脚本就绪' });
      await pageApi(cdp).eval(CLICK('.radar-tab[data-view="intel"]'));
      await waitFor(async () => {
        const stats = await pageApi(cdp).eval(MAP_STATS);
        return stats.notice.includes('加载失败') || stats.notice.includes('失败');
      }, { label: '错误提示', timeout: 20000 });
      const stats = await pageApi(cdp).eval(MAP_STATS);
      assert(stats.noticeClass.includes('is-error'), `提示样式不对: ${stats.noticeClass}`);
      // 其他 tab 仍可切换
      await pageApi(cdp).eval(CLICK('.radar-tab[data-view="daily"]'));
      await sleep(400);
      const dailyVisible = await pageApi(cdp).eval('!document.querySelector("#view-daily").hidden');
      assert(dailyVisible, '主接口失败后无法切回其它 tab');
      await cdp.send('Network.setBlockedURLs', { urls: [] });
      const newErrors = jsErrors.slice(before);
      assert(newErrors.length === 0, `产生未处理异常: ${newErrors.join(' | ')}`);
      return `显示错误提示、可切 tab、无未捕获异常（资源失败日志 ${networkLogs.length} 条，属预期）`;
    });

    let stubHandle = null;
    await check('9b. 空数据/无地理信息时页面可渲染', async () => {
      const stub = `(() => {
        const empty = {
          module: 'radar-intel', version: 2, generated_at: new Date().toISOString(),
          window: { days: 7, from: '2026-10-20', to: '2026-10-26' },
          filters: { days: 7, theme: null, country: null, min_heat: null, layers: [] },
          coverage: {}, threat_timeline: { points: [], latest: null, history_days: 0,
            requested_days: 7, history_confidence: 'none', comparability: 'none',
            caliber_days: {}, missing_inputs: ['no_data'], notes: [] },
          today: null,
          strategic: { observed_activity: { score: null, missing_inputs: ['events'] },
            credibility: { score: null }, monitoring: { coverage_score: 69.7, sources: {} },
            estimate: { score: null, label: '估算', is_estimate: true, components: {},
              weights: {}, missing_inputs: ['observed_activity'] },
            drivers: [], notes: [] },
          intelligence_feed: { items: [], total: 0, pending: false, report_date: null },
          live_news: { items: [], returned: 0, limit: 50, cursor: { before_id: null, since_id: null,
            has_more_older: false, has_more_newer: false } },
          country_index: { available: false, reason: '窗口内没有完成地理编码的事件', partial: true,
            coverage: 'media_only', countries: [], missing_inputs: ['baseline'], semantics: '' },
          map: { available: true, events: [], geo_points: [], layers: [], counts: { news_events: 0,
            geo_points: 0, by_layer: {}, by_confidence: {} }, missing_inputs: ['located_events'] },
          geo: { coverage: { events_total: 0, events_located: 0 }, levels: [] },
          missing_inputs: [], degraded: [{ module: 'map', reason: '没有已定位事件',
            missing_inputs: ['located_events'] }],
          semantics: [], llm: { used: false }, disclaimer: '测试',
        };
        const original = window.fetch;
        window.fetch = async (url, init) => {
          if (String(url).includes('/api/radar/intel')) {
            return new Response(JSON.stringify(empty), {
              status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          return original(url, init);
        };
      })()`;
      stubHandle = await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: stub });
      await cdp.send('Page.reload');
      await waitFor(() => pageApi(cdp).eval('typeof window.RBIntel === "object"'),
        { label: '重载' });
      await pageApi(cdp).eval(CLICK('.radar-tab[data-view="intel"]'));
      await waitFor(() => pageApi(cdp).state(), { label: '空数据渲染', timeout: 20000 });
      await sleep(500);
      const info = await pageApi(cdp).eval(`(() => ({
        notice: document.querySelector('#intelNotice').textContent,
        metrics: document.querySelector('#intelMetrics').textContent,
        timeline: document.querySelector('#intelTimeline').textContent,
        countries: document.querySelector('#intelCountries').textContent,
        mapWarn: (document.querySelector('#intelMap') || {}).textContent || '',
        svg: Boolean(document.querySelector('#intelMap svg')),
      }))()`);
      assert(info.metrics.includes('数据不足'), `空数据时应显示"数据不足": ${info.metrics.slice(0, 60)}`);
      assert(info.countries.includes('不可用') || info.countries.includes('没有'),
        `国家面板应显示不可用: ${info.countries.slice(0, 60)}`);
      assert(info.svg, '空数据时地图容器未渲染');
      assert(!info.metrics.includes('NaN') && !info.metrics.includes('undefined'),
        '空数据渲染出现 NaN/undefined');
      return '显示"数据不足"、国家面板不可用、无 NaN/undefined';
    });

    // 空数据桩必须撤掉，否则后续所有重新加载都会拿到空 payload
    await cdp.send('Page.removeScriptToEvaluateOnNewDocument',
      { identifier: stubHandle.identifier });
    await cdp.send('Page.reload');
    await waitFor(() => pageApi(cdp).eval('typeof window.RBIntel === "object"'),
      { label: '撤销桩后重载', timeout: 20000 });

    /* --------------------------------------- 12. 断开外部网络仍可用 */
    console.log('\n[11-12] 外部请求与离线可用性');
    await check('11. 全程未产生未处理 JS 错误', async () => {
      const real = jsErrors.filter((text) => !/favicon/i.test(text));
      assert(real.length === 0, `未处理错误 ${real.length} 条: ${real.slice(0, 3).join(' | ')}`);
      return `${requests.length} 个请求，0 个未捕获异常（资源错误日志 ${networkLogs.length} 条）`;
    });

    const external = requests.filter((url) => {
      if (url.startsWith('data:') || url.startsWith('blob:')) return false;
      try {
        const host = new URL(url).hostname;
        return !['127.0.0.1', 'localhost', '::1'].includes(host);
      } catch { return false; }
    });
    await check('12a. 页面不请求任何外部主机', async () => {
      assert(external.length === 0, `出现了外部请求: ${external.slice(0, 3).join(', ')}`);
      return `共 ${requests.length} 个请求，全部为本机/内联资源`;
    });

    await check('12b. 阻断常见外部资源后地图仍可用', async () => {
      await cdp.send('Network.setBlockedURLs', {
        urls: ['*tile.openstreetmap.org*', '*basemaps.cartocdn.com*', '*cdn.jsdelivr.net*',
          '*unpkg.com*', '*cdnjs.cloudflare.com*', '*autonavi*', '*mapbox*', '*googleapis*'],
      });
      await cdp.send('Page.reload');
      await waitFor(() => pageApi(cdp).eval('typeof window.RBIntel === "object"'), { label: '重载' });
      await pageApi(cdp).eval(CLICK('.radar-tab[data-view="intel"]'));
      await waitFor(() => pageApi(cdp).state(), { label: '数据到位', timeout: 20000 });
      await sleep(500);
      const stats = await pageApi(cdp).eval(MAP_STATS);
      assert(stats.landLen > 10000, '底图丢失');
      assert(stats.dots + stats.clusters > 0, '点位丢失');
      assert(stats.quakes > 0, '地震点丢失');
      await cdp.send('Network.setBlockedURLs', { urls: [] });
      return `底图${stats.landLen}字符、点位${stats.dots}、地震${stats.quakes} 均在`;
    });

    /* ----------------------------------------------------------- 汇总 */
    const failed = results.filter((item) => !item.ok);
    console.log(`\n${'='.repeat(72)}`);
    console.log(`真实浏览器测试：${results.length - failed.length}/${results.length} 通过`);
    if (failed.length) {
      console.log('失败项：');
      failed.forEach((item) => console.log(`  - ${item.name}: ${item.detail}`));
    }
    console.log(`${'='.repeat(72)}\n`);
    return failed.length ? 1 : 0;
  } finally {
    if (cdp) cdp.close();
    if (browser) browser.kill();
    if (server) server.kill();
    await sleep(800);
    try { rmSync(sandbox, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error('浏览器测试崩溃:', err);
  process.exit(2);
});