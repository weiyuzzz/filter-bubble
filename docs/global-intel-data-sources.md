# 全球态势监控 · 地理来源、归属与许可

> 本文记录**实际接入**的数据来源、来源归属、许可证与当前状态。
> 所有"可用性"结论都来自本机真实请求，不是照抄文档。核验日期：**2026-10-26**。

---

## 1. 数据来源总表

| 来源 | 用途 | 接入方式 | 需要 Key | 许可证 / 归属 | 当前状态 |
|---|---|---|---|---|---|
| **Natural Earth 10m admin_0 countries** | 国家代码、中英文名、国家质心、底图多边形 | `scripts/build-geo-countries.py`、`scripts/build-basemap.py` 一次性下载 → 落成本地文件 | 否 | **公共领域（Public domain）** | ✅ 已接入 |
| **USGS Earthquake Hazards Program** | 地震点位图层 | `core/geo/sources.py`（GeoJSON） | 否 | **美国联邦政府作品 / 公共领域**；数据由 USGS 提供 | ✅ 已接入 |
| 项目自带地名库 | 城市/地区 → 坐标 | `core/geo/gazetteer.py`（手工整理） | 否 | 项目内自定义数据集；**坐标事实本身不受版权保护** | ✅ 已接入 |
| 现有 RSS / newsnow / AnySearch 新闻源 | 新闻条目（地理识别的输入） | 既有 `core/radar/sources/` | 部分（AnySearch） | 见各源自身条款（`docs/radar.md`） | ✅ 沿用 |
| GDACS | 灾害警报图层 | 未接入，仅登记目录 | 否 | EU JRC，需署名，条款待核 | ⛔ 未接入（`connected:false`） |
| NASA EONET | 自然事件图层 | 未接入，仅登记目录 | 否 | NASA，公共领域 | ⛔ 未接入（`connected:false`） |
| UCDP GED | 冲突事件 | 未接入 | **需免费 token** | 需署名，条款待核 | ⛔ 未接入 |
| ACLED | 冲突事件 | 未接入 | **需注册审批** | 有再分发限制 | ⛔ 未接入（本机网络亦不可达） |
| GDELT | 新闻发现（已有适配器） | `core/radar/sources/intl.py` | 否 | 有限额与使用条款 | ⚠️ 已接入但长期 429，被熔断暂停 |
| 在线地理编码服务（Nominatim 等） | — | **刻意不使用** | — | — | ⛔ 设计上排除 |

> **我们在 UI 上如何体现"未接入"**：图层面板显示计数 `0` 与「未接入」，
> 点击不产生任何假点位；`GET /api/radar/intel/map` 的 `layers[]` 里 `connected:false`。

---

## 2. 底图与地图资源

| 资源 | 文件 | 体积 | 来源 | 许可 |
|---|---|---|---|---|
| 世界陆地 + 国界矢量 | `web/world-110m.js` | 约 131 KB | Natural Earth 110m admin_0 countries，Web Mercator 投影后转成 SVG path | 公共领域 |
| 国家参考数据 | `core/geo/data/countries.json` | 约 48 KB | 同上 | 公共领域 |

**运行时零网络**：地图不使用任何瓦片服务器、CDN 或第三方脚本，
断网时仍可显示底图与数据库中已存的事件点位。

### 中国地图边界合规提示（重要）

Natural Earth 的国界表述**不保证符合中国境内公开发布地图的合规要求**。
若该页面需要**面向公众在中国境内展示**，应改用**自然资源部标准地图**
（`bzdt.ch.mnr.gov.cn`，免费下载、带审图号）替换 `web/world-110m.js` 的边界数据。
**本项目当前使用 Natural Earth，仅适用于个人/内部使用，不能声称已满足所有展示合规要求。**

---

## 3. USGS 图层的真实可用性（实测）

```
端点  https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson
结果  HTTP 200
解析  192 条（写入 radar_geo_points，幂等 upsert）
时间  2026-10-26T03:06:14+08:00
地图  其中 159 条落在"最近 7 天"的展示窗口内（其余早于窗口起点，只存不展示）
```

说明：

* 只保留 `max_age_days`（默认 7）内的记录；
* 无效经纬度（越界 / `(0,0)` 空岛 / 非数值）在解析阶段直接丢弃；
* 损坏 JSON、超时、HTTP 429、上游异常 → 抛错交给既有源健康机制（记录失败、降频、熔断），
  **不会中断新闻采集或日报**；
* 幂等键是 `(layer, external_id)`，外部 ID 取自 USGS 的 `ids`/`id`，重复采集只更新不新增。

---

## 4. 地理识别依据与置信度

| 等级 | 含义 | 依据 |
|---|---|---|
| `high` | 来源给出并经校验的事件坐标 | 目前只有"来源明确给坐标"这条路径（如未来接入带坐标的源）；**当前语料命中 0 条** |
| `medium` | 地名库匹配到城市/地区，或来源声明国家 | 标题/成员标题命中 `core/geo/gazetteer.py` |
| `low` | 只能确认国家 | 使用国家质心作为**近似锚点**，详情标注「国家级近似位置」 |
| `none` | 无法可靠定位 | **不产生任何标记** |

### 明确不做的事

1. **不因"新闻提到某国"就认定事件发生在该国**。标题出现 ≥2 个国家/地区时判为歧义，
   返回未知（例：「美国警告中国某项政策可能影响日本」→ 不锚定到任何一处）。
2. **不因多平台转载提高置信度**：转载不等于独立的位置证据。
3. **不为跨国事件伪造地理质心**。
4. **区分两个国家字段**：
   * `event_country_code`（事件发生国）—— 由地名匹配得出；
   * `origin_country_code`（报道来源国）—— 由来源/平台所属国得出（如 BBC→GB、IT之家→CN）；
   * 二者**绝不互相顶替**；来源出现多国或未知时 `origin_country_code` 留空。

### 已知误判（如实记录）

* 体育/娱乐标题里的**以城市命名的球队**（`北京男篮`、`辽宁铁人`、`大连英博`）仍可能被当成地点；
  已加组织/球队后缀排除（`队`/`俱乐部`/`后卫`/`锋线`/`联赛` 等），但覆盖不全。
* 事件"主体国"与"发生地"不同的报道（中国女孩在泰国遇险）可能锚到主体国。
* 繁体地名已补常见别名，但仍非全覆盖。

---

## 5. 许可与再分发注意

* **Natural Earth**：公共领域，无署名义务（我们仍在文件头与 `world-110m.js` 里保留来源声明）。
* **USGS**：美国联邦政府作品，公共领域；引用时注明 USGS 更稳妥。
* **项目地名库**：坐标是事实性数据，不受版权保护；该文件由本项目维护，随项目许可。
* **GDACS / EONET / UCDP / ACLED**：若要接入，**必须先核实各自的署名、再分发与商用条款**；
  这些来源当前**未接入**，因此本阶段不产生任何相关义务。
* **不复制 World Monitor 的源码或资产**（其许可为 AGPL-3.0-only 且有独立商标政策）。
  本项目的投影、聚类、评分与图层结构均为自行实现。

---

## 6. 待办

1. GDACS / EONET 适配器（接口已预留，`fetch_points` 目前对未接入图层直接抛错）。
2. UCDP token 申请与冲突数据接入（用于替换"媒体可观测"口径）。
3. 中国标准地图边界替换（合规前置）。
4. 地名库扩充（当前 244 条；国家词条 545 条来自 Natural Earth + 简称/别名）。

---

## 7. 底图来源、许可证与"合规不等于许可证"（阶段四补充）

### 7.1 实际使用情况

| 项 | 值 |
|---|---|
| 数据 | Natural Earth 110m admin_0 countries（陆地 + 国界多边形） |
| 生成脚本 | `scripts/build-basemap.py`（一次性，Web Mercator → SVG path） |
| 产物 | `web/world-110m.js`（约 133 KB，路径字符 133 416） |
| 运行时 | **零网络**：不请求瓦片、不请求 CDN、不请求任何第三方脚本 |
| 许可证 | Natural Earth 声明为**公共领域（public domain）** |
| 来源声明位置 | 生成文件头注释、图例下方常驻文案、导出 CSV 文件头 |

`web/world-110m.js` 文件头保留了：来源 URL、许可证、生成命令、投影参数。

### 7.2 必须说清楚的一件事：公共领域 ≠ 展示合规

Natural Earth 的数据是公共领域，**但它的边界表达政策包含实际控制线（de facto）
与争议边界的处理方式**，并且明确不构成对任何司法辖区的法律意见。
因此：

* **公共领域只解决"版权/署名"问题，不解决"某地能不能这样展示"的问题**；
* 本项目**不声称**已满足中国境内公开展示地图的要求；
* 当前底图仅建议用于**个人/内部**用途。

### 7.3 需要人工核查的事项（未完成，需业务方决策）

1. 若面向公众在中国境内展示，应改用**自然资源部标准地图**（带审图号）替换边界数据；
2. 若改用标准地图，需同步确认其**坐标系/投影**与本项目 Web Mercator 的差异，
   并重新生成 `web/world-110m.js`（脚本支持 `--url` 换源，但**几何格式需适配**）；
3. 核实 Natural Earth 官方边界政策页的最新表述（本阶段**未逐条核对原文**）；
4. 若未来启用瓦片底图，必须另行审核各瓦片服务的使用条款 —— 本阶段**刻意未启用**。

### 7.4 本阶段未做的事

* 未切换任何未授权瓦片服务；
* 未删除或替换现有离线底图；
* 未声称已满足任何司法辖区的展示合规要求。

---

# 阶段四 B：新增数据源实测记录（2026-10-26）

## 8. 已接入的结构化灾害图层

| 图层 | API | 认证 | 实测 | 入库 |
|---|---|---|---|---|
| `usgs_quake` | `earthquake.usgs.gov/.../4.5_week.geojson` | 无 | HTTP 200 | 192 条（本轮 191 条全部 `updated`、0 `inserted`） |
| `eonet` | `eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7&limit=200` | 无 | HTTP 200 | 4 条（原始 8 条，按灾害类别过滤掉冰山等） |
| `gdacs` | `www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH` | 无 | HTTP 200 | 86 条（84 新增 / 2 更新） |

### 8.1 NASA EONET v3（第一优先级，已完成）

* **端点**：`https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7&limit=200`
* **一个重要坑**：响应头写的是 `application/rss+xml`，**但响应体是合法 JSON**。
  如果按 Content-Type 判断就会误判为失败 —— 适配器直接按 JSON 解析。
* **结构**（实测）：
  ```json
  {"title":"EONET Events","events":[{"id":"EONET_25590","title":"...","description":"...",
    "closed":null,"categories":[{"id":"wildfires","title":"Wildfires"}],
    "sources":[{"id":"IRWIN","url":"https://irwin.doi.gov/..."}],
    "geometry":[{"magnitudeValue":2060.0,"magnitudeUnit":"acres",
                 "date":"2026-10-22T21:45:00Z","type":"Point","coordinates":[-82.8577,30.4952]}]}]}
  ```
* `geometry` 是**时间序列**，取最新一条；类型为 `Point` 或 `Polygon`。
* **几何诚实**：`Point` → `position_precision="point"`；
  `Polygon` → 保存原始坐标 + `position_precision="area"`，前端按面绘制，
  中心点只用于视野定位，**不冒充精确事发点**。
* **实测 Polygon 情况**：截至 2026-10-26，实时接口返回的几何**全是 Point**
  （7 天窗口 308/308、野火 34/34、海冰 536/536）。因此 Polygon 分支**没有真实抓取样本**，
  测试用的是按官方 v3 结构**构造**的 `tests/fixtures/eonet_polygon.json`，
  文件内已标注 `CONSTRUCTED FIXTURE`，**不声称存在线上面数据**。
* **类别过滤**：默认只要 `wildfire/volcano/flood/storm/earthquake/drought`；
  冰山（`seaLakeIce`）等归 `other`，不当地图灾害层。

### 8.2 GDACS（第二优先级，已完成）

* **端点**：`https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH`
  （`geteventlist/MAP` 需必填 `eventtype`，返回 400；Swagger 见
  `https://www.gdacs.org/gdacsapi/swagger/index.html`，其 `swagger/v1/swagger.json` 可下载）
* **响应**：`FeatureCollection`，实测 86 个要素；`eventtype` 分布
  `TC 18 / EQ 22 / FL 18 / WF 15 / VO 7 / DR 6`；`alertlevel` 分布 `Red 19 / Orange 67`。
* **关键字段**：`eventtype, eventid, episodeid, eventname, description, alertlevel,
  alertscore, fromdate, todate, datemodified, country, iso3, severitydata, url, iscurrent`
* **唯一 ID**：`<eventtype>-<eventid>-<episodeid>`（如 `EQ-1573141-1741837`），幂等 upsert。
* **为什么默认 `current_only=False`**：实测 `iscurrent=true` 只有 **2 条**，
  地图覆盖过低；改为存全量列表、由**时间窗**决定展示（旧事件自然落在窗口外）。
  `iscurrent` 仍保留在 `raw` 里供审计。
* **不并入新闻聚类**：GDACS/EONET 只写 `radar_geo_points`，不写 `radar_items`。

### 8.3 同一次灾害被多个来源报道时

**不自动合并**。地图负载会给位置/时间接近（≤165 km、≤72 h）且**来源不同**的点
挂 `related_sources`，前端在详情里显示"疑似同一事件（未自动合并）"。
没有经过验证的唯一关联关系，擅自合并会凭空减少灾害数量。

## 9. 新增的国际新闻源（第三优先级）

| 来源 | RSS | 实测 | 本轮入库 | 语言/许可 |
|---|---|---|---|---|
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | HTTP 200，31 条 | 30 | en / BBC 条款，需署名 |
| The Guardian World | `https://www.theguardian.com/world/rss` | HTTP 200，45 条 | 30 | en / Guardian 条款 |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | HTTP 200，25 条 | 25 | en / 半岛条款 |
| NPR World | `https://feeds.npr.org/1004/rss.xml` | HTTP 200，10 条 | 10 | en / NPR 条款 |

* **共 95 条英文国际新闻**进入 `radar_items`（`language='en'`），
  走的是**现有** `rss` 适配器 + `make_item` + 聚类/去重流水线，没有第二套事件系统；
* 发布者所在国**不**被当成事件发生国：来源国只用于 `origin_country_code`，
  事件国仍由标题/条目做地理匹配；
* 实测失败/未采用的候选：AP（Cloudflare 403）、ReliefWeb v2（403 需注册 appname）、
  UN News RSS（XML 解析失败），**均未接入**。

## 10. GDELT 现状（仍受限，未绕过）

* 实测 `api.gdeltproject.org/api/v2/doc/doc?...` → **HTTP 429**，响应体明确写着
  "Please limit requests to one every 5 seconds"；
* `radar_sources` 中 `gdelt` 状态 `paused`、`consecutive_failures=6`，
  最后成功 `2026-10-24T00:18:22`；
* 本阶段**没有**做重试加速、并发、代理轮换或强制重置；
* 覆盖缺口由上面 4 个已验证的英文 RSS 源补上。

## 11. 国家基线（§6 调查结论：已验证但**未实施**）

### 11.1 关键发现：原先设想的指标码是错的

* `VA.EST` / `PV.EST` / `CC.EST` / `GE.EST`（不带前缀）在新版 World Bank API 上返回：
  `{"message":[{"id":"175","value":"The indicator was not found. It may have been deleted or archived."}]}`
* 正确指标码在 **`GOV_WGI_*`** 命名空间下（来源 3 = Worldwide Governance Indicators，
  共 36 个指标），例如 `GOV_WGI_PV.EST`（Political Stability）、`GOV_WGI_VA.EST`。

### 11.2 实测数据质量（`GOV_WGI_PV.EST`）

| 项 | 实测值 |
|---|---|
| 请求 | `api.worldbank.org/v2/country/all/indicator/GOV_WGI_PV.EST?format=json&per_page=400&mrnev=1` |
| HTTP | 200 |
| 国家总数 / 有值 | **216 / 209**（覆盖率 96.8%，缺失约 3.2%） |
| 最新年份 | **2025**（部分国家为 2017 等较早年份） |
| 量纲 | `approx. -2.5 to +2.5`，**数值越高治理/稳定越好** |
| 请求稳定性 | 单指标多次请求中出现过 **超时**（PV/CC/GE 各有 1 次），需重试与超时保护 |
| 许可 | World Bank Open Data，通常为 **CC BY 4.0（需署名）**；**官方条款页需人工核对**后引用 |

### 11.3 结论：具备实施条件，但本阶段未接入

**可以做**：把 `GOV_WGI_PV.EST` 作为**长期治理背景**（低频更新、按年份），
归一化为 `(2.5 - value) / 5` 得到 0–1 的"脆弱度"参考，
与"实时可观测紧张度"**分两个维度并列展示**。

**为什么这次没做**：

1. 阶段重点是"灾害与国际新闻真正进入系统并上图"，基线属于次要项；
2. WGI 是**年度低频指标**，硬塞进当前每日口径的 `radar_country_daily` 会误导
   （会让读者以为国家风险每天都随治理指标变化）；
3. 需要额外引入年份维度、署名信息与"长期指标 ≠ 实时风险"的展示约束，
   属于独立的一次改造。

**明确不做**：抓取 HTML 页面、硬编码分数、把 GDP/媒体量/搜索热度当不稳定性。
国家面板继续保留 `coverage="media_only"`、`partial=true`、`missing_inputs=["baseline"]`。
