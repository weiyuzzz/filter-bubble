# 全球态势监控工作台 · 阶段一：代码审查与实现方案

> 本文是**只读审查 + 方案设计**的产物，**不含任何业务代码改动**。
> 审查对象：`D:\dpsworkbench\filter-bubble`（GitHub: `weiyuzzz/filter-bubble`，分支 `main`，HEAD `36397da`）
> 审查与实测时间：**2026-10-26**（Asia/Shanghai）
> 目录说明：`docs/` 目录已存在，因此本报告按要求写入 `docs/global-intel-design.md`，无需另择位置。

---

## 0. 结论摘要（先看这一段）

1. **现有架构比预期好得多。** `core/radar/` 已经是一套完整的「适配器注册表 → 统一入库去重 → 两级聚类 → 可解释打分 → 日报 → 推送」流水线，13 张 `radar_*` 表、13 个 API、35 个数据源、独立线程调度器。本次要加的五个面板，**主体应该做成对现有数据的"只读聚合视图"，而不是第二套新闻系统**。
2. **最致命的缺口只有一个：地理维度完全没有。** `radar_events` / `radar_items` / `radar_stories` 三张表里**没有任何国家、经纬度、地区字段**（全库 grep `lat/lon/country/geo` 零命中，唯二命中是 `platform_count`、`last_latency_ms` 之类的假阳性）。地理信息在采集时确实拿到了（GDELT 的 `sourcecountry`），但被丢进了一行 `raw_content` 文本里。**没有地理维度就没有地图，也没有国家不稳定性指数。**
3. **第二个缺口：历史深度只有 3 天。** `radar_event_snapshots` 887 行、`radar_events` 886 行，**只覆盖 2026-10-23 / 24 / 25 三天**；日报只有 3 份。所以「过去 7 天威胁等级趋势」**今天不可能从历史真实算出**，只能先搭好计算与展示骨架，等运行满 7 天自然成型。**这一条不许用插值或假数据糊过去。**
4. **天花板：`category` 95.5% 是「综合」。** 886 条事件里 846 条落到 `综合`，因为 `category` 语义是「命中了我配置的哪个兴趣主题」，不是新闻分类。任何依赖 `category` 分栏的面板都必须先做 `classify.py`。
5. **硬约束（这条决定了地图选型）：仓库自带测试明令禁止外部 CDN，且只允许一份 CSS 文件。** `tests/test_web.py:60` 的 `test_no_external_assets` 会对 `index.html/radar.html/status.html` 里任何 `src="http(s)://..."` 直接 `fail`；`tests/test_web.py:165` 的 `test_css_is_not_duplicated_across_files` 要求 `web/*.css` 只能是 `["styles.css"]`。**用 CDN 引 Leaflet（含它自带的 `leaflet.css`）会直接踩红两条既有测试。** 所以地图只能走「零依赖内联 SVG」或「库文件本地内置」两条路，推荐前者。
6. **测试基线是 240 个全绿，不是文档里写的 51 个。** 实测 `Ran 240 tests in 7.968s / OK`；而 `docs/radar.md:335` 仍写着「51 个测试全部通过」。**文档已过期**，本次方案里所有"改动不能破坏现有测试"的说法都以 240 为基准。
7. **外部数据源的可达性是实测出来的，不是抄文档的。** USGS 地震、World Bank、Natural Earth、jsDelivr/unpkg、OSM/Carto/高德瓦片全部 **200 通过**；GDELT Doc API **429**、GDELT Geo API **404/超时**、UCDP **401（要 token）**、ACLED **TLS 被断**、ReliefWeb v1 **410 Gone**。详见 §6.3。
8. **大模型不是必需品。** 五个面板**全部可以零 LLM 算出**（`summarize.py` 已有抽取式回退，`investigate.py` 未启用模型时走本地规则引擎，`_method_note()` 会自报"未启用任何外部模型"）。只有"精炼叙事"这一步值得用模型，且必须可缓存、可跳过。
9. **前置风险：工作区有未提交的在途改动（bookmarks 模块）。** `app.py`(+149)、`core/bookmarks.py`、`web/bookmarks.*`、`web/styles.css`(+98) 等 15 个文件都是 dirty 状态。**新工作必须从干净分支起步，否则必然冲突。**
10. **第一阶段（本阶段）交付物就是本文档**，不改一行业务代码。

---

## 1. 本阶段的审查方法与证据等级

| 等级 | 含义 | 本文如何标注 |
|---|---|---|
| **已核实** | 我本次真的读了代码 / 查了数据库 / 发了请求，并给出文件行号或原始输出 | 正文直接陈述 |
| **待验证** | 无法确认能否使用，或依赖外部状态 | 标 `⚠️待验证` |
| **原型阶段实测** | 来自 `prototype/需求梳理与改造方案.md` 的历史结论，**本阶段未复验** | 标 `🔶原型实测` |

执行过的只读操作：
- 读代码：`app.py`、`core/radar/*.py`、`core/radar/sources/*`、`core/db.py`、`core/radar/config.py`、`web/radar.html`、`web/radar.js`、`tests/_bootstrap.py`、`tests/test_web.py`、`docs/radar.md`、`README.md`、`prototype/需求梳理与改造方案.md`
- 查库：以 `mode=ro` 思路只读打开 `data/filter-bubble.db`（8.5 MB），导出全部 DDL、行数、字段分布与样本行
- 跑测试：`python -m unittest discover -s tests -t tests -v`
- 发请求：对 20+ 个外部端点做可达性探测（USGS/GDELT/UCDP/ACLED/World Bank/CDN/瓦片服务等）
- `git status` / `git diff`：确认未提交改动范围

**未做**（明确声明）：没有安装任何依赖、没有建表、没有迁移数据库、没有删除或重命名文件、没有修改业务代码、没有启用任何数据源、没有调用大模型。

---

## 2. 现有架构与数据流实测分析

### 2.1 运行方式与静态资源

`app.py` 共 788 行，是**唯一入口**：`serve()` 建库（`db.init()`）→ 起 GitHub 日报调度线程 → 起雷达调度 → 起论文工作线程 → 起 HTTP 服务（`ThreadingHTTPServer`，默认 `127.0.0.1:8791`，可由 `GITPULSE_HOST/PORT` 改）。

静态资源由 `Handler._static()` 提供（`app.py:116-138`）：

```python
full = (config.WEB_DIR / rel).resolve()
full.relative_to(config.WEB_DIR.resolve())   # 目录穿越防护
if not full.is_file():
    full = config.WEB_DIR / "index.html"     # 找不到的文件回落到首页
```

要点（对本次方案有直接影响）：
- **任意子目录都能服务**：`web/vendor/leaflet.js` 会自动映射到 `/vendor/leaflet.js`，无需改 `app.py`。
- **缓存策略是 `Cache-Control: no-cache`**，配合 URL 无版本号 → 前端改动刷新即生效，但也意味着大文件（内联底图）每次都会重新传输，需要注意体积。
- **不存在的路径静默回落 `index.html`（HTTP 200）**：如果新页面文件名写错，会得到一个"看起来正常但内容不对"的首页，**排查时容易被误导**。新资源必须用真实路径验证。

MIME 表来自 `mimetypes`，`.js` / `.css` / `.json` / `.svg` 均可正常下发。

### 2.2 前端形态：无构建、无依赖、单一样式表

- **没有** `package.json`、`node_modules`、打包器、构建步骤（全仓库已确认）。
- `web/` 下全部是手写原生 HTML/JS/CSS：`radar.js` 38 KB、`styles.css` 68 KB、`shell.js` 7.6 KB。
- 每个页面引用且只引用**本地**资源：`/styles.css`、`/shell.js`、`/radar.js`（`web/radar.html:10,162,163`）。`<head>` 里只有内联的 data-URI favicon 和主题引导脚本。
- **当前没有任何图表库、没有任何地图库、没有任何瓦片请求。** `prototype/` 目录里有地图原型，但**运行中的应用一行都没引用它**（`app.py` / `core/` / `web/` 中 grep `prototype` 零命中）。
- 共享外壳由 `shell.js` 提供：主题三态（light/dark/auto，存 `fb.theme`）、侧栏折叠（`fb.nav`）、视图密度（`fb.view`/`is-list`）。**`shell.js` 明令不许调用业务接口**（`tests/test_web.py:182`），业务请求必须留在页面级脚本里。

### 2.3 后端模块地图

```
app.py                      路由总入口 + GitHub 日报调度线程 + 静态服务
core/
  db.py                     基础库（12 张原表）+ connect()/now_iso()/get_meta/set_meta
  radar/
    api.py        379 行    雷达全部 HTTP 路由（GET 10 个 + POST 7 个）
    service.py    359 行    编排：collect → cluster → score → report → push；独立调度线程
    store.py      778 行    雷达全部 DDL + 迁移(_ensure_column) + 全部查询/写入函数
    cluster.py    343 行    两级聚类：items→events→stories
    score.py      261 行    可解释热度/重要度打分
    report.py     440 行    日报 payload 组装（highlights/zh_web/intl/tech/coverage…）
    summarize.py  199 行    摘要：可选大模型 + 抽取式回退
    investigate.py 640 行   事件调查 / 事实核验（本地规则引擎为主）
    config.py     264 行    配置 DEFAULTS + 环境变量覆盖，每次调用重新加载
    fetch.py                统一 HTTP（超时/重试/限速）
    push.py                 微信推送（Server酱）
    sources/
      __init__.py 501 行    源注册表 SOURCES + ADAPTERS + collect()
      rss.py / intl.py / newsnow.py / hotlists.py / anysearch.py
  papers/                   学术论文（13 张 paper_* 表，与雷达完全隔离）
  ai_client.py / ai_brief.py / ai_settings.py / ai_usage.py / ai_secrets.py
  brief.py / digest.py / github.py / follows.py / bookmarks.py / notify.py / prefs.py / rank.py / seen.py
```

**隔离设计是硬性约定**：`app.py:558` 起，雷达/论文的调度启动都包在 `try/except` 里，注释写明「这里的任何异常都不能影响 GitHub 服务启动」；`core/radar/api.py` 头部写明「nothing in this module imports app.py」。**新增功能必须继续遵守这条。**

### 2.4 调度器：一次采集到出报的完整链路

`core/radar/service.py:299` `scheduler_loop(stop)`，与 GitHub 日报调度**共用同一个 `stop` Event 但各自独立线程**：

```
每 30 秒醒一次
├─ 每天到点（默认 08:00，且当天还没出报）
│    └─ collect_and_report()   完整链路 + 推送
└─ 每 collect_interval_minutes（默认 60）且距上次 ≥20 分钟
     └─ collect_only()        只采集，不出报不推送
```

`collect_and_report()` 的实际步骤（`service.py:112-192`）：

1. `store.start_run()` 落一条 `radar_runs`（status=running）
2. `sources.collect(conn, cfg)` 并发抓所有启用源 → 逐源写 `radar_sources` 健康状态
3. **取 `recent_items(conn, since=now-36h, limit=max_candidates)`**（当前配置 600，`service.py:139`）
4. `cluster.build_events(conn, today, items, cfg)` → 打分
5. 只保留 `max_events`（当前 220）个，按 `importance+heat` 排序
6. `cluster.build_stories(...)` 再聚一层
7. 逐事件：`assign_event` + `upsert_event` + `save_event_snapshot`
8. `report.build()` → `store.save_report(date,'daily')`
9. `push.send_daily()`（默认关闭）
10. `store.prune(conn, retention.days=90)`

**关键性能天花板**：整条链路的候选池是"最近 36 小时 + 上限 600 条"，事件上限 220，而聚类是 **O(n²) 两两比较**。`docs/radar.md:469` 自己写明「候选上限 400 条以内没问题」。**把全球结构化图层（地震/灾害等）塞进这条链路会直接把它压垮**，这正是下面方案里"结构化地理点单独入 `radar_geo_points`、不进新闻聚类"的原因。

### 2.5 数据库现状（只读实测）

数据库 `data/filter-bubble.db`，共 **32 个对象**，其中雷达 **13 张表**：

| 表 | 行数 | 说明 |
|---|---|---|
| `radar_sources` | 35 | 源健康状态、失败原因、延迟、熔断标记 |
| `radar_items` | 3144 | 统一格式原始材料（`url_hash` 唯一去重 + `content_hash` 跨源去重） |
| `radar_events` | 886 | 事件 |
| `radar_event_snapshots` | 887 | **每事件每日快照（趋势计算的唯一可靠来源）** |
| `radar_stories` | 397 | 故事层 |
| `radar_claims` | 38 | 事实主张 |
| `radar_evidence` | 181 | 证据 |
| `radar_claim_evidence` | 181 | 主张↔证据关系 |
| `radar_summaries` | 119 | 摘要缓存 |
| `radar_reports` | 21 | 3 份 daily + 18 份 investigation |
| `radar_rank_history` | 4523 | 每次采集的榜单位次 |
| `radar_push_log` | 1 | 推送去重 |
| `radar_runs` | 20 | 每次运行统计（**含 3 条卡在 running 的僵尸记录**，见 §2.9） |

`docs/radar.md:292` 写「新增 12 张表」，实测是 13 张（漏了 `radar_claim_evidence` 的计数口径或 `radar_runs`）。**文档口径与库不一致，以库为准。**

**历史深度（决定"7 天趋势"可行性）：**

| 表 | 日期范围 | 每日行数 |
|---|---|---|
| `radar_event_snapshots` | **2026-10-23 → 10-25（仅 3 天）** | 433 / 220 / 234 |
| `radar_events.date` | 2026-10-23 → 10-25（仅 3 天） | 433 / 218 / 235 |
| `radar_rank_history` | 2026-10-23 → 10-26（4 天） | 945 / 2760 / 413 / 405 |
| `radar_reports`(daily) | 2026-10-23 → 10-25（**3 份**） | 1 / 1 / 1 |
| `radar_items.collected_at` | 2026-10-23T00:00 → 2026-10-26T02:08 | — |

**字段级关键事实（这是本方案最重要的输入）：**

- `radar_events` 全部列：`id, date, title, summary, category, keywords, heat, importance, confidence, item_count, platform_count, source_count, first_seen_at, last_updated_at, origin_item_id, tech_flag, investigation_state, metrics, story_id`。
  **没有 country / latitude / longitude / severity / threat_level。** 唯一的程度量是 `heat`(0–92) 与 `importance`(0–63.7)，以及 `confidence`(取值只有 `reported` 512 / `unverified` 374；代码里定义的 `verified_multi` 一行都没有)。
- `radar_items` 全部列：`id, source_id, platform, title, url, url_hash, author_name, published_at, collected_at, language, engagement, content_hash, raw_content, source_type, fetch_status, last_seen_at, event_id`。
  **同样没有地理字段**；`raw_content` 是唯一可能残留来源国的地方。
- **`published_at` 有 1578/3144（50.2%）是空串**——热榜类来源（微博/百度/抖音/头条…）根本没有发布时间。任何"按发布时间排序/统计"的逻辑必须回落到 `collected_at` 并显式标注。
- **`language` 取值严重不一致**：`zh`(2774) / `en`(235) / `zh-CN`(35) / `English`(28) / `Chinese`(22) / `Spanish`(8) / `German`(7) …… 还有被截断成 `Vietname`、`Ukrainia`、`Portugue`、`Lithuani` 的值（因为 `make_item` 里 `language` 取自 GDELT 的 `entry["language"]` 并被 `[:8]` 截断过）。**必须做一次归一化。**
- `radar_events.metrics` 是 JSON，键为 `lifecycle, velocity, cross_platform, engagement, verification, interest, weights, platforms, sources, independent_domains, news_domains, official_source, flags, interest_hits, previous_item_count, raw_heat`。**可解释性基础很好，可直接复用为面板的分项依据。**
- `radar_events.date` 的语义是**"最后活跃日"而不是"发生日"**：`store.upsert_event()`(`store.py:397`) 在冲突时执行 `date=excluded.date`，即每次该事件再次被聚类到就会被改写为当天日期。`first_seen_at` 才是最早材料时间。
  → **推论：做"过去 7 天趋势"必须用 `radar_event_snapshots(date, event_id, item_count, platform_count, heat)`，不能用 `radar_events.date` 分组。** 这是本方案里一个容易踩错的技术点。
- `radar_events.category` 分布：`综合` 846 / `AI 与大模型` 21 / 空 5 / 其余均 ≤3。**95.5% 是兜底值。**
- `store.prune()`(`store.py:767`) **只删 `radar_items` 和 `radar_runs`**（>90 天），**事件/快照/日报永久保留**。→ 这恰好保证了时序面板的数据不会被清理掉。

**迁移机制（可放心复用）：**

```python
def init() -> None:                       # store.py:200
    with db.connect() as conn:
        conn.executescript(SCHEMA)         # CREATE TABLE IF NOT EXISTS ... 幂等
        _ensure_column(conn, "radar_events", "story_id", "TEXT")
        _ensure_column(conn, "radar_sources", "paused_reason", "TEXT")
        conn.commit()

def _ensure_column(conn, table, column, decl):   # store.py:210
    existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column in existing: return
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
```

`_ensure_column` 是**只加列、不动数据、可重复执行**的安全迁移。本次测试运行日志里就实际打印了 `[radar] migrated: radar_events.story_id` / `radar_sources.paused_reason`，证明它在真实库上工作正常。**新字段一律走这个机制，不写自造迁移框架。** 库的 `PRAGMA user_version = 0`（未被使用）。

### 2.6 两级聚类的真实算法（决定"不要重复聚类"）

`core/radar/cluster.py`：

- **分词**：CJK 取**双字 bigram** + 拉丁词（≥3 字符），带停用词表。
- **相似度**：Jaccard = `|A∩B| / |A∪B|`。
- **第一级 items→events**（`cluster_items`，阈值 `cluster.within_run_threshold=0.30`）：
  1. 先按 `content_hash`（无则 `url_hash`）**精确合并完全重复**；
  2. 剩余条目两两算 Jaccard，≥0.30 连边；
  3. 并查集（union-find）合并连通分量。**注释明确承诺「Never drops an item」**。
- **跨轮身份保持**（`build_events`，阈值 `cluster.match_threshold=0.35`）：
  取「今天 + 昨天」的已有事件作为比对池，新分组的规范标题 token 与之比对，取相似度最高的且 ≥0.35 → **沿用已有 `event_id`（历史因此累积）**；否则用 `_event_id(date, title) = sha1(date + 前12个token)` 生成稳定新 id。
  同一轮新建的事件**立刻进入比对池**（注释说明这是为修「75% 事件只有一条材料」而加的）。
- **第二级 events→stories**（`build_stories`，阈值 `cluster.story_threshold=0.42`）：
  两条规则**任一成立**即合并：① 标题 token **包含系数**（`|∩|/min(|A|,|B|)`）≥0.42；② **稀有词共现 ≥2**（稀有 = 该 token 的文档频率 ≤ `max(2, 8%×事件总数)`，且不在媒体名/套话停用片段里）。
  故事 id = `sha1(成员事件id排序后拼接)`，**由成员集合决定 → 稳定，不会因标题变化而漂移**。
- 故事热度 = `max(成员 heat) + min(6, 2×(角度数-1))`，即"多角度加成"。

**结论：聚类已经很成熟且阈值可配置。新增面板必须是这套结果的消费者，绝不能自己再聚一次。**

### 2.7 打分的可解释性（可直接复用的分项）

`score.score_events()`（`score.py:131`）产出 `heat` 与 `importance`，并把**全部中间量写进 `event.metrics`**：

- `velocity`（对比前一日快照的增长，首次出现则 `item_count/6`）
- `cross_platform`（`平台数/4`）
- `engagement`（**按平台分别归一化**后再平均，避免跨平台比点赞数）
- `verification`（`新闻类独立域名数/3`，官方源直接抬到 ≥0.6）
- `interest`（命中配置兴趣主题数/3）
- 权重来自 `cfg.scoring`（默认 30/20/20/20/10），**会一并写进 `metrics.weights`**
- 惩罚项：来源集中 ≥70% → ×0.85 并打 `来源集中` 标；单一平台且 ≥5 条 → ×0.9 打 `单一平台` 标；`recirculated`（旧闻重传）→ ×0.8 打 `旧闻重传` 标
- `confidence`：`news_domains≥3 且 verification≥0.66` → `verified_multi`；`news_domains>0` → `reported`；否则 `unverified`
- 最后按当日最强事件**相对重标定到 ≤92**，原始值保留在 `metrics.raw_heat`
- `importance = 0.45×verification + 0.25×cross_platform + 0.15×tech + 0.15×时效`

**这套分项就是"严重程度/可信度"面板的现成原料**——不需要发明第二套打分。

### 2.8 大模型的使用与回退（回答"能不能不用模型"）

- **全局配置**在 `app_settings.json` + `data/ai_secrets.json`（`core/ai_settings.py`），有**主/备两个接口**，`token_limits.primary/fallback = 500000`，`window_hours=24`，主接口额度用尽自动切备用、两个都用尽则**暂停 24 小时**（`ai_usage.py`）。运行日志与错误落在 `data/llm-errors.log`。
- **雷达摘要**（`summarize.py`）：`summarize_event()` 先查 `radar_summaries` 缓存（键 = `事件id|provider|model|内容哈希`），未命中才调模型；**模型失败/未配置/返回非 JSON/摘要超 800 字 → 一律回退到 `extractive()`**（`summarize.py:174-175`），`extractive()` 只做**已采集标题的拼接**并按相似度挑 3 个"不同角度"的标题，**明确声明不生成新事实**。回退来源记为 `provider="extractive"`。
- **事件调查**（`investigate.py`）：主体是**本地规则引擎**（句子切分、来源角色判定、主张抽取、关系判定、矛盾检测、更正检测）。`_method_note()`(`investigate.py:461`) 会按是否启用模型输出不同声明：未启用时写明「分析由本地规则引擎基于已采集材料生成（未启用任何外部模型）」。**即调查/核验功能在没有模型时是完整可用的。**
- **已实测**：本机当前主备额度都已用尽（日志 `[ai] 主备接口额度都用尽，AI 暂停到 2026-10-27T02:14:59+08:00`），雷达日报仍正常生成——**这本身就是"零 LLM 可用"的现场证据**。

**→ 结论：五个面板全部按"零 LLM 可算"设计，模型只作为可选润色层，且必须走 `radar_summaries` 式的内容哈希缓存。**

### 2.9 现有稳定性问题（本次实测到的新发现）

1. **SQLite 写锁竞争已经真实发生过**：`radar_runs` 里有 `run-4f66b6e08c43` 状态 `partial`，`stats.collect.error = "OperationalError: database is locked"`（2026-10-24 08:00）。同时库里有 **3 条永远停在 `running` 的僵尸运行记录**（`run-f452f991f33a`、`run-3d9f7d2ca7b6` 等），说明进程曾在写库中途被杀。
   → **新增的趋势物化写入必须放进取报的同一个事务里，不要再开第二条写入路径。**
2. **源熔断机制工作正常且已经触发**：`radar_sources` 中 `gdelt`、`v2ex_hot`、`bbc_zh`、`dw_zh` 四条 `status=paused`，`consecutive_failures=6`，`paused_reason='连续失败 6 次，已自动停用'`（阈值 `backoff.pause_after=6`）。可手动 `POST /api/radar/sources/reset` 复活。
3. **5 个源永久不可用且被诚实标注**：`x_twitter, xiaohongshu, instagram, threads, tiktok` 的 `kind='unavailable'`，备注统一为「无合规免费入口，未接入（不绕过登录/验证码/访问控制）」。**这个价值观要保持。**
4. **3 个源默认关闭但代码在**：`bluesky`（本机 403）、`reddit_tech`（本机 403 Blocked）、`arxiv`（本机 TLS 证书被中间人拦截 `CERTIFICATE_VERIFY_FAILED`）。
5. `radar_reports` 的 `daily` payload **只存了 3 天**，且 `version` 字段存在（当前 2），说明日报结构已在演进——**新字段必须走"增量加键 + 旧键保留"**。

### 2.10 API 契约（现有 13 个雷达接口，一个都不能动）

`core/radar/api.py` 统一分发，`app.py:199` 只做转发（`/api/radar` 及 `/api/radar/*`）。

| 方法 | 路径 | 作用 | 关键返回 |
|---|---|---|---|
| GET | `/api/radar/status` `/health` | 运行状态 | `state{stage,running,last_error…}`、`today_ready`、`schedule{time,next_run,collect_interval_minutes}`、`push`、`summary_backend`、`cluster`、`sources{total,ok,error,paused,disabled,unavailable}`、`runs[]` |
| GET | `/api/radar/report[?date=]` | 日报 | `{date, generated_at, payload{highlights,zh_web,intl,tech,investigation_updates,coverage,evidence_gaps,stats,summary_backend,disclaimer,scoring_weights,version}}`；无报时返回 **202 `{pending:true}`** |
| GET | `/api/radar/events[?date=]` | 某日事件（含前 6 条 items） | `{date, events[]}` |
| GET | `/api/radar/event?id=` | 单事件全量 | event 列 + `items[]` + `claims[]` |
| GET | `/api/radar/sources` | 源列表 | `{sources[], summary{total,ok,error,paused,disabled,unavailable}}`（调用时会先把配置里的新源同步入库） |
| GET | `/api/radar/history` | 历史 | `{reports[], investigations[], runs[]}` |
| GET | `/api/radar/settings` | 监控设置（脱敏） | `{settings, path}` |
| GET | `/api/radar/investigation?event_id=\|q=` | 读已有调查报告，否则现场跑 | investigation payload |
| POST | `/api/radar/collect` | 异步触发完整链路 | `{started, status}` |
| POST | `/api/radar/report` | 只重算日报 | `{result, status}` |
| POST | `/api/radar/investigate` | 跑调查 `{event_id\|query}` | investigation payload |
| POST | `/api/radar/sources/reset` | 重置熔断 `{source_id?}` | `{reset, sources[]}` |
| POST | `/api/radar/push` | 推送日报 `{force?}` | `{result, push}` |
| POST | `/api/radar/settings` | 保存白名单设置 | `{saved, settings, path, llm_status}` |
| POST | `/api/radar/llm/test` | 兼容旧前端 | `{result, llm_status}` |

`daily` payload 的 `highlights` 是**故事级**对象，已含：`story_id, event_id, title, summary, headline, points, summary_provider, summary_cached, category, heat, importance, confidence, confidence_label, angle_count, angles[], item_count, lifecycle, flags[], tech_flag, platforms[], spread{zh_items,intl_items,platforms}, earliest_source{}, latest_update, integrity_note, claim_summary, sources[], suggest_investigation, is_new`。

**→ 「Intelligence Feed」面板所需的全部字段，这里已经有了。** 这是复用价值最高的一处。

### 2.11 前端页面结构与"加一个面板"的标准做法

`web/radar.html` 165 行，结构固定：

```
aside.sidebar（品牌 + 5 个一级导航 + 折叠）
  └─ 导航项：/  · /radar.html · /papers.html · /settings.html · /status.html
div.app-main
  ├─ header.topbar（面包屑 + 状态灯 #radarDot/#radarState + #radarUpdated + #collectBtn + #pushBtn + #themeToggle）
  └─ main.content > div.shell
       ├─ section.radar-head（4 个指标 #mEvents/#mItems/#mSources/#mFailed）
       ├─ nav.radar-tabs#radarTabs（6 个 button.radar-tab[data-view]）
       └─ 6 个 section.radar-view#view-{daily,investigate,tech,sources,history,settings}
```

`radar.js` 的既有模式（**新面板照抄这套即可**）：
- `api(path, options)`（`radar.js:39`）统一 fetch + 错误处理 + `message` 提取
- `switchView(name)`（`:68`）切 tab：toggle `.is-active` / `hidden`，并按需懒加载
- 每个 tab 一个 `loadXxx()` → 一个 `renderXxx()` → 写入 `#xxxResult` 的 `innerHTML`
- `startPolling()`（`:675`）用 `setInterval` 轮询状态
- localStorage 键沿用 `fb.` 前缀（`fb.theme` / `fb.nav` / `fb.view`）

`tests/test_web.py` 给前端立的**硬契约**（新面板必须满足）：
- `test_radar_has_every_id_radar_js_uses`：`radar.js` 里每个 `$("#id")` 都必须在 `radar.html` 里有对应 `id="id"`（例外名单 `DYNAMIC_IDS` 只放设置表单里由 JS 自己渲染的字段）。
- `test_no_external_assets`：三个主页面**不得出现任何 `http(s)://` 的 src/href**。
- `test_css_is_not_duplicated_across_files`：`web/*.css` 只允许 `styles.css`。
- `test_radar_has_no_papers_view`（实为 `test_papers_page_is_first_level_navigation` 的一部分）：`radar.html` 不得出现 `data-view="papers"`，`radar.js` 不得出现 `loadPapers`。
- `test_status_js_only_reads_known_endpoints`：状态页只读、不得 POST。
- 设计令牌：`--fb-accent/bg/surface/text/border/success/warn/danger/info/r-md/shadow-1/dur/max-w` 必须存在；`[data-theme="dark"]` 独立令牌块；`prefers-reduced-motion`；断点 `1240/1080/860/560`。

### 2.12 数据源清单（35 个，实测状态）

按 `kind` 分：`rss` 12 · `api` 11 · `hotlist` 9 · `query` 1 · `unavailable` 5（计数含 newsnow 代理项）。

- **中文强**：中新网(滚动/国内/国际)、新浪、IT之家、量子位、澎湃、凤凰、财联社、华尔街见闻 + 热榜(微博/百度/B站/抖音/头条/网易/贴吧/知乎)。
- **国际弱**：只有 Hacker News、TechCrunch、GDELT(已暂停)、DW 中文(已暂停)、BBC 中文(已暂停)、arXiv(默认关)。
- **AnySearch**：配置驱动的检索式发现源（`social_radar.json → anysearch.queries`，当前 2 条：AI、芯片），`min_interval_minutes=240` 节流以保护 20 次/窗口的额度；`investigation.external_search=true` 时还用于调查取证（本次测试日志里实际出现 `[investigate] AnySearch 取证失败：HTTP 429`，**说明额度真的很紧**）。
- **newsnow** 第三方聚合（`newsnow.busiyi.world`）承载抖音/知乎/贴吧/华尔街见闻/财联社/澎湃/凤凰，带**域名校验**（`verify_domain`，测试日志有 `[newsnow] douyin 丢弃 1 条域名不匹配的链接`）——这个"防第三方数据被篡改"的设计值得保留。

**源的可扩展性（本次方案要用的地方）**：`sources/__init__.py:22` 是静态 `SOURCES` 列表，`ADAPTERS` 分发表（`:154`），`_merged()` 合并「静态清单 + 自定义 RSS + AnySearch 查询 + 用户覆盖」。
**加一个新源 = ①`SOURCES` 加一条 spec（含 `adapter` 键）②`ADAPTERS` 注册一个函数 ③函数签名 `(spec, make_item) -> list[dict]`。** 已经有 `_rss_specs()` / `_anysearch_specs()` 两个"配置驱动展开"的先例可以照抄。

### 2.13 测试基线（本次实测）

```
$ python -m unittest discover -s tests -t tests -v
Ran 240 tests in 7.968s
OK
```

| 文件 | 用例数 | 关注点 |
|---|---|---|
| `test_radar_pipeline.py` | 54 | 流水线端到端 |
| `test_ai.py` | 39 | 主备切换、token 限额、密钥、错误分类 |
| `test_radar.py` | 37 | 分词/相似度、去重聚类、打分、主张状态、**离线调查**、自由文本检索、源注册表、推送安全 |
| `test_papers.py` | 32 | 论文分类与 AI |
| `test_anysearch.py` | 23 | 发现源、外部取证纪律、节流、垂直参数、错误卫生 |
| `test_web.py` | 21 | 页面/外壳/脚本契约/设计令牌 |
| `test_github_regression.py` | 17 | **原有 12 表、13 接口、调度线程名、digest/prefs 行为未变** |
| `test_phase2.py` | 17 | 关键词语法、已读、榜位历史、自定义 RSS、调度默认值 |

**测试纪律**（`tests/_bootstrap.py`）：每个测试模块先 `import _bootstrap`，它会
①把仓库根加进 `sys.path`；②把 `GITPULSE_DATA/GITPULSE_DB` 指向 `.testtmp/<随机>`；
③把 `RADAR_CONFIG / RADAR_SECRETS / AI_CONFIG / AI_SECRETS` 全部重定向到临时文件，**确保测试永不写坏真实 `social_radar.json` / `filter-bubble.db`**；④固定 host/port。
**→ 新测试必须沿用这个 bootstrap，且必须离线（不打网络）。**

### 2.14 Git 状态与在途改动

- 分支 `main`，HEAD `36397da feat(radar): AnySearch 支持垂直域必填参数（params 透传）`，`origin` 已配置（与 `origin/main` 同步到 `36397da`，工作树领先）。
- **未提交改动 15 个文件 + 3 个未跟踪项**：

```
 M app.py (+149)          core/ai_api.py   core/ai_brief.py   core/ai_settings.py
 M core/brief.py(-335)    core/digest.py   core/github.py     core/notify.py
 M docs/ui.md   tests/test_ai.py
 M web/app.js   web/index.html   web/settings.html   web/settings.js
 M web/status.html   web/status.js   web/styles.css (+98)
?? core/bookmarks.py   web/bookmarks.html   web/bookmarks.js   prototype/
```

- 从 `git diff app.py` 看，在途工作是**一个收藏夹模块**：新增 `/api/bookmarks`、`/api/bookmarks/folder`、`/api/bookmarks/move`、`/api/bookmarks` 分页、`bookmarks.toggle/exists/init`，并把 `index.html` 的侧栏加了 `/bookmarks.html` 入口。
- **`web/radar.html`、`web/status.html` 等的 "M" 是 CRLF/行尾噪声**（`git diff -- web/radar.html` 输出为空），实际内容未变。
- **`core/radar/**` 与 `web/radar.js` 当前是干净的**——这意味着雷达侧改动冲突面较小，但 **`web/styles.css` 和 `app.py` 是热的**。

---

## 3. 十项重点评估的逐条回答

### 3.1 现有事件是否具备国家、坐标、严重程度和发布时间？

| 字段 | 现状 | 位置 / 证据 |
|---|---|---|
| **国家** | ❌ **完全没有** | `radar_events` 无任何地域列；`radar_items` 同样没有 |
| **坐标** | ❌ **完全没有** | 全库 grep `lat/lon/geo` 零业务命中 |
| **严重程度** | ⚠️ **有替代物，但不是威胁等级** | `heat`(0–92，**相对当日重标定**，跨日不可直接比) + `importance`(0–63.7) + `confidence`(仅 `reported`/`unverified`) + `metrics.flags`（来源集中/单一平台/旧闻重传）。**"相对重标定"这点很关键：不同日期的 heat 不能直接画一条趋势线。** |
| **发布时间** | ⚠️ **一半没有** | `radar_items.published_at` 空值 1578/3144；事件级 `first_seen_at` 是"最早材料时间"，但热榜条目无时间时会回落到采集时刻 |
| 可替代的时间锚 | ✅ `collected_at` / `first_seen_at` / `last_updated_at` / `radar_event_snapshots.date` | — |
| 主题分类 | ⚠️ 有名无实 | `category` 95.5% 为 `综合` |
| 出处与证据 | ✅ 很完整 | `radar_items.url/platform/source_type/language`、`radar_evidence`(url/quote/published_at/first_hand/independent_group)、`radar_reports[investigation]` 全链路 |

**→ 直接后果：①地图现在做不出来（没坐标）；②国家不稳定性指数现在算不出来（没国家）；③"7 天威胁趋势"要先解决 `heat` 的相对标定问题。**

### 3.2 哪些现有来源已经能够提供全球新闻及地理事件？

- **全球新闻**：`GDELT`（已接入但**已熔断暂停**，历史采到 100 条）、`Hacker News`（community）、`TechCrunch`（en）、`cn_world`（中新网国际）、`BBC 中文`/`DW 中文`（已暂停）。
- **GDELT 是被浪费掉的关键资产**：`sources/intl.py:54` 的 `gdelt_doc()` 里，响应字段 `sourcecountry` 和 `domain` **确实被读到了**，但只写进了一行文本：

```python
raw_content="GDELT · %s · %s" % (entry.get("domain", ""), entry.get("sourcecountry", "")),
```

  → **来源国在手里，却在入库时被拍扁成字符串。** 这是本次方案"可以零成本拿回国家维度"的最大机会点（不需要新源，只要在 `make_item` 里多带一个字段）。
- **结构化地理事件**：**一个都没有接入**。USGS / GDACS / EONET 等只存在于 `prototype/` 原型里，运行时代码零引用。

### 3.3 如何复用现有事件聚类，而不是再次聚类生成重复事件？

**明确原则：五个面板 + 地图全部是 `radar_events` / `radar_stories` / `radar_event_snapshots` 的只读消费者，一行聚类逻辑都不新增。**

具体分工：
1. **新闻类新源**（新增国际 RSS 等）→ 走现有 `SOURCES`/`ADAPTERS` + `make_item` → 进 **`radar_items`** → 自动被现有 `cluster.build_events()` 消费。**不新增任何聚类代码。**
2. **结构化地理点**（地震/灾害/火山…）→ **不进新闻聚类**，写独立新表 `radar_geo_points`。理由有三：①它们的"标题"是程序生成的，塞进 token 相似度会**污染新闻事件标题**；②它们量大且与新闻不同频（USGS 一天几十条），会把 `cluster` 的 O(n²) 候选池挤爆（§2.4 的天花板）；③它们本来就不需要聚类——GeoJSON 一条就是一条事实。
3. **"新闻事件 ↔ 地理点"的关联**用**时间+空间邻近**做**只读标注**（例如"该事件 300 km / 24 h 内有 M5.2 地震与一条 GDACS 洪水警报"），**不做合并、不写回事件**。这就是"交汇/收敛"能力的正确实现方式，且完全不动聚类。
4. 事件→故事的两级结构保持不变，新面板默认**以 `story` 为展示粒度**（聚合度更高、更少重复），需要时下钻到 `event_id`。

### 3.4 哪些数据源适合增加 GDELT、USGS、ACLED 或 UCDP 等适配器？

先说实测结论（详见 §6.3 的完整表）：

| 源 | 本次实测 | 判断 |
|---|---|---|
| **USGS**（地震 GeoJSON） | ✅ **200 通过**，67–675 ms | **最优先接入**：免 key、公共领域、自带经纬度/震级/时间，是"结构化地理点"的理想起点 |
| **GDELT Doc API** | ❌ **429 Too Many Requests**（该 IP 公共额度紧张） | **适配器已经有**（`gdelt_doc`），**要做的是"修好它"而不是新写**：收窄 query、拉长 `min_interval`（现 15 s）、降低 `max_items`、并**把 `sourcecountry` 落成正式字段** |
| **GDELT Geo API** | ❌ `/api/v2/geo/geo` **404 / TLS 握手超时** | ⚠️ **待验证**：该端点在这台机器上不可用（`🔶原型实测` 同样记为 404，端点可能已下线）。**不要按它设计** |
| **UCDP GED** | ❌ **401**，响应头明示 `API token required. Add header: x-ucdp-access-token` | ⚠️ **待验证**：技术上可接入，但**必须先申请免费 token**；接入前不能假设可用 |
| **ACLED** | ❌ `api.acleddata.com` **TLS 被中断**（`UNEXPECTED_EOF_WHILE_READING`）；官网本身 200 | ❌ **暂不接入**：本机网络层不可达，且 ACLED 还要注册审批 + 有再分发限制 |
| **ReliefWeb** | ❌ **410 Gone** | ❌ **不要接入**，API 已下线 |

**推荐的接入顺序**：USGS（立刻可用）→ GDELT（修现成的）→ GDACS/EONET（🔶原型实测 200，需复验）→ UCDP（等 token）→ ACLED（搁置）。

### 3.5 国家风险和战略风险需要哪些可靠数据？哪些可以计算，哪些暂时缺少依据？

**能算的（今天就有数据）：**
- **媒体侧国家热度/负面聚集度**：把已有事件按国家聚合，用 `heat/importance/confidence/source_count/news_domains` 算。**前提是先有国家字段。**
- **信息覆盖风险（战略风险的一部分）**：`radar_reports.coverage` + `radar_sources` 的 `failed/paused/unavailable` **已经是权威输入**——"全球监控里哪些区域现在是盲的"这个问题现在就能答，而且很有价值。
- **可信度风险**：`confidence` 分布 + `metrics.verification` + `flags`（来源集中/单一平台/旧闻重传）**现成可用**。
- **一致性风险**：事件的 `independent_domains` 与 `news_domains` 差距、`evidence_gaps` **现成可用**。

**暂时缺少依据的（必须标"无依据"而不是编数）：**
- **真实冲突烈度（死亡/伤亡/袭击次数）**：依赖 ACLED/UCDP → **现在拿不到**。
- **国家基线（脆弱性/治理/政治稳定）**：World Bank API 实测 **200 通过**（`/v2/country` 返回含 `iso2Code/region`），可做基线；但**具体指标代码需要逐一验证**（我试的 `PV.EST` 返回 400 "indicator was not found"，说明代码必须查准）。Fragile States Index / INFORM / GPI 官网都是 **HTML 页面，没有确认的机器可读 API** → ⚠️ **待验证，不建议抓网页**。
- **灾害损失**：GDACS/EONET 提供事件与级别，🔶原型实测可达，但**本阶段未复验**。

**→ 诚实结论：现阶段只能做出"媒体可观测的紧张度/脆弱度"指标，不能做出国际机构口径的冲突烈度指数。面板上必须写明口径与"缺少依据"的字段。**

### 3.6 哪种地图库适合当前无前端构建流程的原生 HTML/JS 项目？

先摆硬约束（§2.11）：
1. **既有测试禁止三主页面出现任何 `http(s)://` 外部资源**；
2. **`web/*.css` 只允许存在 `styles.css` 一个文件**；
3. 项目自我定位是**零依赖、断网可用**（`README` 与原型方案都强调）；
4. 但 **CDN 与瓦片服务实测全部可达**（jsDelivr/unpkg/staticfile 的 Leaflet 1.9.4、ECharts 5、d3 v7、topojson 均 200；OSM / CARTO / 高德瓦片均 200）。

三个候选：

| 方案 | 优点 | 代价 | 是否踩测试 |
|---|---|---|---|
| **A. 零依赖内联 SVG 矢量底图**（`🔶原型已验证`：Natural Earth 110m → 62 KB SVG 路径，Web Mercator，断网可用） | 无依赖、无网络、体积小、与项目气质一致、暗色主题好做、`stroke-dasharray` 做点位聚类很自然 | 无街道细节、无地名标注、缩放需自己做 | ✅ 全绿 |
| **B. Leaflet 本地内置**（`web/vendor/leaflet.js` + 把 `leaflet.css` **并入 `styles.css`**） | 交互成熟（缩放/平移/弹窗/瓦片）、社区熟悉 | 多 ~150 KB 本地文件；**瓦片仍需联网**（会打破"断网可用"）；把第三方 CSS 混进 `styles.css` 增加维护成本；要处理 `no-cache` 下的体积 | ⚠️ 需把 CSS 合并进 `styles.css`，否则踩第 2 条 |
| **C. CDN 直接引 Leaflet** | 最省事 | 违背项目原则；写进 HTML 会直接 `fail` 测试；即便用 JS 运行时注入**绕过测试**，也是"测试通过但违背测试意图" | ❌ **明确否决** |

**推荐 A 为第一版，B 作为后续可选增强（且瓦片层默认关闭）。** 理由：本阶段要展示的是"事件的**空间分布**"，不是"可缩放到街道级别的地图"；A 方案零风险、零依赖、断网可用，且原型阶段已证明能画出来。若之后确实需要细节底图，再按 B 升级，**并把瓦片标注为"需联网，离线时自动隐藏"**。

**底图合规提醒（继承原型方案的重要风险项）**：Natural Earth 的国界表述在国内使用**存在合规风险**。若该页面需要对外展示，中国部分应改用**自然资源部标准地图**（`bzdt.ch.mnr.gov.cn`，免费、带审图号）。**这是合规问题，优先级高于技术选型。**

### 3.7 如何保持 SQLite、现有 API、原页面结构和运行方式兼容？

| 维度 | 保持方式 |
|---|---|
| **SQLite** | ①新字段一律走现成的 `store._ensure_column()`（`PRAGMA table_info` 判存在 → `ALTER TABLE ADD COLUMN`），**只加列、不改列、不改类型**；②新表一律 `CREATE TABLE IF NOT EXISTS` + `radar_` 前缀；③**不引入任何新依赖、不换 ORM、不改 `journal_mode`**；④所有新写入复用流水线同一个连接与事务，规避已出现过的 `database is locked` |
| **现有 API** | **15 个雷达接口的路径、方法、返回结构一个都不改**。新增走**新路径**（`/api/radar/intel*`）。日报 `payload` 只**追加键**（`version` 字段已有，按需 +1），**旧键 `zh_web/intl/tech/highlights` 全部保留**，保证历史 `radar_reports` 仍可渲染 |
| **原页面结构** | ①`radar.html` **只加 1 个 tab + 1 个 `<section>` + 1 个 `<script>`**，现有 6 个 tab 与 DOM 一字不动；②新面板复用一个独立 JS 文件 `web/radar-intel.js`，**不动 `radar.js` 的既有函数体**（只在 `switchView` 里加一个分支）；③`styles.css` **只追加新小节**，不修改任何既有规则；④满足 `test_radar_has_every_id_radar_js_uses`（新 id 都写进 HTML） |
| **运行方式** | 保持单进程 / 单 SQLite / 线程调度；新采集**挂在现有 `scheduler_loop`**（同一 `stop` Event），**不起第二个调度器**；异常全部 `try/except` 兜住，绝不传播到 GitHub 模块 |
| **推送/通知** | 不改 `push.py` 行为；新面板默认不推送，若将来要推必须走现有 `radar_push_log` 去重 |

### 3.8 哪些功能可以不依赖大模型完成，以减少 Token 消耗？

**五个面板全部可以不依赖模型完成**（这是设计目标，不是愿望）：

| 面板 | 零 LLM 实现方式 | 现有可复用件 |
|---|---|---|
| Threat Timeline | 对 `radar_event_snapshots` 做**纯算术聚合**（见 §6.7） | `radar_event_snapshots` / `radar_reports.stats` |
| Country Instability Index | 国家分组计数 + 加权求和 | `radar_events`（加国家字段后）/ USGS / World Bank |
| Strategic Risk Overview | 5 个输入的加权合成，输入全部来自已有列 | `radar_reports.coverage` / `metrics.flags` / `confidence` |
| Intelligence Feed | 直接读 `radar_reports.highlights`；摘要缺口用 `summarize.extractive()`（**只拼标题、不编事实**） | `report.highlight_story()` / `summarize.extractive()` |
| Live News | 直接读 `radar_items`，标题+来源+链接 | `radar_items`（已有 3144 条） |
| 地图 | 只做**坐标渲染 + 投影**，零语义 | §6.6 的内联矢量底图 |

**模型只在三处可选出现**：①把 `extractive()` 的拼接摘要升级成跨来源归纳；②给"战略风险"生成一句人话解读；③给国家面板生成简报。**三处都必须：先查 `radar_summaries` 式缓存 → 失败静默回退到零 LLM 版本 → 在 UI 上标注 `provider=extractive` 让用户知道这句话不是模型写的。**
另外要**把好钢用在刀刃上**：现有 token 额度实测已用尽（主备都触发 `AI 暂停到 2026-10-27`），**不要给每个面板都加一次模型调用**。

### 3.9 哪些功能需要外部 API Key、付费服务或许可？

| 项 | 需要 | 现状 | 备注 |
|---|---|---|---|
| 大模型（可选） | Key | ✅ 已有主/备 + token 限额 | 面板可以完全不用 |
| AnySearch | Key（可空） | ✅ 已配 `ANYSEARCH_API_KEY` | 空值走匿名通道但额度更低；**实测已 429** |
| Server酱推送 | SendKey | ✅ 已配 | 与本次无关 |
| **USGS** | **无** | — | 美国联邦政府作品，公共领域 |
| **Natural Earth** | **无** | — | 公共领域 |
| **World Bank API** | **无** | — | 通常为 CC BY 4.0，**需署名** ⚠️待核原文 |
| **GDELT** | **无 Key，但有限额与使用条款** | ⚠️ 实测 429 | 使用条款对再分发/商用有限制，⚠️待核 |
| **UCDP** | **要免费 token**（`x-ucdp-access-token`） | ❌ 未申请 | ⚠️待验证；数据许可与署名要求 ⚠️待核 |
| **ACLED** | **要注册审批 + OAuth 凭据**，且明确限制再分发 | ❌ 网络亦不可达 | 暂不接入 |
| **GDACS / EONET** | 无 Key | 🔶原型实测 200，本阶段未复验 | 需署名 ⚠️待核 |
| **OSM / CARTO / 高德 瓦片** | 无 Key（实测可达） | ✅ 但仅方案 B 需要 | **各自都有使用条款**：OSM 瓦片政策禁止系统性/批量抓取并要求署名；高德免 key 端点**并非面向第三方应用的正式授权**。⚠️ **若走方案 B 必须逐一核对条款** |
| **Leaflet / ECharts / d3 / topojson / world-atlas** | 无 | ✅ CDN 可达 | Leaflet BSD-2 / ECharts Apache-2.0 / d3 ISC / topojson ISC（**许可结论来自公开常识，未逐一下载 license 原文核对** ⚠️） |
| **World Monitor 本身** | — | — | **AGPL-3.0-only + 独立商标政策**：可借鉴公开文档中的公式与架构思路并自行重写；**不得复制其源码文件**；名称/Logo 不可白标（`🔶原型阶段结论`） |

**本文所有许可结论都标注为"待核原文"，因为本阶段我没有去逐份拉取许可证全文。落库前应逐一核实。**

### 3.10 如何保证地图、情报面板和新闻列表共享同一份事件数据？

**用"一个聚合接口 + 一次请求 + 一个前端状态对象 + 一个 join 键"四件事保证：**

1. **后端只有一个计算入口** `core/radar/intel.py`，所有面板的数字都由它从**同一批表**、**同一个时间窗参数**、**同一次请求**算出。**绝不允许各面板各自写 SQL、各算各的**（那必然出现"地图 12 个点、列表 13 条"的数字打架）。
2. **一个聚合接口** `GET /api/radar/intel?window_hours=&date=&story_limit=&news_limit=` 返回 `{threat_timeline, country_index, strategic, intelligence, live_news, map, degraded}`，**五个面板 + 地图全部从这一个响应渲染**。
3. **前端一个状态对象** `state.intel` = 该响应；`radar-intel.js` 内部所有渲染函数都读它，任何筛选（地区/主题/时间窗）都**只过滤这一份数据**，不重新请求。
4. **一个 join 键**：`story_id`（主）+ `event_id`（明细）。地图点位、情报卡、国家面板的行、时间轴的当日明细**全部携带同一组 id**，因此"点地图上的点 = 打开同一张情报卡 = 高亮同一条 Live News"是天然成立的。
5. **轮询入口也只走这一个接口**：Live News 想更快刷新就用 `GET /api/radar/intel/live?cursor=` —— 它**复用 `intel.py` 里同一个取数函数**，只是限制返回体量，保证与聚合接口口径一致。

---

## 4. 五个能力与现状的差距矩阵

| 能力 | 现成可用 | 缺什么 | 阶段 |
|---|---|---|---|
| **① Threat Timeline**（过去 7 天威胁趋势） | ✅ `radar_event_snapshots` 每日 item_count/platform_count/heat；✅ `radar_reports.stats` 每日总量 | ⚠️ **历史仅 3 天**；⚠️ `heat` 是**当日相对标定**，跨日不可直接比 → 需定义"每日威胁等级"绝对量并**物化**；缺日级威胁表 | **一（无地理）** |
| **② Country Instability Index** | ⚠️ 事件可信度与媒体覆盖度可用 | ❌ **无国家字段**；❌ 无国家基线表；❌ 无冲突烈度来源 | **三（先地理，再 CII）** |
| **③ Strategic Risk Overview** | ✅ `coverage`/`sources`/`confidence`/`metrics.flags`/`evidence_gaps` 全部现成 | 需一个**可解释的合成公式** + 输入可见 | **一** |
| **④ Intelligence Feed** | ✅ `radar_reports.highlights` 字段极其完整（含 `confidence_label`、`angles`、`sources`、`is_new`） | 只需新接口 + 排序/分页；`NEW` 已有 `seen_items` 机制 | **一** |
| **⑤ Live News** | ✅ `radar_items` 3144 条，含 collected_at/source/platform/url/language | 需**新接口 + 游标分页**；需处理 50% 无发布时间、language 不一致 | **一** |
| **⑥ 交互式世界地图** | 🔶 原型已验证底图与质心算法 | ❌ 无坐标字段；❌ 无地名库；❌ 无前端地图组件 | **二** |

---

## 5. 建议新增与修改的文件清单

**原则：全部新增为主，修改限"加列/加键/加分支"，不动既有语义。**

### 5.1 新增文件

| 文件 | 行数估计 | 职责 |
|---|---|---|
| `core/geo/__init__.py` | ~20 | 地理模块入口（**独立于 radar**，便于单测与复用） |
| `core/geo/gazetteer.py` | ~200 | 地名库 + 匹配器：国家名/别名/主要城市/一级行政区 → `(country_code, lat, lon, confidence)`；库以 JSON 形式内置（由 Natural Earth populated places 生成，先做 500–2000 词规模） |
| `core/geo/geocode.py` | ~180 | 标题/正文 → 国家与坐标；**多命中取高频、无命中返回 `None`**；输出 `geo_hits`/`geo_confidence`（`high`=≥2 条不同平台命中 / `low`=单条 / `none`） |
| `core/geo/centroid.py` | ~120 | 事件/故事地理质心：成员条目坐标**加权质心**，**经度用圆均值**防 ±180° 回绕；故事继承代表事件坐标（🔶原型已验证算法） |
| `core/geo/sources.py` | ~260 | 结构化地理源适配器：USGS（先做）→ GDACS / EONET；走现有 `core/radar/fetch.py` 的超时/重试/限速；**输出统一点位结构，不产出新闻 item** |
| `core/geo/baseline.py` | ~120 | 国家基准：内置国家质心与中文名；可选从 World Bank 拉基线（**缺数据时留 `None` 并标注**，绝不填 0） |
| `core/radar/classify.py` | ~200 | `classify_theme(title, keywords) -> theme`（闭合主题表）与 `classify_region(items) -> region`（`cn/intl/unknown`）；规则写在 `social_radar.json` 便于手改 |
| `core/radar/intel.py` | **~450** | **本方案的核心**：从既有表只读聚合出五个面板 + 地图所需数据；含 `threat_series()`、`country_index()`、`strategic_overview()`、`intelligence_feed()`、`live_news()`、`map_payload()`；**零 LLM** |
| `web/radar-intel.js` | ~600 | 新面板前端：内联 SVG 世界地图（投影/缩放/平移/点位聚类/弹窗/图层开关）、时间轴、CII 表、战略风险卡、情报流、Live News；暴露 `window.RBIntel` 单例 |
| `web/world-110m.js` | ~62 KB | **一次性生成**的内联世界底图（Natural Earth 110m → Web Mercator SVG path），断网可用（🔶原型产物形态已验证） |
| `scripts/build-basemap.py` | ~120 | 离线一次性工具：geojson → `web/world-110m.js`（**开发期工具，不进运行时**） |
| `scripts/build-gazetteer.py` | ~140 | 离线一次性工具：GeoNames/Natural Earth → `core/geo/gazetteer_data.json` |
| `core/geo/gazetteer_data.json` | 视规模 | 地名库数据（纯数据，可评审、可手改） |
| `tests/test_intel.py` | ~250 | 五个面板的**离线**单测（用固定 fixture 造库，断言公式与降级行为） |
| `tests/test_geo.py` | ~200 | 地名匹配、质心（含 ±180 回绕）、置信分层、**"无命中必须返回 None 而不是默认坐标"** |
| `docs/global-intel-design.md` | 本文件 | 方案 |

### 5.2 修改文件（逐处说明改动范围）

| 文件 | 改动 | 风险控制 |
|---|---|---|
| `core/radar/store.py` | ①`init()` 里追加 `_ensure_column` 调用与新表 `CREATE TABLE IF NOT EXISTS`；②新增查询函数（`snapshots_between` / `country_daily_upsert` / `threat_daily_upsert` / `geo_points_insert` / `items_stream` …）。**既有函数签名与语义不改。** | 全部走现成迁移机制；只加不改 |
| `core/radar/sources/__init__.py` | ①`SOURCES` 追加 `kind="geo"` 的 spec（USGS 等）；②`ADAPTERS["geo"] = geo.sources.fetch`；③`collect()` 里对 `kind=="geo"` 分流写入 `radar_geo_points`（**不进 `radar_items`**）。 | 分流必须**短路**在写 items 之前；geo 源失败不影响新闻源 |
| `core/radar/cluster.py` | `build_events()` 产出事件时调用 `geo.geocode`/`geo.centroid` 填入 `country_code/lat/lon/geo_label/geo_confidence`；故事继承代表事件坐标（**约 5–8 行**）。聚类阈值与算法**一行不改**。 | 地理失败不得影响聚类（`try/except` + 留空） |
| `core/radar/score.py` | ①`event["category"] = topic or "综合"`（`score.py:204`）改为落到闭合主题表（`classify.classify_theme`），保留 `interest_hits` 不动；②新增 `severity()` 把可解释的严重度分项写入 `metrics`。 | `scoring` 权重结构不变，保证旧日报可重算 |
| `core/radar/report.py` | ①把 `report.py:238-260` 的**两个硬编码平台白名单**换成 `region` 驱动，**旧键 `zh_web/intl/tech` 全部保留**作为兼容回退；②`highlight_story()` 输出追加 `geo`/`region`/`theme`；③追加 `payload["threat"]`/`payload["strategic"]`（**新键，不改旧键**）。 | 历史 `radar_reports` 必须仍能渲染 → 新键只增不替 |
| `core/radar/api.py` | 追加 GET `/api/radar/intel`、`/api/radar/intel/live`、`/api/radar/intel/map`、`/api/radar/intel/countries`；并在 `_save_settings` 白名单追加 `geo` 段可编辑键。**`handle_get` 既有分支顺序不变。** | 新路由放在 `return _err(404)` 之前；旧路径零改动 |
| `core/radar/service.py` | 在 `collect_and_report()` 的**同一个事务**里、`report.build()` 之后追加：①物化当日 `radar_threat_daily`；②物化 `radar_country_daily`；③`geo` 源采集（挂在现有 `collect_only`/`sources.collect` 内）。**不改调度时间线、不加线程。** | 置于 `conn.commit()` 之前，复用同一连接，规避 `database is locked` |
| `core/radar/config.py` | `DEFAULTS` 追加 `geo` 段（图层开关/间隔/地名阈值/收敛参数）与 `themes` 段。 | 缺省即可运行（现有 `_merge` 保证部分配置也能跑） |
| `social_radar.json` | 追加同上的 `geo`/`themes` 配置块。 | 纯追加键 |
| `web/radar.html` | **只加 3 处**：①`radarTabs` 里加 1 个 `<button class="radar-tab" data-view="intel">全球态势</button>`；②加 1 个 `<section class="radar-view" id="view-intel" hidden>`（内含各面板容器 id）；③加 `<script src="/radar-intel.js"></script>`（放在 `radar.js` 之前或之后均可，需与初始化顺序一致）。**现有 6 个 view 与 topbar 一字不动。** | 满足 `test_no_external_assets`（本地路径）；所有 `$("#id")` 都写进 HTML |
| `web/radar.js` | 仅 2 处：①`switchView()` 里加 `if (name === "intel") RBIntel.ensureLoaded();`；②`bind()` 里可加一处 tab 事件透传。**既有 render/load 函数体不动。** | 不触碰既有函数 |
| `web/styles.css` | **末尾追加**一小节（`.intel-*` 命名空间），全部使用既有 `--fb-*` 令牌。**不修改任何既有规则。** | 满足单 CSS 文件约束与设计令牌测试 |
| `docs/radar.md` | 追加新接口与地理模块说明；**修正"51 个测试"为 240**。 | 文档 |
| `tests/test_web.py` | 仅在"新增 id 由 JS 动态渲染"时才追加 `DYNAMIC_IDS`；**优先把 id 写进 HTML 以避免改测试**。 | 尽量不改测试 |

### 5.3 明确**不做**的事

- ❌ 不建第二套新闻采集/聚类/打分系统；
- ❌ 不引入股票行情、金融交易、飞机/船舶追踪、卫星监控、直播电视、摄像头、预测市场、供应链级联等 World Monitor 其他面板；
- ❌ 不引入构建步骤、npm 依赖、CDN 运行时依赖；
- ❌ 不新增一级页面（保持 `radar.html` 内的 tab 模式，符合既有测试契约）；
- ❌ 不改 `journal_mode`、不引 ORM、不动 `core/db.py` 的原表定义；
- ❌ 不覆盖/删除任何现有功能、不迁移数据库、不删文件。

---

## 6. 详细设计

### 6.1 数据库扩展方案（全部增量、幂等）

**A. `radar_events` 加列**（走 `_ensure_column`，已实测可用）：

```sql
ALTER TABLE radar_events ADD COLUMN region       TEXT;    -- cn | intl | unknown
ALTER TABLE radar_events ADD COLUMN country_code TEXT;    -- ISO-3166 alpha-2，大写
ALTER TABLE radar_events ADD COLUMN geo_lat      REAL;
ALTER TABLE radar_events ADD COLUMN geo_lon      REAL;
ALTER TABLE radar_events ADD COLUMN geo_label    TEXT;    -- 命中的地名原文，便于人工复核
ALTER TABLE radar_events ADD COLUMN geo_confidence TEXT;  -- high | low | none
ALTER TABLE radar_events ADD COLUMN severity     REAL;    -- 0-100，跨日可比（见 6.7）
ALTER TABLE radar_events ADD COLUMN severity_components TEXT;  -- JSON，逐项可解释
```

**B. `radar_items` 加列**：`region TEXT`、`country_code TEXT`。
**说明**：GDELT 的 `sourcecountry` 在 `make_item` 时即可填入 `country_code`（来自来源国，**是"报道者所在国"而不是"事件发生国"** —— 必须在 UI 上区分这两个概念，绝不能混用！建议字段语义定为 `origin_country`=来源国，事件发生国由标题地理编码得到，两者都存）。

**C. 新表（4 张，`IF NOT EXISTS`）：**

```sql
-- ① 日级威胁快照（物化）：让 7 天趋势变成一次简单 SELECT，且不受 radar_items 90 天清理影响
CREATE TABLE IF NOT EXISTS radar_threat_daily (
    date TEXT PRIMARY KEY,
    level REAL NOT NULL DEFAULT 0,
    components TEXT NOT NULL DEFAULT '{}',   -- {"intensity":..,"growth":..,"breadth":..,"verification_gap":..,"weights":{..}}
    event_count INTEGER NOT NULL DEFAULT 0,
    story_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    unverified_share REAL NOT NULL DEFAULT 0,
    distinct_platforms INTEGER NOT NULL DEFAULT 0,
    history_days INTEGER NOT NULL DEFAULT 0, -- 计算当日的可用历史深度，用于诚实降级
    computed_at TEXT NOT NULL
);

-- ② 国家日级聚合（物化）：CII 的输入，避免每次请求扫全表
CREATE TABLE IF NOT EXISTS radar_country_daily (
    date TEXT NOT NULL,
    country_code TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    media_volume REAL NOT NULL DEFAULT 0,
    severity_sum REAL NOT NULL DEFAULT 0,
    severity_max REAL NOT NULL DEFAULT 0,
    conflict_n INTEGER NOT NULL DEFAULT 0,
    disaster_n INTEGER NOT NULL DEFAULT 0,
    unrest_n INTEGER NOT NULL DEFAULT 0,
    unverified_share REAL NOT NULL DEFAULT 0,
    components TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (date, country_code)
);
CREATE INDEX IF NOT EXISTS idx_radar_country_daily ON radar_country_daily(country_code, date DESC);

-- ③ 结构化地理点（地震/灾害/火山…，不进新闻聚类）
CREATE TABLE IF NOT EXISTS radar_geo_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL,            -- usgs_quake | gdacs | eonet | ...
    external_id TEXT,               -- 源侧 id，用于幂等
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    title TEXT,
    detail TEXT,
    magnitude REAL,                 -- 通用强度（震级/警报级别数值化）
    severity TEXT,                  -- 源侧级别原文
    observed_at TEXT,               -- 事件发生时间
    source_id TEXT,
    url TEXT,
    url_hash TEXT NOT NULL UNIQUE,  -- 复用现有 URL 归一化去重思路
    collected_at TEXT NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_radar_geo_points_layer ON radar_geo_points(layer, observed_at DESC);

-- ④ 国家基准（缺依据时 baseline 为 NULL，绝不填 0）
CREATE TABLE IF NOT EXISTS radar_geo_baseline (
    country_code TEXT PRIMARY KEY,
    name_zh TEXT,
    name_en TEXT,
    lat REAL, lon REAL,             -- 国家质心，用于无坐标事件的上图锚点（低置信）
    baseline REAL,                  -- 0-1，可为 NULL = 缺少依据
    baseline_source TEXT,           -- 例如 worldbank:PV.EST@2024，便于追溯
    updated_at TEXT
);
```

**D. 为什么不建"图层注册表"表**：直接复用 `radar_sources`。每个地理图层注册为一条 `kind="geo"` 的 source spec，**源健康、失败计数、延迟、熔断、`/api/radar/sources` 展示全部自动获得**，无需新表和新的状态接口。图层开关走 `social_radar.json → geo.layers`（与现有 `sources` 覆盖机制一致）。

**E. 兼容性保证**：以上全部是 `ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`，对现有 15 个接口与全部既有查询**透明**。存量 886 条事件的 `country_code/geo_*` 为 `NULL`，正确表达"未知"，**不做任何猜测性回填**。

### 6.2 数据来源：认证、额度、许可与更新限制

**本次实测结果（2026-10-26，本机直连，无代理）：**

| 端点 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| `earthquake.usgs.gov/.../4.5_day.geojson` | ✅ **200** | 675 ms | 免 key |
| `earthquake.usgs.gov/.../significant_week.geojson` | ✅ **200** | 609 ms | 免 key |
| `api.worldbank.org/v2/country` | ✅ **200** | 370 ms | 免 key，返回含 `iso2Code/region` |
| `api.worldbank.org/v2/country/CHN/indicator/PV.EST` | ⚠️ **400** | 762 ms | **指标代码不存在** → 代码必须查准 |
| `raw.githubusercontent.com/.../ne_110m_admin_0_countries.geojson` | ✅ **200** | 489 ms | 底图几何 |
| `unpkg.com/world-atlas@2/countries-110m.json` | ✅ **200** | 721 ms | TopoJSON，更小 |
| `cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json` | ✅ **200** | 301 ms | — |
| `cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.{js,css}` | ✅ **200** | ~300 ms | 方案 B 才需要 |
| `unpkg.com/leaflet@1.9.4` | ✅ **200** | 322 ms | — |
| `cdn.staticfile.org/leaflet/1.9.4/leaflet.js` | ✅ **200** | 283 ms | 国内 CDN 可用 |
| `npm.elemecdn.com/...` | ❌ **404** | 245 ms | 失效，勿用 |
| `cdn.bootcdn.net/...` | ❌ **超时** | 12 057 ms | 勿用 |
| `cdn.jsdelivr.net/npm/echarts@5.5.1` | ✅ **200** | 319 ms | 方案 B/图表才需要 |
| `cdn.jsdelivr.net/npm/d3@7` / `topojson-client@3` | ✅ **200** | ~300 ms | — |
| `tile.openstreetmap.org/{1,2}/...` | ✅ **200** PNG | 306 ms | **OSM 瓦片政策**限制批量使用并要求署名 |
| `basemaps.cartocdn.com/dark_all/...` | ✅ **200** PNG | 330 ms | 有使用条款 |
| `webrd01/webrd02.is.autonavi.com/appmaptile…` | ✅ **200** PNG | ~80 ms | 高德，**中文标注最合适**；但免 key 端点非正式第三方授权 ⚠️ |
| `wprd01.is.autonavi.com/...style=7` | ✅ **200** PNG | 87 ms | 同上 |
| `rt0.map.gtimg.com/tile?...` | ❌ **400** | 166 ms | 参数不对，勿用 |
| `flagcdn.com/w40/cn.png` | ✅ **200** | 321 ms | 国旗图标可用 |
| `restcountries.com/v3.1/all` | ⚠️ 200 但 `success:false` | — | 需注册/改参数，勿依赖 |
| `api.gdeltproject.org/api/v2/doc/doc` | ❌ **429** | 11 931 ms | 与 DB 熔断原因一致 |
| `api.gdeltproject.org/api/v2/doc/doc?format=html` | ✅ **200** | 15 196 ms | 主机可达，配额受限 |
| `api.gdeltproject.org/api/v2/geo/geo` | ❌ **404 / TLS 超时** | 12 021 ms | **地理端点不可用** |
| `ucdpapi.pcr.uu.se/api/gedevents/24.1` | ❌ **401** `API token required. Add header: x-ucdp-access-token` | 2 031 ms | 需申请 token |
| `api.acleddata.com` | ❌ **SSL 连接被中断** | 435 ms | 网络层不可达 |
| `acleddata.com/`（官网） | ✅ 200 HTML | 746 ms | 仅页面可达 |
| `api.reliefweb.int/v1/disasters` | ❌ **410 Gone** | 788 ms | API 已下线 |
| `drmkc.jrc.ec.europa.eu/inform-index` | ✅ 200 HTML | 1 618 ms | **无确认的机器可读 API** |
| `fragilestatesindex.org` / `visionofhumanity.org` | ✅ 200 HTML | — | **无确认 API** |
| `www.imf.org/external/datamapper/api/v1/…` | ✅ **200** | 2 287 ms | 可用但本方案不需要 |

**🔶原型阶段实测（本阶段未复验，落地前必须复验）**：NASA EONET 200、GDACS 200、OpenSky 200、Open-Meteo 200、NASA FIRMS 需 key（无 key 返回 400）、adsb.lol 免 key、AISStream 需 key、Cloudflare Radar 需 Bearer token。

**更新频率与额度建议**：

| 图层/源 | 建议间隔 | 理由 |
|---|---|---|
| USGS 地震 | 5–10 min | 免 key、数据量小、时效敏感 |
| GDACS / EONET 灾害 | 15–30 min | 日更新为主，无需高频 |
| GDELT（新闻，修好后） | **≥ 60 min，且 `max_items` 降到 10–25** | 公共额度紧张，实测 429；必须尊重现有 `backoff`（3 次降频 / 6 次停用） |
| 国家基准 | 7 天 | 几乎不变 |
| 新闻类（现有） | 保持 60 min | 不动现有节奏 |
| 瓦片（若用方案 B） | 前端按需、**默认关闭** | 使用条款 + 离线可用性 |

**关键纪律**：所有新源**必须**接入现有 `radar_sources` 熔断（`record_source_result` / `mark_source_paused`），并遵守 `min_interval_minutes` 节流（照抄 AnySearch 的做法），否则会像 GDELT 一样把额度烧光后被停用。

### 6.3 API 接口设计

**新增 4 个 GET（全部只读，路径前缀 `/api/radar/intel`，与现有 15 个接口互不干扰）：**

#### ① `GET /api/radar/intel`
主聚合接口 —— 五个面板 + 地图的**唯一数据来源**。

参数：`window_hours`(默认 168) · `date`(默认今天) · `story_limit`(默认 20) · `news_limit`(默认 50) · `region`(可选 `cn|intl|unknown`) · `theme`(可选) · `country`(可选)

```json
{
  "generated_at": "2026-10-26T02:30:00+08:00",
  "window": { "hours": 168, "from": "...", "to": "...", "days_available": 3, "days_requested": 7 },
  "threat_timeline": {
    "points": [
      { "date": "2026-10-25", "level": 61.4,
        "components": { "intensity": 0.78, "growth": 0.42, "breadth": 0.55,
                        "verification_gap": 0.61,
                        "weights": { "intensity": 0.40, "growth": 0.25, "breadth": 0.20, "verification_gap": 0.15 } },
        "event_count": 234, "item_count": 265, "distinct_platforms": 11,
        "unverified_share": 0.42, "sample": 1.0 }
    ],
    "trend": "up", "delta": 6.2,
    "level_today": 61.4,
    "history_confidence": "insufficient",   // insufficient(<5天) | fair(5-6) | ok(>=7)
    "note": "历史仅 3 天，趋势仅供参考；满 7 天后自动转为完整口径"
  },
  "country_index": {
    "countries": [
      { "code": "CN", "name": "中国", "score": 43.1,
        "components": { "conflict": {...}, "unrest": {...}, "disaster": {...},
                        "media": {...}, "baseline": { "value": null, "source": null, "missing": true } },
        "event_count": 58, "story_count": 21, "coverage": "media_only",
        "confidence": "low", "missing_inputs": ["baseline"] }
    ],
    "coverage_note": "仅统计已被地理编码的事件；未编码事件占 34%",
    "available": true
  },
  "strategic": {
    "score": 54.2,
    "components": {
      "threat_level":   { "value": 61.4, "weight": 0.30, "source": "radar_threat_daily" },
      "coverage_gap":   { "value": 0.18, "weight": 0.20, "source": "radar_sources", "detail": { "paused": 4, "unavailable": 5, "failed": 0 } },
      "verification":   { "value": 0.42, "weight": 0.20, "source": "radar_events.confidence" },
      "concentration":  { "value": 0.25, "weight": 0.15, "source": "metrics.flags" },
      "blind_spot":     { "value": 0.63, "weight": 0.15, "source": "region 分布" }
    },
    "drivers": ["4 个源已自动停用", "42% 事件未获多方验证", "国际来源占比偏低"],
    "missing_inputs": ["conflict_fatalities"]
  },
  "intelligence": {
    "items": [ { "story_id": "st-…", "event_id": "ev-…", "title": "…", "headline": "…",
                 "summary": "…", "points": [], "confidence": "reported",
                 "confidence_label": "有媒体报道，尚未多方独立验证",
                 "heat": 78.0, "importance": 63.7, "category": "综合", "theme": "other",
                 "region": "cn", "angle_count": 3, "item_count": 6,
                 "platforms": [], "flags": [], "is_new": true,
                 "geo": { "lat": 27.9, "lon": 109.8, "label": "湖南 麻阳", "country_code": "CN", "confidence": "high" },
                 "summary_provider": "openai", "earliest_source": {}, "sources": [] } ],
    "cursor": "…", "total": 12
  },
  "live_news": {
    "items": [ { "id": 16845, "title": "…", "url": "…", "platform": "知乎",
                 "source_id": "zhihu_hot", "source_type": "social_trend",
                 "language": "zh", "published_at": "", "published_at_missing": true,
                 "collected_at": "2026-10-26T02:08:21+08:00",
                 "event_id": null, "region": "cn", "country_code": null,
                 "is_trend_only": true } ],
    "cursor": "16845", "has_more": true
  },
  "map": {
    "events": [ { "event_id": "ev-…", "story_id": "st-…", "title": "…",
                  "lat": 27.9, "lon": 109.8, "geo_confidence": "high", "geo_label": "湖南 麻阳",
                  "country_code": "CN", "severity": 61.0, "heat": 74.0, "importance": 63.7,
                  "confidence": "reported", "item_count": 6, "platform_count": 3,
                  "published_at": "", "first_seen_at": "…" } ],
    "geo_points": [ { "id": 1, "layer": "usgs_quake", "lat": 35.6, "lon": 140.1,
                      "title": "M5.2 …", "magnitude": 5.2, "observed_at": "…" } ],
    "layers": [ { "key": "news_events", "label": "新闻事件", "count": 6, "enabled": true, "source": "radar_events" },
                { "key": "usgs_quake",  "label": "地震",     "count": 190, "enabled": true, "source": "USGS" },
                { "key": "gdacs",       "label": "灾害警报", "count": 0, "enabled": false,
                  "source": "GDACS", "state": "not_connected",
                  "note": "未接入或需授权；不会用假点位填充" } ],
    "convergence": [ { "grid": "35.5,140.0", "types": 3, "score": 81, "label": "日本 周边" } ]
  },
  "degraded": [ { "module": "country_index", "reason": "34% 事件尚无地理编码", "since": "…" } ],
  "llm": { "enabled": false, "used": false, "provider": "extractive" }
}
```

#### ② `GET /api/radar/intel/live?cursor=&limit=&region=&lang=`
只返回 Live News（复用 `intel.live_news()`），供 60 s 高频轮询。`cursor` = 上一批最小 `radar_items.id`（**id 单调递增，比时间戳更稳**）。

#### ③ `GET /api/radar/intel/map?window_hours=&layers=`
只返回 `map.events` + `map.geo_points`（可选 `layers=usgs_quake,news_events` 过滤），供图层开关即时切换而无需重取全部。

#### ④ `GET /api/radar/intel/countries?days=7`
只返回 `country_index.countries`（排序/分项），供表格排序。

**约定**：
- 全部 **GET + 只读**，不写库（物化在采集链路里做）；
- 全部包在 `try/except` 里（沿用 `api.py` 既有风格），失败返回 500 + `{error}`，**绝不影响其他接口**；
- `degraded[]` 是这个设计的**核心诚实机制**：任何拿不到、算不出的东西都必须在响应里显式声明，而不是静默省略或填 0。
- **不新增 POST**（避免任何写入口），复用现有 `POST /api/radar/collect` 触发重算。

### 6.4 地图技术方案

**方案 A（推荐，第一版）——零依赖内联 SVG：**

1. **底图**：一次性脚本 `scripts/build-basemap.py` 把 Natural Earth 110m 陆地/国界 GeoJSON 投影到 **Web Mercator**，输出 `web/world-110m.js`（约 62 KB，形态已在原型验证：62 021 字符 SVG path）。运行时零网络请求。
2. **渲染**：前端用一个 `<svg viewBox="0 0 W H">`，底图一个 `<path>`；事件点位按 `story_id` 生成 `<circle>`；`transform="translate/scale"` 做缩放平移。
3. **投影必须前后端同源**：`core/geo/project.py` 与 `web/radar-intel.js` 用**同一组公式**（`x = (lon+180)/360`，`y = (1 - ln(tan(π/4 + φ/2))/π)/2`），避免"后端算的点在前端偏一格"。
4. **点位聚类**：🔶原型已验证的**网格聚类**（不用 Supercluster）。电流实测点位量级为个位数到十几（12 个故事里 6 个可编码），远未到需要重度聚类的规模。
5. **地图高度三档（继承原型实测结论）**：目标屏是 **1366×768**；能显示到 ±60° 纬度的世界地图，高度约为宽度的 0.32 倍（宽 1090 px → ≈350 px），所以地图放顶部时故事卡会掉出首屏。因此：
   - 收起 **220 px**（看分布）/ 默认 **360 px**（看全 ±60°）/ 展开 **620 px**（看全 ±85°）；
   - `innerHeight < 860` 时**自动落到"收起"档**；
   - 图层面板可折叠，折起后地图吃满整宽。
6. **交互**：滚轮/双击缩放、拖拽平移、`?lat=&lon=&zoom=&layers=` 写入 URL 便于分享（原生 JS 十几行）；点选点位 → 打开对应情报卡；卡片 hover → 高亮点位。**这些全部在 `state.intel` 内完成，不重新请求。**
7. **图层说明卡**：每层显式标注「用途 / 来源 / 接入方式 / 新鲜度 / 局限」，未接入的层计数为 **0** 并弹出"该层需 key / 尚未接入，**不会用假点位填充**"。这是项目既有价值观的延续。
8. **合规**：中国部分若需对外展示，改用**自然资源部标准地图**（`bzdt.ch.mnr.gov.cn`，带审图号）。**这是合并前必须由你决定的一项。**

**方案 B（可选升级）——Leaflet 本地内置**：把 `leaflet.js`（BSD-2）放到 `web/vendor/`，把 `leaflet.css` **内容并入 `web/styles.css`**（因单 CSS 文件约束），瓦片层默认关闭、开启时用高德（中文标注，实测 80 ms）或 CARTO 暗色。**代价是打破"断网可用"，因此仅作为后续可选增强，且必须在 UI 上标注"底图需联网"。**

**明确否决**：CDN 直接引用（违反既有测试与项目原则）。

### 6.5 五个模块的前端布局与交互

**入口**：`radar.html` 的 `radarTabs` 新增第 7 个 tab —— `全球态势`（`data-view="intel"`），对应 `<section class="radar-view" id="view-intel">`。**不新增一级页面**（符合既有测试契约）。

**布局（三区，桌面三栏 / 平板两栏 / 手机单栏，复用断点 1240/1080/860/560）：**

```
┌─────────────────────────────────────────────────────────────────────┐
│ 筛选条  [时间窗 24h/7d/30d]  [地区 全部/国内/国际]  [主题 chips]  [图层▾] │
├─────────────────────────────────────────────────────────────────────┤
│ ① 交互式世界地图（三档高度，默认按 innerHeight 自动选档）                │
│    底图 SVG + 事件点位 + 地震/灾害点位 + 收敛区高亮                     │
│    图例 / 层开关 / 未接入层提示（计数 0，不造假）                        │
├───────────────────────┬─────────────────────────────────────────────┤
│ ② Threat Timeline      │ ④ Intelligence Feed                        │
│  7 天折线（SVG）+ 当日  │  精选情报卡（来自 highlights），            │
│  等级大数 + 4 个分项条  │  点击 → 复用现有「深入调查」                │
│  历史不足时显式提示     │  过滤：地区 / 主题 / 只看 NEW               │
│                        │                                             │
│ ③ Strategic Risk       │ ⑤ Live News                                │
│  综合分 + 5 个输入条    │  近实时流（60 s 轮询，游标分页）             │
│  每条标注来源表/权重    │  标题+平台+时间；无发布时间显式标注          │
│  missing_inputs 明示    │  「热榜仅发现、无正文」标注                  │
│                        │                                             │
│ ⑥ Country Instability  │                                             │
│  可排序表格：国家/总分/  │                                             │
│  冲突/社会/灾害/媒体/    │                                             │
│  基线；缺基线显「无依据」 │                                             │
│  点行 → 过滤地图与情报   │                                             │
└───────────────────────┴─────────────────────────────────────────────┘
```

**交互约定：**
- **一次请求驱动全部**：`RBIntel.load()` 拉 `/api/radar/intel` 存入 `state.intel`，所有面板 + 地图由这一份数据渲染。
- **筛选是纯客户端过滤**（作用于 `state.intel`），**不重新请求**，保证数字自洽。
- **联动**：点国家行 → 地图只显示该国点位 + 情报流过滤；点地图点 → 打开该 `story_id` 情报卡；hover 卡片 → 点位加脉冲环。
- **下钻**：情报卡沿用现有「深入调查」按钮 → `POST /api/radar/investigate`（**完全不改现有调查链路**）。
- **降级提示**：`degraded[]` 在面板顶部以 `.intel-notice` 显示（如「国家指数：34% 事件尚无地理编码」）。
- **无图表库**：折线与分项条用**手写内联 SVG / CSS 条**实现（`--fb-*` 令牌），不引入 ECharts（虽然实测 CDN 可达，但违反项目零依赖原则）。
- **轮询策略**：Live News 60 s（`/intel/live`）；其余面板 5 min 或手动刷新；**页面不可见时暂停**（`document.visibilityState`），避免无谓开销。
- **可访问性**：沿用 `prefers-reduced-motion`（关闭点位脉冲）、暗色主题令牌、键盘可达（tab 可聚焦、点位可用 Enter 打开）。

### 6.6 国家不稳定性与战略风险的可解释计算方案

**铁律：每一个数字都必须能在界面上回溯到它的原始输入；缺依据的项必须显示"缺少依据"，绝不填 0、不插值、不外推。**

#### ① 每日威胁等级 TL(d)（0–100，**跨日可比**）

必须先解决 `heat` 是"当日相对标定"的问题。因此 TL 的三个输入**全部用绝对量或结构比例**：

```
intensity(d)        = mean( top5( metrics.raw_heat ) ) / 92        # 用 raw_heat，不用被重标定过的 heat
growth(d)           = min(1, count(事件:item_count 较前一日快照增长≥50%) / 5)
breadth(d)          = min(1, distinct(platforms across events on d) / 12)
verification_gap(d) = 1 - mean(confidence != 'unverified')

TL(d) = 100 * ( 0.40*intensity + 0.25*growth + 0.20*breadth + 0.15*verification_gap )

history_days(d)     = number of days available up to d
confidence          = insufficient(<5) | fair(5-6) | ok(>=7)
```

- 数据来源：`radar_event_snapshots`（item_count/platform_count/heat）+ `radar_events.metrics`（raw_heat/platforms/previous_item_count）+ `radar_reports.stats`（交叉校验）。
- **物化到 `radar_threat_daily`**，采集时写入 → 查询是一次 `SELECT`，**且不受 `radar_items` 90 天清理影响**。
- **诚实降级**：`history_days` 与 `confidence` 随响应返回；前端 <5 天时显示「历史深度不足（3/7 天），趋势仅供参考」，**不画插值曲线**。满 7 天后自动变为完整口径——**这是唯一正确的做法，因为过去的数据根本不存在。**

#### ② 国家不稳定性指数 CII(c)（0–100）

```
conflict(c)   = min(1, conflict_events(c)/8)        # 地缘冲突/战争/袭击类事件数
unrest(c)     = min(1, unrest_events(c)/8)          # 抗议/罢工/骚乱/治安类
disaster(c)   = min(1, disaster_events(c)/6 + structured_disaster_points(c)/20)
media(c)      = min(1, event_count(c)/40)           # 媒体可见度（也是暴露度，不是危险度）
baseline(c)   = 国家基线 0-1，来源 World Bank 等；**取不到时为 NULL**

CII(c) = 100 * ( 0.30*conflict + 0.20*unrest + 0.15*disaster + 0.15*media + 0.20*baseline )

若 baseline 为 NULL → 该权重按比例分摊到其余四项，并在响应里
   missing_inputs=["baseline"]、coverage="media_only"
```

**必须写进 UI 的三句话：**
1. 「本指数基于**公开媒体与开源事件的可观测性**，不是官方冲突烈度统计」；
2. 分母（8/6/40）是**可配置的归一化常量**，不是国际标准；
3. `conflict`/`unrest` 的区分依赖 `classify_theme()` 的规则，**规则误判会直接失真**（原型里就出现过"体育比赛冲突"被判成地缘冲突）。

**复核参照（不作为计算输入）**：`🔶原型阶段记录的 World Monitor 公开文档公式` 为 `eventScore = 不稳定×0.25 + 冲突×0.30 + 社会×0.20 + 信息×0.25`、`composite = baseline×0.4 + eventScore×0.6 + boosts`。**该权重表本阶段未再次核对源码，标 ⚠️待验证；且 AGPL 项目只可借鉴思路、不可复制代码。**本方案给出的是**自己重写、输入全部可溯源**的版本。

#### ③ 战略风险综合概况 SRI（0–100）

```
SRI = 100 * ( 0.30*TL_today
            + 0.20*coverage_gap        # 源停用/不可用/失败 → 信息盲区
            + 0.20*verification_gap    # 未获多方验证的事件占比
            + 0.15*concentration       # 来源集中/单一平台 flag 占比
            + 0.15*blind_spot )        # 国际来源占比不足 / region 覆盖失衡
```

- `coverage_gap` 来自 `radar_sources`（实测：paused=4, unavailable=5, failed=0）——**这是当前最真实、最有价值的一项**；
- 权重是**可配置的**（放 `social_radar.json → geo.strategic_weights`），并把 `weights` 一并返回，保证可解释；
- `drivers[]` 返回人类可读的成因（例如"4 个源已自动停用"），`missing_inputs` 明示缺项（如 `conflict_fatalities`）。

**为什么不把"冲突死亡人数"算进去**：ACLED 网络不可达、UCDP 需 token —— **没有数据就不算，并显示"缺少依据"**。

### 6.7 分阶段实施顺序与验收标准

| 阶段 | 内容 | 验收标准（可执行、可复现） |
|---|---|---|
| **阶段 0（本次）** | 只出本文档 | ① `docs/global-intel-design.md` 存在；② `git status` 除该文档外**无新增业务代码改动**；③ 现有测试基线记录在案（240 绿） |
| **阶段 1：无地理的态势台**（最小可用） | `core/radar/intel.py` + 4 个新接口 + `radar_threat_daily` 物化 + `radar.html` 加 1 tab + 5 个面板（**不含地图**） | ① `python -m unittest discover -s tests -t tests -v` **仍 240+ 全绿**且新增 `test_intel.py` 通过；② 在**真实库的只读副本**上 `GET /api/radar/intel` 返回 200 且字段齐全；③ Intelligence Feed 的条目与 `GET /api/radar/report` 的 `highlights` **逐条一致**，Live News 条数与 `SELECT count(*) FROM radar_items` **口径一致**；④ 断言 `llm.enabled=false` 时 `/api/radar/intel` 的模型调用次数 = **0**；⑤ 现有 6 个 tab 与 15 个接口行为**逐字节不变**（回归测试）；⑥ 页面控制台 **0 报错**；⑦ 不新增任何数据源、不建新表以外的东西 |
| **阶段 2：地理维度与地图** | `core/geo/`（gazetteer/geocode/centroid/project）+ `classify.py` + 新列回填（**仅新采集的事件**）+ 内联 SVG 地图 + 地图点位=现有故事 | ① 在**人工标注的 ≥200 条真实标题样本**上实测地理编码**准确率/召回率并写进文档**；② **未命中必须返回 `None`**（单测强制：禁止默认坐标兜底）；③ 经度圆均值单测覆盖 ±180° 回绕；④ 地图**断网可用**（`test_no_external_assets` 仍绿）；⑤ 区分「来源国 `origin_country`」与「事件发生国 `country_code`」并各自单测；⑥ 地图三档高度在 1366×768 下不把故事卡挤出首屏 |
| **阶段 3：结构化图层 + CII** | USGS 适配器（**实测 200**）→ GDACS/EONET（**须先复验**）+ `radar_geo_points` + `radar_country_daily` + CII 面板 + 图层注册（复用 `radar_sources`） | ① 每个图层**可独立开关**；② 单图层失败**不影响**日报与其它图层（照抄现有熔断测试）；③ 新源遵守 `min_interval_minutes` 与 `backoff`（3 次降频/6 次停用）；④ CII 在 `baseline` 缺失时返回 `missing_inputs=["baseline"]` 且**不填 0**；⑤ 未接入图层显示计数 0 + "不会用假点位填充" |
| **阶段 4：可选增强**（各自独立） | ① 地理收敛告警（1°×1° 网格，≥2 类信号）② 时间轴回放（`radar_rank_history`/`radar_event_snapshots` 已在存）③ 国家面板下钻 ④ 底图升级为瓦片（方案 B，默认关闭） | 每项独立验收；①收敛算法的单测用固定坐标夹具；②回放只读历史、不重算；④瓦片层必须显式标注"需联网"且离线时自动隐藏 |

### 6.8 可能的兼容性风险与回归测试方案

**风险清单（按严重度排序）：**

| # | 风险 | 证据 | 缓解 |
|---|---|---|---|
| 1 | **既有测试禁止外部 CDN** | `tests/test_web.py:60` | 地图走零依赖内联 SVG；若用 Leaflet 必须本地内置。**禁止运行时注入 CDN 来"绕过"测试**（那是测试绿了但意图被破坏） |
| 2 | **只允许一份 CSS 文件** | `tests/test_web.py:165` `web/*.css == ["styles.css"]` | 不新增 `.css` 文件；第三方 CSS 内容（如 Leaflet）必须**并入 `styles.css`** |
| 3 | **`$("#id")` 与 HTML 必须对齐** | `tests/test_web.py:89` | 新面板所有 id **写进 `radar.html`**，而不是靠 JS 动态生成后再改 `DYNAMIC_IDS` |
| 4 | **工作树是脏的（bookmarks 在途）** | 15 个 M + 3 个 ??；`app.py`/`styles.css` 是热的 | **先提交或 stash**；新工作开分支；**尽量不碰 `app.py`**（新路由放 `core/radar/api.py`）；`styles.css` 只追加 |
| 5 | **SQLite 写锁竞争已真实发生** | `radar_runs` 里 `database is locked` + 3 条僵尸 `running` | 新物化写入必须在**取报的同一事务**里；不进第二个写入路径；可选加 `busy_timeout`（**属 `db.connect()` 改动，需单独评估**） |
| 6 | **`published_at` 50% 为空** | 1578/3144 | Live News 必须回落 `collected_at` 并标注 `published_at_missing:true`；**不得据此算"时效性"** |
| 7 | **`language` 取值混乱且被截断** | `zh/en/zh-CN/English/Chinese/…/Vietname/Ukrainia` | 在采集处归一化（BCP-47 主语言标签），并保留原值；`region` 判定不得只依赖语言 |
| 8 | **`category` 95.5% 是「综合」** | 846/886 | 阶段 1 **不得依赖 `category` 分栏**；阶段 2 先上 `classify.py` 再改 `report.py` 的分栏 |
| 9 | **历史仅 3 天** | `radar_event_snapshots` 2026-10-23→25 | TL 显式返回 `history_days`/`confidence`，前端如实提示，**不插值** |
| 10 | **`heat` 跨日不可比** | `score.py:226-232` 按当日最大值重标定到 92 | TL 用 `metrics.raw_heat`（绝对量）而非 `heat` |
| 11 | **新源容易被额度打死** | GDELT 已因 6 次失败被暂停；AnySearch 实测 429 | 新源必须接熔断 + `min_interval_minutes`；GDELT 先降频降量再复活 |
| 12 | **`intl` 分栏靠硬编码平台白名单** | `report.py:258-260` | 阶段 2 换 `region` 驱动并**保留旧键回退**，保证历史日报仍可渲染 |
| 13 | **`docs/radar.md` 已过期** | 说 51 个测试，实测 240；说 12 张表，实测 13 | 随阶段一并修正，避免后来者按错基线判断 |
| 14 | **静态服务把未知路径回落首页** | `app.py:129-130` | 新资源路径必须**逐个真实请求验证**，防止"看起来正常"的假象 |
| 15 | **第三方数据可被篡改** | newsnow 已有 `verify_domain` 校验 | 新源沿用域名/格式校验思路 |
| 16 | **中国地图边界合规** | 🔶原型阶段结论 | **合并前必须决定用哪套边界**；对外展示应换自然资源部标准地图 |
| 17 | **AGPL 传染** | World Monitor 是 AGPL-3.0-only | 只借鉴公开文档思路、自行重写；**不复制源码文件**；不白标其名称/Logo |
| 18 | **保留策略会清掉原始材料** | `store.prune()` 只删 `radar_items`/`radar_runs`（>90 天） | 任何需要长历史的指标**必须物化到 `radar_threat_daily`/`radar_country_daily`**，不能依赖 `radar_items` |

**回归测试方案：**

1. **基线冻结**：阶段 1 开工前记录 `Ran 240 tests / OK`（本报告已记录，含耗时 7.968 s），作为对比基准。
2. **扩充既有回归契约**：照抄 `tests/test_github_regression.py` 的写法，新增断言：
   - 原有 12 张表 + 13 个接口 + 调度线程名（`filter-bubble-scheduler` / `radar-scheduler`）不变；
   - `core/db.py` 的 `SCHEMA` 常量**不得被雷达代码修改**；
   - `radar_events` 的既有 19 列**列名与语义不变**，新列只增不改；
   - `GET /api/radar/report` 的**旧键仍在**（`highlights/zh_web/intl/tech/coverage/evidence_gaps/stats`）。
3. **新增离线单测**（**严禁打网络**，沿用 `tests/_bootstrap.py` 隔离库）：
   - `tests/test_intel.py`：TL 公式（手算期望值）、`history_days < 5` 时 `confidence == "insufficient"`、baseline 缺失时 `missing_inputs` 包含 `baseline` 且**分数不等于填 0 的版本**、`degraded[]` 存在性；
   - `tests/test_intel.py::test_no_llm_calls`：monkeypatch `llm.chat*`，断言五个面板全程 **0 次**调用；
   - `tests/test_intel.py::test_intelligence_feed_matches_report`：同一库下 `/intel` 的 `intelligence.items[*].story_id` 与 `radar_reports` 的 `highlights[*].story_id` **集合相等**；
   - `tests/test_geo.py`：地名匹配（含"日本小笠原群岛"/"也门胡塞武装"/"西藏巴青"等真实标题）、**未命中返回 `None`**、经度 ±180° 圆均值、置信分层（≥2 不同平台=`high`）；
   - 地理源适配器用 `prototype/raw/` 里的**真实抓取快照作为 fixture**，测试解析而不联网。
4. **契约测试**：扩展 `tests/test_web.py` 风格——新 tab 的 `data-view` 存在、`$("#id")` 与 HTML 对齐、`test_no_external_assets` 仍绿、`web/*.css` 仍只有一份。
5. **只读金标测试**：对**真实库的只读副本**（复制 `data/filter-bubble.db` 到 `.testtmp/`）跑 `/api/radar/intel`，只断言**响应 schema**（键存在、类型正确、数值区间合法 0–100），**不断言具体数值**（避免数据变化导致脆性）。
6. **性能守护**：在 8.5 MB 真实库上 `/api/radar/intel` 单次响应应 < 300 ms（当前数据量下聚合是几百行级）；若超阈值改用物化表直查。
7. **手工验收**：浏览器打开 `radar.html` → 切到新 tab → **控制台 0 报错** → 五个面板 + 地图数字与后端接口**逐项一致** → 现有 6 个 tab 全部回归正常。
8. **数据安全**：全程以库的**只读副本**测试；**永不**在真实 `data/filter-bubble.db` 上跑迁移以外的写入测试；所有测试经 `_bootstrap` 重定向到 `.testtmp/`。

---

## 7. 最终结论

### 7.1 现有代码哪些**可以**复用（这是本方案最重要的部分）

| 复用对象 | 用法 | 价值 |
|---|---|---|
| **`radar_events` / `radar_stories` / `radar_event_snapshots`** | 五个面板的**唯一事实来源**，只读消费 | 避免整套重复建设 |
| **`radar_reports.highlights`** | **Intelligence Feed 直接用**（已含 headline/summary/points/confidence_label/angles/sources/is_new） | 省掉一整个面板的后端 |
| **`radar_items`**（3144 条） | **Live News 直接用** | 零新增采集 |
| **`radar_event_snapshots`（每日快照）** | **Threat Timeline 的唯一可靠时序源**（因为 `radar_events.date` 是"最后活跃日"） | 时序能力已经存在 |
| **`radar_rank_history`（4523 行）** | 时间轴回放 / 榜位趋势 | 现成燃料 |
| **`score.py` 的 `metrics` 全分项** | 严重程度与可信度面板的**现成输入**（velocity/cross_platform/engagement/verification/interest/flags/weights） | 不用发明第二套打分 |
| **`SOURCES` + `ADAPTERS` + `make_item` + `fetch.py`** | 新增新闻源与地理源适配器的**现成骨架**（加源不改代码） | 扩展成本极低 |
| **`radar_sources` 健康 + 熔断 + 节流** | **图层注册与状态展示直接复用**，无需新表 | 省掉一套状态系统 |
| **`_ensure_column` 迁移机制** | 全部新字段走它 | 零迁移风险 |
| **`summarize.extractive()` 回退** | 零 LLM 摘要（已实现且已声明"不编事实"） | 省 Token |
| **`investigate.py` 本地规则引擎** | 情报卡"深入调查"下钻，**未启用模型也完整可用** | 零新增 |
| **`seen_items` + `report` 的 `is_new`**（`annotate`） | 情报流的 NEW 标签 | 现成 |
| **`push.py` + `radar_push_log`** | 若将来要推态势摘要 | 现成去重 |
| **`tests/_bootstrap.py` + `test_github_regression.py`** | 隔离库测试与"原有行为不变"契约的模板 | 回归有保障 |
| **`prototype/`（未跟踪）的成果** | 地名库、质心算法、网格聚类、收敛算法、62 KB 内联底图、`raw/` 真实抓取快照（可作测试 fixture） | **最难的底图与地理编码已被验证过** |
| **`config.py` 的 DEFAULTS + 环境覆盖 + `cached()`（每次重载）** | 新配置段（`geo`/`themes`）落在这里，改配置无需重启 | 现成 |
| **前端 tab 模式 + `api()` + `switchView()` + `--fb-*` 令牌** | 新面板照抄既有面板写法 | 一致性好 |

### 7.2 需要**新增**哪些模块

1. **`core/radar/intel.py`** —— 五个面板的只读聚合层（**本方案的核心**，零 LLM）。
2. **`core/geo/`** —— 地名库、地理编码、质心、投影、结构化地理源适配器、国家基准（**独立包，可单测**）。
3. **`core/radar/classify.py`** —— 主题（闭合表）与地域（`cn/intl/unknown`）分类，替换 `report.py` 的平台名硬编码。
4. **4 张新表** —— `radar_threat_daily`、`radar_country_daily`、`radar_geo_points`、`radar_geo_baseline` + `radar_events`/`radar_items` 的若干新列（**全部增量**）。
5. **4 个新 GET 接口** —— `/api/radar/intel`（主）、`/intel/live`、`/intel/map`、`/intel/countries`。
6. **前端 3 件套** —— `web/radar-intel.js`（面板 + 内联 SVG 地图）、`web/world-110m.js`（内联底图）、`radar.html` 的 1 个 tab / 1 个 section / 1 个 script；`styles.css` 追加一节。
7. **2 个离线生成脚本 + 2 个新测试文件** —— `scripts/build-basemap.py`、`scripts/build-gazetteer.py`、`tests/test_intel.py`、`tests/test_geo.py`。

### 7.3 **第一阶段应该实现什么**

**本阶段（已完成）**：只有本文档，**不改任何业务代码**。

**下一个第一阶段（代码阶段 1）建议这样做 —— 「先做能算准的，后做需要地理的」：**

**做：**
1. `core/radar/intel.py` + `GET /api/radar/intel`（含 4 个子接口）。
2. **Threat Timeline**：以 `radar_event_snapshots` + `metrics.raw_heat` 算日级 TL，物化进 `radar_threat_daily`，**显式返回 `history_days` 与 `confidence`**，不足 7 天就如实提示。
3. **Strategic Risk Overview**：5 个输入全部来自现有列，权重可配、逐项可回溯、`missing_inputs` 明示。
4. **Intelligence Feed**：直接复用 `radar_reports.highlights` + `seen_items` 的 NEW。
5. **Live News**：`radar_items` + 游标分页 + 诚实处理"无发布时间/热词榜无正文"。
6. **Country Instability Index**：**本阶段只交付"不可用"状态**（`available:false` + `degraded` 说明"尚无国家字段"），把公式与表结构定好，**等阶段 2 的地理编码落地后自动生效**。
7. `radar.html` 加 1 个 tab + 5 个面板（**先不做地图**）；`styles.css` 追加一节。
8. `tests/test_intel.py`（含 `test_no_llm_calls`）。

**不做（留到后续阶段）：** 地图与地理编码（阶段 2）、结构化地理源与真正的 CII（阶段 3）、收敛告警/回放/瓦片底图（阶段 4）。**World Monitor 的股票、航班、船舶、卫星、直播、摄像头、预测市场、供应链等面板一律不做。**

**为什么这样切**：阶段 1 **完全不依赖任何外部新数据源、不依赖大模型、不依赖地理编码**，因此可以立刻在现有 3 天数据上跑通并被验收；而地图与 CII 都被"没有地理字段"卡住，硬做只能造假数据——**这与项目"不编造、诚实标注未接入"的既有价值观直接冲突**。把地理维度放到阶段 2 单独解决，是最稳的顺序。

### 7.4 需要你决策的三件事

1. **中国地图边界用哪一套**（Natural Earth 有合规风险；对外展示建议用自然资源部标准地图）——**这是合并前的前置决策**。
2. **是否接受把地图做成零依赖内联 SVG**（无街道细节、断网可用），还是必须上 Leaflet + 瓦片（好看但需联网，且要把 `leaflet.css` 并进 `styles.css`）。
3. **是否申请 UCDP token**（国家风险要真实烈度数据就得有它；不申请则 CII 只能停留在"媒体可观测"口径，并如实标注）。

---

## 附录 A：本次实测的原始证据（可复现）

```bash
# 1) 测试基线
python -m unittest discover -s tests -t tests -v
# → Ran 240 tests in 7.968s
# → OK

# 2) 数据库结构与规模（只读）
python - <<'PY'
import sqlite3
c = sqlite3.connect('data/filter-bubble.db')
for n, in c.execute("select name from sqlite_master where type='table' order by name"):
    print(n, c.execute('select count(*) from "%s"' % n).fetchone()[0])
PY
# → radar_sources 35 / radar_items 3144 / radar_events 886 / radar_event_snapshots 887
# → radar_stories 397 / radar_claims 38 / radar_evidence 181 / radar_summaries 119
# → radar_reports 21 / radar_rank_history 4523 / radar_runs 20

# 3) 历史深度（关键约束）
# radar_event_snapshots: 2026-10-23 → 2026-10-25（仅 3 天；433 / 220 / 234）
# radar_events.date:     2026-10-23 → 2026-10-25（仅 3 天）
# radar_reports(daily):  3 份

# 4) 地理字段（关键缺口）
# radar_events 无 country/lat/lon/severity；radar_items 同样无 → 全库 grep 零业务命中

# 5) 外部源可达性（20+ 端点，详见 §6.2 表）
# USGS 200 · World Bank 200 · Natural Earth 200 · jsDelivr/unpkg 200
# OSM/CARTO/高德瓦片 200 · GDELT doc 429 · GDELT geo 404 · UCDP 401 · ACLED TLS 断 · ReliefWeb 410
```

## 附录 B：全文标注的「待验证」清单（合并前必须逐条落实）

| # | 待验证项 | 为什么 |
|---|---|---|
| 1 | **GDELT Geo API 是否真的下线** | 实测 404 + TLS 超时；不得据此设计功能 |
| 2 | **UCDP 申请 token 后的可用性与数据许可** | 实测 401，需 header `x-ucdp-access-token` |
| 3 | **GDACS / EONET / OpenSky 当前可用性** | 仅 `🔶原型阶段实测`，本次未复验 |
| 4 | **World Bank 各指标的正确 indicator 代码** | 实测 `PV.EST` 返回 400 |
| 5 | **INFORM / FSI / GPI 是否有机器可读接口** | 只确认 HTML 可达，**无确认 API** |
| 6 | **OSM / CARTO / 高德瓦片的使用条款与授权** | 实测可达 ≠ 允许使用；高德免 key 端点非正式第三方授权 |
| 7 | **Leaflet / ECharts / d3 / topojson 的许可证原文** | 本文许可结论来自公开常识，**未逐份拉取原文** |
| 8 | **GDELT / UCDP / ACLED / GDACS / World Bank 的署名与再分发要求** | 落库与展示前必须核实 |
| 9 | **World Monitor 的 CII 权重表** | 来自原型调研；本文未核对源码；AGPL 只可借鉴思路 |
| 10 | **Natural Earth 边界在国内展示的合规性** | 需要你决策（见 §7.4） |
| 11 | **`radar_events.metrics.raw_heat` 在历史行里的完整覆盖** | 实测 886 行中 878 行有 `raw_heat`（8 行缺失，多为 `lifecycle=query`），TL 计算需处理缺失 |
| 12 | **新增物化写入是否引入新的锁竞争** | 虽复用同一事务，仍需在真实负载下观察 |

---

# 附：阶段一（基础后端）落地记录 · as-built

> 实施时间：2026-10-26。本节记录**实际写进代码的东西**，与上面的方案稿如有出入，以本节为准。
> 本次只做基础后端：`core/radar/intel.py`、Threat Timeline 真实历史统计、可解释战略风险与覆盖状态、
> Intelligence Feed、Live News 游标分页、增量迁移与离线测试。
> **未实现**：地名匹配、国家风险排名、交互式地图、结构化地理数据源。

## A1 数据库变化（全部增量、幂等）

新增 1 张表 + 13 个预留列，全部在既有的 `store.init()` / `_ensure_column` 机制里完成，可重复执行：

```sql
-- 新表：每日态势快照（固定口径物化）
CREATE TABLE IF NOT EXISTS radar_threat_daily (
    date TEXT PRIMARY KEY,
    caliber_version INTEGER NOT NULL DEFAULT 1,   -- 1=固定口径；0=由快照反推的旧口径
    activity REAL NOT NULL DEFAULT 0,             -- 0-100 威胁活动
    credibility REAL,                             -- 0-100 情报可信度（可为 NULL=无输入）
    activity_components TEXT NOT NULL DEFAULT '{}',
    credibility_components TEXT NOT NULL DEFAULT '{}',
    event_count INTEGER NOT NULL DEFAULT 0,
    story_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    platform_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    unverified_share REAL, concentration_share REAL, evidence_gap_count INTEGER,
    continuing_event_count INTEGER NOT NULL DEFAULT 0,
    grew_event_count INTEGER NOT NULL DEFAULT 0,
    top_activity_heat_mean REAL,
    inputs_available TEXT NOT NULL DEFAULT '[]',
    missing_inputs TEXT NOT NULL DEFAULT '[]',
    computed_at TEXT NOT NULL
);

-- 预留列（本阶段全部保持 NULL，不做任何自动回填）
ALTER TABLE radar_events ADD COLUMN origin_country_code TEXT;   -- 报道来源国
ALTER TABLE radar_events ADD COLUMN event_country_code  TEXT;   -- 事件发生国（两者不得互相顶替）
ALTER TABLE radar_events ADD COLUMN geo_lat REAL;  ... geo_lon / geo_label /
                       geo_confidence / geo_method / geo_source
ALTER TABLE radar_events ADD COLUMN theme TEXT;                 -- 事件类型（独立于 category）
ALTER TABLE radar_events ADD COLUMN region TEXT;                -- 地域（独立于 category）
ALTER TABLE radar_items  ADD COLUMN origin_country_code TEXT;  ... event_country_code
```

`category` 的语义**未做任何改动**（仍是"命中用户哪个兴趣主题"）；`score.py` / `report.py` 的既有业务语义本次
未改，也没有重算历史日报。

## A2 两个独立指标与三种口径版本

* **威胁活动 `activity`（0–100）** = `0.40×intensity + 0.25×volume + 0.20×breadth + 0.15×growth`
  * `intensity` = 当日 top5 **`metrics.raw_heat`** 均值 / 100 —— **刻意不做 `event.heat` 回退**，
    因为 `heat` 是按当日最强事件重标定过的相对值。
  * `volume` = 当日条目数 / `item_ref`(400)；`breadth` = 当日平台广度 / `platform_ref`(12)
  * `growth` = 较前一日快照真正增长的事件数 / 有前一日快照的事件数
* **情报可信度 `credibility`（0–100）** = `0.45×已验证比例 + 0.25×非集中比例 + 0.30×有媒体报道比例`
  —— **独立于 activity**。未经验证的新闻数量不会进入、也不会抬高威胁活动。
* **口径版本**：`1` = 本次开始记录的固定口径（输入全部为当次真实值）；`0` = 由当时写入的
  `radar_event_snapshots` 反推（只有条目量与平台广度）。两者**不可直接比较**，接口用
  `comparability` = `uniform | mixed | legacy_only | none` 显式声明。
* **缺输入一律如实上报**：`missing_inputs` + 权重在可用分项间重新归一化（`renormalized: true`），
  不插值、不填 0 冒充真实值。当天没有事件时返回 `available:false, activity:null`——
  "没有观测"不等于"活动为 0"。

## A3 战略风险：三个维度分开 + 明确标注的估算

`strategic` 返回三块互不掩盖的结果，再加一个**明确标注为估算**的合成值：

| 块 | 含义 |
|---|---|
| `observed_activity` | 全球观测到的威胁活动（含 `basis`：`fixed_caliber`/`legacy_caliber`、`partial`） |
| `credibility` | 情报可信度（未验证比例、来源集中、缺媒体报道） |
| `monitoring` | **系统监控覆盖与数据质量**（源停用/不可用/失败、国际来源占比） |
| `estimate` | `0.55×observed_activity + 0.25×evidence_uncertainty + 0.20×monitoring_blind_spot` |

所有输入统一归一化到 0–1 后合成（TL 除以 100），权重与分项随响应返回。
`estimate.label` = 「系统自评估算（非官方指数，含监控盲区，不可解释为现实冲突风险）」，
`is_estimate: true`。**源停用、额度不足、国际覆盖偏弱只进 `monitoring`，绝不并入威胁活动**
（`test_monitoring_blind_spot_is_separate_from_observed_activity` 覆盖）。
数据不足以支撑估算时返回 `score: null` 而不是编一个数。

## A4 API 契约（新增 4 个只读 GET，既有 15 个一字未动）

| 端点 | 参数 | 说明 |
|---|---|---|
| `GET /api/radar/intel` | `days` `date` `feed_limit` `feed_offset` `news_limit` `before_id` `since_id` `q` | 主聚合：`window / coverage / threat_timeline / today / strategic / intelligence_feed / live_news / country_index / map / missing_inputs / degraded / semantics / llm` |
| `GET /api/radar/intel/threat` | `days` `date` | 只返回时间轴 + 覆盖状态 |
| `GET /api/radar/intel/live` | `limit` `before_id` `since_id` `q` | 只返回实时新闻流 |
| `GET /api/radar/intel/countries` | `days` | **`available:false`** + 原因 + `requires` + `planned_fields`，`countries: []` |
| `GET /api/radar/intel/map` | `days` | **`available:false`** + 原因，无假点位 |

统一带 `generated_at` / `window` / `coverage` / `missing_inputs`；失败返回
`{error, error_type, path, hint}`（可诊断），且不影响其它接口。

**Live News 游标语义严格分开**（`live_news`）：
* `before_id`：取更小的 id 向历史翻页（`ORDER BY id DESC`）
* `since_id`：取更大的 id 拉新增，**按 id 升序取最老的一批再倒序展示** → 限流截断时不会漏掉中间记录
* 游标以**单调递增的 `radar_items.id`** 为准（比时间戳稳）；`published_at` 为空时回退 `collected_at`，
  并用 `time_source` / `published_at_missing` 标明；热榜条目 `is_trend_only: true`。

## A5 不在事务里等外部请求

* `intel.py` **零网络、零大模型**（单测断言：无 `llm`/`summarize`/`ai_client` 导入，且打桩后全程 0 次调用）。
* 快照物化放在**取报的同一个事务**里（`service.collect_and_report` 与 `generate_report_only`），
  不新增写入路径、不新增线程。
* 未正常结束的运行记录不再永久挂着 `running`：`store.finish_stale_runs()` 在调度器启动时收尾，
  并在 `collect_and_report` 的 `finally` 里加了兜底收尾。

## A6 真实库副本上的实测结果

在 `data/filter-bubble.db` 的**副本**上（原库 mtime/size 未变）：

* `GET /api/radar/intel` → 200；`history_days = 3`，`history_confidence = insufficient`，
  `comparability = legacy_only`；`degraded = [country_index, map, threat_timeline]`
* 时间轴：10-23/24/25 为 caliber 0（`activity` 59.5 / 29.8 / 43.1，`credibility = null`），
  10-20~10-22 与 10-26 为 `available:false`（**不插值**）
* 战略：`observed_activity = 43.1`（`basis=legacy_caliber, partial=true`）、`credibility = null`、
  `monitoring = 69.7`（源 35：ok 22 / paused 4 / unavailable 5）、`estimate = 39.7`（已标注为估算）
* Intelligence Feed：12 条，`story_id` 与日报 `highlights` **逐条一致**
* Live News：3144 条上翻页 **0 重叠、0 遗漏**；`since_id` 只取新增
* 物化 10-25（真实有数据的一天）→ `caliber 1`，`activity = 69.9`，`credibility = 50.6`，
  输入齐全（`intensity 0.6012 / volume 0.7325 / breadth 1.0 / growth 0.5`）
* 既有接口 `/status /report /events /sources /history /settings` 全部 200，旧字段全在
* **地理预留列被自动回填的行数 = 0**

## A7 测试

`python -m unittest discover -s tests -t tests` → **Ran 288 tests / OK**
（原 240 + 新增 `tests/test_intel.py` 48 个）。新增用例覆盖：口径公式精确值、历史缺失与
`caliber_version`、反推点**不使用重标定 heat**、`available:false` 不冒充 0 活动、可信度与活动分离、
战略风险归一化与数值范围、缺失输入重新归一化、情报流 `story_id` 一致性、
`before_id`/`since_id` 不重不漏、旧 API/旧字段兼容、**零大模型调用**、
写入失败能收尾并记录错误、迁移幂等。

## A8 未完成（明确的后续工作）

1. 地名库/地理编码/地图组件（阶段二）——`country_index` 与 `map` 目前**故意返回不可用**。
2. `origin_country_code` 回填：GDELT 的 `sourcecountry` 目前仍只写在 `raw_content` 文本里；
   要落成字段必须先明确它**只是报道来源国**。
3. `theme` / `region` 的填充与 `report.py` 分栏数据驱动（本阶段只预留了列）。
4. 结构化地理数据源（USGS 实测可用；GDACS/EONET 需复验；GDELT 仍 429；UCDP 需 token；ACLED 不可达）。
5. 前端 5 个面板与地图（本阶段只做后端契约）。
6. **未做**：`docs/radar.md` 的接口清单与新端点同步；其中"51 个测试"的过期说法（实际 288）也待修正。
7. **历史序列仍需积累**：固定口径从本次运行才开始物化，`history_confidence` 要满 7 天才转 `ok`。
   **当前不能声称已有完整的 7 天真实指数。**

---

# 附：阶段三（地理能力 · 交互式地图 · 工作台前端）落地记录 · as-built

> 实施时间：2026-10-26。本节记录**实际写进代码的东西**；与上面方案稿不一致处，以本节为准。

## B1 新增模块

```
core/geo/__init__.py          地理模块入口
core/geo/countries.py         国家代码/中英文名/简称别名/质心（读本地 JSON）
core/geo/gazetteer.py         城市与地区地名库（244 条）+ 组织/球队后缀排除
core/geo/geocode.py           优先级解析器（source_coordinates → gazetteer → 国家质心 → None）
core/geo/validate.py          坐标校验、经度回绕、圆均值质心
core/geo/project.py           Web Mercator（与前端同一组公式）
core/geo/sources.py           USGS 适配器 + 图层目录（GDACS/EONET 只登记不接入）
core/geo/data/countries.json  239 个国家（Natural Earth 10m，公共领域）
core/radar/classify.py        九类主题规则分类（可解释、带 evidence）
core/radar/geo_tag.py         地理标注编排（批量、短事务、幂等）
web/radar-intel.js            工作台前端（地图 + 五个面板，零依赖）
web/world-110m.js             离线矢量底图（Natural Earth，131 KB，运行时零网络）
scripts/build-geo-countries.py  国家数据生成器（ISO_A2_EH 兜底 + 主权国优先去重）
scripts/build-basemap.py        底图生成器（土地+国界，单路径复用）
scripts/geo-backfill.py         一次性补全（地理标注 + 可选 USGS）
scripts/eval-geocode.py         人工标注样本评估
tests/test_geo.py               51 个离线测试
tests/fixtures/geo-labelled-sample.tsv  200 条人工标注样本
docs/global-intel-setup.md / -data-sources.md / -testing.md
```

修改：`core/radar/store.py`（`radar_geo_points` 表 + 地理列写入/查询）、
`core/radar/sources/__init__.py`（geo 分支 + `GEO_ADAPTERS`）、
`core/radar/intel.py`（真实地图/国家面板/主题富化）、`core/radar/service.py`（采集后地理标注）、
`core/radar/api.py`（筛选参数）、`web/radar.html`（第 7 个 tab）、`web/radar.js`（一行触发）、
`web/styles.css`（末尾追加 `.intel-*`）。

## B2 数据库变化

```sql
CREATE TABLE IF NOT EXISTS radar_geo_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL, external_id TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL,
    title TEXT, detail TEXT, magnitude REAL, severity TEXT,
    observed_at TEXT, source_id TEXT, url TEXT,
    collected_at TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_radar_geo_points_ext ON radar_geo_points(layer, external_id);
```

地理列**复用阶段一已预留的字段**（没有重复建列）：
`origin_country_code / event_country_code / geo_lat / geo_lon / geo_label /
geo_confidence / geo_method / geo_source / theme / region`。

## B3 与阶段一方案的关键差异（都是被真实数据推翻后改的）

| 方案稿 | 实际实现 | 原因 |
|---|---|---|
| 用 Natural Earth 的 `name_zh` 直接匹配 | 新增**简称/别名表**（`SHORT_ZH`/`EXTRA_ZH`/`EXTRA_EN`） | NE 给的是正式名「中华人民共和国」，标题里写的是「中国」，直接匹配几乎零命中 |
| 用 `ISO_A2` 取国家码 | 改用 **`ISO_A2_EH` 兜底** | 这一版 NE 把法国/挪威/科索沃写成 `-99`、台湾写成 `CN-TW`，直接丢掉了 4 个新闻高频国家 |
| 无同码冲突处理 | 按 NE 的 `TYPE` **主权国优先**去重 | 兜底后「法国 vs 克利珀顿岛」同码，必须留主权国 |
| 英文名顺序匹配 | 改**统一按词长倒序**匹配 | 否则「印度」会抢在「印度尼西亚」前面命中 |
| 地图画"陆地层+国界层" | 单条路径复用两次（fill + stroke） | 两份完全相同的 path 白占 130 KB |
| 事件位置来自 `sourcecountry` | **只用于 `origin_country_code`**，事件国靠地名匹配 | GDELT 的 `sourcecountry` 是报道来源国，不是发生国 |

## B4 修复的真实缺陷（自查发现）

1. **`collect()` 没有把 geo 源分流**：新增了 `_run_geo` 却忘了改 futures 提交，
   USGS 被当成 RSS 去抓 `spec["url"]`（空串）→ `unknown url type: ''`。
   已改为按 `kind=="geo"` 选择 runner，并在结果循环里单独处理。
2. **英文国名大小写 bug**：英文词用原样大小写去匹配小写文本，`Ukraine` 永远匹配不上。
   已改为先 `lower()` 再转义。
3. **`upsert_geo_points` 计数错误**：SQLite 的 `ON CONFLICT DO UPDATE` 也返回 `rowcount=1`，
   把"更新"算成了"新增"。已改为写入前查存在性。
4. **繁体写法缺失**：`美國`/`烏克蘭` 等匹配不到，已补常见繁体别名。

## B5 真实数据结果

| 指标 | 数值 |
|---|---|
| 事件总数 | 886 |
| 完成地理标注 | **241（27.2%）** |
| 其中 `medium`（地名匹配） | 107 |
| 其中 `low`（国家质心近似） | 134 |
| 其中 `high`（来源坐标） | 0（当前语料没有带坐标的新闻源） |
| USGS 点位（库内 / 7 天窗口内） | 192 / 159 |
| 地图实际展示 | 241 个新闻事件 + 159 个地震点 |
| 主题分布（全量） | general_news 600 / technology 186 / politics 29 / disaster 35 / conflict 18 / unrest 11 / cyber 5 / security 1 / infrastructure 1 |

## B6 人工标注评估

200 条真实样本（150 已定位 + 50 未定位），132 条有单一国家依据：

* 标题级：精确率 **87.2%**，召回率 **99.2%**（TP 129 / FP 19 / FN 1 / TN 51）
* 生产级：精确率 **85.0%**，召回率 **100%**
* `medium` 准确率 86.3%（n=73）；`low` 准确率 83.8%（n=80）
* 主题准确率 **72.0%**

主要错误类型：以城市命名的球队（11）、主体国≠发生地（3）、跨国评论被单国锚定（4）。

## B7 测试

`python -m unittest discover -s tests -t tests` → **Ran 339 tests / OK**
（288 基线 + 51 新增）。新增覆盖 14 项验收要求，详见 `docs/global-intel-testing.md`。

## B8 仍未完成

1. **GDACS / EONET 未接入**（只在图层目录里登记为 `connected:false`）。
2. **国家面板仍无基线**：`coverage=media_only`、`partial=true`，不是官方冲突烈度指数；
   `baseline` 恒为 `null`，`missing_inputs=["baseline"]`。
3. **地图未使用瓦片**：无街道细节（换取断网可用）。
4. **中国边界合规**未处理（Natural Earth 不等于合规底图）。
5. **无浏览器端自动化测试**：前端只做了 `node --check` 语法校验 + DOM 契约测试 + 手工验收清单，
   没有 headless 浏览器回归。
6. **地理命中率 ~27%**：语料以国内娱乐/体育热搜为主，且我们刻意不猜。

---

# 附：阶段四 B（灾害图层与国际新闻源扩展）落地记录 · as-built

> 实施时间：2026-10-26。目标**不是**接入尽可能多的来源，而是让灾害与国际新闻
> 真正进入现有系统、正确上图，并且可追踪、可诊断、可持续运行。

## C1 新增/修改

**新增**
```
scripts/collect-now.py                按需采集指定源（复用同一个 sources.collect）
tests/test_geo_layers.py              27 个离线测试
tests/fixtures/eonet_events.json      真实抓取样本（6 事件）
tests/fixtures/gdacs_events.json      真实抓取样本（12 要素 / 6 种灾害）
tests/fixtures/eonet_polygon.json     构造样本（线上只有 Point，见 §C4）
```

**修改**
* `core/geo/sources.py`：重写为三适配器（usgs / eonet / gdacs）+ 灾害类型映射
  + 几何诚实处理 + 图层目录（三者全部 `connected=true`）；
* `core/radar/store.py`：`radar_geo_points` +7 列（hazard / magnitude_unit /
  geometry_type / geometry_json / position_precision / closed_at / updated_at）、
  `geo_hazard_counts()`、幂等 upsert 扩展；
* `core/radar/sources/__init__.py`：注册 `eonet`、`gdacs` 两个 geo 源
  与 `bbc_world`、`guardian_world`、`aljazeera`、`npr_world` 四个 RSS 源；
  `GEO_ADAPTERS` 扩到三个；
* `core/radar/intel.py`：`map_payload` 增加 hazard 维度、几何透传、
  跨来源疑似重复提示（`_attach_duplicate_hints`），`build_payload`/API 增加 `hazards` 参数；
* `core/radar/api.py`：`hazards=` 查询参数；
* `web/radar-intel.js`：灾害**按类型**开关、多边形绘制、灾害点位详情
  （时间/来源/精度/原文/最后更新/疑似重复提示）；
* `web/styles.css`：`.intel-hazard-*` / `.intel-sources` 等样式。

## C2 数据落地（真实库）

| 项 | 数值 |
|---|---|
| `radar_geo_points` 总量 | **282**（usgs 192 / gdacs 86 / eonet 4） |
| 按灾害类型 | 地震 213 · 风暴 21 · 洪水 18 · 野火 16 · 火山 7 · 干旱 6 · 其他 1 |
| 按几何 | Point 281 · 空 1（阶段三迁移前的旧行） |
| 新增英文新闻 | **95 条**（BBC 30 / Guardian 30 / Al Jazeera 25 / NPR 10），`language=en` |
| USGS 幂等 | 本轮 191 条全部 `updated`、`inserted=0` |
| 源健康 | 7 个新源全部 `status=ok`，`last_ok_at` 为真实采集时间 |

## C3 关键设计决定

1. **不建立第二套新闻系统**：4 个 RSS 源走现有 `rss` 适配器 + `make_item` +
   聚类流水线；2 个灾害图层走现有 geo 分支 + `radar_geo_points`。
2. **灾害按类型开关，而不是按源开关**：同一场风暴可能来自 EONET 或 GDACS，
   用户关心的是"风暴"这一层，不是"哪个 API"。
3. **跨来源不合并**：只挂 `related_sources` 提示（≤165 km / ≤72 h、跨来源），
   因为**没有权威关联 ID**，合并会凭空减少灾害数量。
4. **几何诚实**：`Point` 才是精确位置；`Polygon` 存原始坐标、标 `area`、
   详情写明"面范围，非精确事发点"。
5. **`current_only` 默认关闭**：实测 GDACS `iscurrent=true` 只有 2 条，
   改为存全量、由时间窗决定展示，`iscurrent` 留在 `raw` 供审计。
6. **不绕过 GDELT 限流**：仍 429，保持 `paused`，用 4 个已验证 RSS 源补覆盖。

## C4 与预期的差异（都是实测推翻的）

| 原以为 | 实际 | 处理 |
|---|---|---|
| 世界银行 `PV.EST` 可用 | 返回 `indicator was not found` | 正确码是 `GOV_WGI_PV.EST`（见数据源文档 §11） |
| EONET 返回 JSON（Content-Type 正确） | 头写 `application/rss+xml`，体是 JSON | 解析不看 Content-Type |
| EONET 会有 Polygon | 7 天/野火/海冰共 878 个几何**全是 Point** | Polygon 分支用**构造样本**测试并明确标注 |
| GDACS 用 `iscurrent` 过滤最新 | 只剩 2 条 | 存全量 + 时间窗 |
| GDACS `geteventlist/MAP` 可列表 | 400 需要 eventtype | 改用 `SEARCH` |

## C5 测试

`Ran 383 tests / OK`（356 + 27）；真实浏览器 `22/22`。
改写 2 个阶段三用例（新源接入后固定值必然过时），改为断言不变量。

## C6 未完成

1. **国家基线未实施**：已验证 `GOV_WGI_PV.EST` 可用（209/216 国家、2025 年、
   CC BY 4.0 需署名），但它是**年度低频指标**，与当前每日口径不同维度，
   需要单独的年份维度与展示约束，留到下一阶段；国家面板继续 `media_only`。
2. **无真实多边形样本**（线上全 Point）。
3. **在线集成验证未纳入自动化**（要求离线）。
4. GDACS 使用条款、World Bank 署名要求仍需人工核对官方条款页。