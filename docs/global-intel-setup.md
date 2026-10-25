# 全球态势监控 · 启动、配置与故障排查

> 适用版本：阶段三（地理能力 + 交互式地图 + 工作台前端）
> 架构前提不变：**纯 Python + SQLite + 原生 HTML/CSS/JS，无构建步骤、无前端依赖、无 CDN**。

---

## 1. 启动

```bash
# 项目根目录
python app.py                 # 默认 127.0.0.1:8791
```

打开浏览器访问 `http://127.0.0.1:8791/radar.html`，点击顶部标签栏的 **「全球态势」**（第 7 个 tab）。

首次启动时 `store.init()` 会做增量迁移（只加列 / `CREATE TABLE IF NOT EXISTS`），
可重复执行，不会改动既有数据。

### 让地图有数据（首次必做一次）

历史事件的地理标注与结构化图层**不会自动回溯**，需要跑一次补全脚本：

```bash
python scripts/geo-backfill.py --fetch-usgs
```

* 第 1 步：给历史事件补 `geo_* / theme / region`（纯本地计算，不联网，幂等）；
* 第 2 步：抓一次 USGS 地震图层写入 `radar_geo_points`（一次外部 HTTP 请求）。

之后每次采集流水线会自动为**当天新事件**做地理标注，并按源间隔抓 USGS，无需再手工运行。

### 重新生成离线底图（一般不需要）

`web/world-110m.js`（约 131 KB）已随仓库提供。需要更新时：

```bash
python scripts/build-basemap.py            # 从 Natural Earth 拉一次，生成前端底图
python scripts/build-basemap.py --dry-run  # 只看规模，不写文件
```

### 重新生成国家数据（一般不需要）

```bash
python scripts/build-geo-countries.py      # -> core/geo/data/countries.json
```

---

## 2. 配置

全部在 `social_radar.json`，改完**无需重启**（`radar_config.cached()` 每次调用重新加载）。

### 2.1 地理图层开关

```json
{
  "sources": {
    "usgs_quake": { "enabled": true, "feed": "4.5", "period": "week", "max_age_days": 7 }
  }
}
```

* `feed`：`significant` / `4.5` / `2.5` / `1.0` / `all`
* `period`：`hour` / `day` / `week` / `month`
* `max_age_days`：只保留最近 N 天的记录（默认 7）

### 2.2 态势口径参数（`intel` 段）

```json
"intel": {
  "window_days": 7,
  "min_days_for_full_confidence": 7,
  "activity_refs": { "event_ref": 200, "item_ref": 400, "platform_ref": 12, "evidence_gap_ref": 5 },
  "activity_weights": { "intensity": 0.40, "volume": 0.25, "breadth": 0.20, "growth": 0.15 },
  "credibility_weights": { "verified_share": 0.45, "concentration_free": 0.25, "evidence_ok": 0.30 },
  "strategic_weights": { "observed_activity": 0.55, "evidence_uncertainty": 0.25, "monitoring_blind_spot": 0.20 },
  "intl_share_ref": 0.30,
  "live_news_default_limit": 50,
  "live_news_max_limit": 200
}
```

> ⚠️ `activity_refs` 是**本站自己的归一化参考值（分母）**，不是国际标准。
> 改动会让新旧日期的序列不可直接比较，因为口径常量已经变了。

### 2.3 不需要配置的东西

* **地图瓦片**：默认不使用任何瓦片服务器，底图是本地矢量；完全断网也能显示。
* **地理编码服务**：不调用任何在线地理编码 API，全部离线（地名库 + 国家表）。
* **API Key**：USGS / Natural Earth 均免 key。

---

## 3. 故障排查

### 3.1 打开「全球态势」一片空白或报错

| 现象 | 检查 | 处理 |
|---|---|---|
| 顶部提示「加载失败：HTTP 500」 | `GET /api/radar/intel` 返回体里的 `error_type` / `path` | 按错误信息定位；该接口失败不会影响其它 tab |
| 地图只有点位没有陆地轮廓 | 浏览器网络面板里 `/world-110m.js` 是否 404 | 跑 `python scripts/build-basemap.py` |
| 提示「底图未加载（/world-110m.js 缺失）」 | 同上 | 同上 |
| 地图空白且「新闻 0 / 地震 0」 | 是否跑过 `scripts/geo-backfill.py` | 先补地理，再 `--fetch-usgs` |

### 3.2 地图上新闻很少

这是**预期行为**，不是 bug：

* 只有完成地理识别的事件才上图。当语料以国内娱乐/体育热搜为主时，命中率约 **27%**（实测 241/886）；
* 未定位事件**不会**用国家质心硬凑（除非只能确认国家，此时标 `low` 并注明"国家级近似位置"）；
* 情报卡上会显示「未定位」，地图下方注明「未定位事件不显示」。

提升命中率的正当方式：补充 `core/geo/gazetteer.py` 的地名条目（当前 244 条城市/地区），
或增加新闻源的语言多样性。**不要**通过放宽歧义规则来提高数字。

### 3.3 地震图层是空的

1. `GET /api/radar/intel/map` 看 `layers` 里 `usgs_quake.count`；
2. 若为 0，看「数据源管理」里 `usgs_quake` 的 `last_error`；
3. 常见原因：上游超时 / HTTP 429。系统会按既有熔断规则处理（连续 3 次降频、6 次停用），
   可在「数据源管理」点重置。

### 3.4 国家面板显示「不可用」

* 说明窗口内没有完成地理编码的事件 → 先跑 `scripts/geo-backfill.py`；
* 或者事件确实太少（<2 条的国家不给分数，避免读成"零风险"）。

### 3.5 时间线一直显示「历史深度不足」

正常。固定口径是从阶段一才开始逐日物化的，`radar_threat_daily` 需要**满 7 天**才会转成
`history_confidence=ok`。系统**不做插值**，缺失日期显示为 `available:false`。

### 3.6 数据库被锁（`database is locked`）

* 新代码的写入都在短事务里，网络请求都在事务之外；
* 若仍然出现，先用系统状态页确认是否有多个进程同时跑；
* 补全脚本可以分批跑：`python scripts/geo-backfill.py --limit 200`。

### 3.7 端口/访问

沿用原有设置：`GITPULSE_HOST` / `GITPULSE_PORT`（`.env`）。
同 Tailscale 设备访问见 `scripts/open-tailnet.cmd`。

---

## 4. 常用接口速查

```bash
# 主聚合（五个面板 + 地图共用这一份数据）
curl "http://127.0.0.1:8791/api/radar/intel?days=7"
# 只看地图（支持 theme / country / min_heat / layers）
curl "http://127.0.0.1:8791/api/radar/intel/map?theme=disaster&days=7"
# 国家面板
curl "http://127.0.0.1:8791/api/radar/intel/countries?days=7"
# 实时新闻：向历史翻页
curl "http://127.0.0.1:8791/api/radar/intel/live?before_id=16500&limit=30"
# 实时新闻：拉新增
curl "http://127.0.0.1:8791/api/radar/intel/live?since_id=16845"
```

| 参数 | 作用 |
|---|---|
| `days` | 时间窗（1–90，默认 7） |
| `theme` | 主题过滤：`conflict / unrest / politics / security / cyber / disaster / infrastructure / technology / general_news` |
| `country` | ISO2 国家码过滤 |
| `min_heat` | 最低热度 |
| `layers` | 逗号分隔的图层 key（如 `usgs_quake`） |
| `before_id` / `since_id` | **两种游标不可混用**：前者翻历史，后者拉新增 |

---

## 5. 已知限制

1. **地理覆盖约 27%**：中文体育/娱乐语料天然缺少地名，且我们刻意不猜。
2. **体育队名会误判**：`广东队`、`北京男篮` 这类以城市命名的球队仍有少量漏网。
3. **没有国家基线**：国家面板只有"媒体可观测紧张度"，不是官方冲突烈度指数。
4. **历史深度不足 7 天**前，时间线只作参考。
5. **底图无街道细节**：这是零依赖的代价，换来了断网可用。

---

## 6. 阶段四新增：真实浏览器测试与精度筛选

### 6.1 跑真实浏览器测试

```bash
node scripts/browser-test.mjs               # headless，22 项检查
node scripts/browser-test.mjs --headful     # 需要肉眼观察
node scripts/browser-test.mjs --port 8899 --cdp-port 9333
```

* 需要本机装有 **Edge 或 Chrome**（默认找 Edge）；可用 `FB_EDGE` 指定可执行文件路径。
* 需要 Node ≥ 18（用到内置 `fetch` 与 `WebSocket`），**不安装任何 npm 依赖**。
* 测试会自己**复制一份真实库**到临时目录再启动服务，跑完删除；**不会写生产库**，
  也不会启动雷达/论文的联网调度。
* 如果找不到浏览器，脚本会明确报错退出（**不会**用 `node --check` 冒充浏览器测试）。

### 6.2 位置精度筛选与导出

「全球态势」工具条新增：

* **位置精度**：`全部` / `仅中高置信` / `仅国家级近似`
  * 高置信 = 来源坐标（实心圆 + 外环）
  * 中置信 = 地名匹配（实心圆）
  * 低置信 = 国家质心（**菱形 + 虚线**，详情标「国家级近似位置」）
* **导出当前视图**：下载 CSV，含 `geo_confidence` / `geo_method` / `geo_label`，
  并在文件头写明"位置精度说明"与底图来源。

### 6.3 地理规则变更后如何重算存量数据

```bash
python scripts/geo-backfill.py --force     # 按当前规则重算全部事件（含已标注的）
python scripts/eval-geocode.py             # 调参集指标
python scripts/eval-geocode.py --validation # 独立验证集指标
```

不加 `--force` 时只处理"未标注 / 标注为 none"的事件。

---

## 7. 阶段四 B：新增灾害图层与国际新闻源

### 7.1 重要：改完代码必须重启应用

正在运行的服务（默认 `http://127.0.0.1:8791`）**不会热加载 Python 代码**。
新增的 `eonet` / `gdacs` / 4 个国际 RSS 源必须**重启 `python app.py`** 之后才会被调度。
（前端文件是静态的，刷新页面即可生效。）

### 7.2 按需采集新源（不跑全量）

```bash
python scripts/collect-now.py --list                      # 看所有 source_id
python scripts/collect-now.py --only eonet,gdacs          # 只跑两个灾害图层
python scripts/collect-now.py --only bbc_world,guardian_world,aljazeera,npr_world
python scripts/collect-now.py --only eonet --force         # 忽略熔断/节流
```

它走的是**同一个** `sources.collect()`，所以源健康、限流、退避、熔断、
`radar_geo_points` 幂等写入与常规调度完全一致。

### 7.3 灾害图层开关（前端）

「全球态势」地图下方的图例按**灾害类型**分组，可分别开关：

* 地震（USGS）
* 火山 / 野火 / 洪水 / 风暴 / 干旱（NASA EONET、GDACS）
* 其他

* 只有**已接入且计数 > 0** 的类型可勾选，其余显示「无数据」且禁用；
* 图例下方列出各数据集的接入状态与许可证；
* 点击灾害点位会打开详情：标题、事件时间、数据来源、位置精度、
  数量级/等级、最后更新时间、原文链接；
* **面几何（Polygon）按多边形绘制**，详情里明确写"面范围，非精确事发点"。

### 7.4 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 重启后「全球态势」没有灾害点位 | 还没采集过 | `python scripts/collect-now.py --only usgs_quake,eonet,gdacs` |
| EONET 显示成功但 0 条 | 7 天窗口内没有灾害类事件（冰山等已按类别过滤） | 正常；换 `days` 或等下一轮 |
| GDACS 条数远大于地图显示数 | 存的是全量列表，地图只显示时间窗内的 | 正常 |
| 源显示 `error` 且 `429` | 上游限流 | 等下一轮；熔断会自动降频/停用 |
| `radar_geo_points` 里 `geometry_type` 为空 | 阶段三迁移前写入的旧行 | 无害；前端按"点"处理 |

### 7.5 数据库变化

`radar_geo_points` 新增 7 列（全部 `_ensure_column` 增量迁移）：
`hazard`、`magnitude_unit`、`geometry_type`、`geometry_json`、
`position_precision`、`closed_at`、`updated_at`。
