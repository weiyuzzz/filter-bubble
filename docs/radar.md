# 全球热点雷达（Social & News Radar）

这是 Filter Bubble 的**第二个独立模块**。原有 GitHub 每日热榜保持原样运行，两者互不影响。

- 入口：<http://127.0.0.1:8791/radar.html>
- 模块切换：页面顶部「GitHub 热点 / 全球热点雷达」可随时来回切换
- 独立数据：新增 `radar_*` 表，写入同一个 `data/filter-bubble.db`；不修改、不清空任何旧表
- 独立调度：`radar-scheduler` 线程，**每小时采集**、**每天 08:00 出日报并推送**，与 GitHub 模块互不影响
- 独立推送：独立 `radar_push_log` 防重，默认**关闭**

---

## 1. 数据源（实测结果）

测试时间：2026-10-22 23:57–23:59（Asia/Shanghai），本机直连。
直连与聚合源都不需要付费；AnySearch 是可选源，实测**不配 key 也能走匿名通道**（额度更低）。

| 数据源 | 类型 | 结果 | 说明 |
|---|---|---|---|
| 中国新闻网·滚动/国内/国际 | RSS | ✅ 可用 | 30 条/频道，约 200–300ms |
| 新浪新闻·国内 | RSS | ✅ 可用 | 15 条 |
| IT之家 | RSS | ✅ 可用 | 40 条 |
| 量子位 | RSS | ✅ 可用 | 10 条 |
| 微博热搜 | 公开 JSON | ✅ 可用 | 需带 `Referer: https://weibo.com/`，40 条 |
| 百度热搜 | 页面内嵌 JSON | ✅ 可用 | 30 条 |
| B 站热门 | 公开 API | ✅ 可用 | 30 条，含播放/点赞 |
| 今日头条热榜 | 公开 API | ✅ 可用 | 30 条 |
| 网易热榜 | 公开 API | ✅ 可用 | 30 条 |
| V2EX 热门 | 公开 API | ✅ 可用 | 较慢（约 2s） |
| 德国之声·中文 | RSS | ✅ 可用 | 40 条 |
| Hacker News 首页 | 公开 API（Algolia） | ✅ 可用 | 25 条 |
| TechCrunch | RSS | ✅ 可用 | 20 条 |
| BBC 中文 | RSS | ⚠️ 间歇可用 | 本机有一次被重置，配置 `HTTPS_PROXY` 后可稳定 |
| GDELT 全球新闻 | 公开 API | ⚠️ 受限 | 公共额度返回 429，适配器会记录失败并继续 |
| 抖音热点 | 聚合 API（newsnow） | ✅ 可用 | 30 条，直连需登录态与签名，改走聚合 |
| 知乎热榜 | 聚合 API（newsnow） | ✅ 可用 | 20 条，匿名直连返回 401 |
| 百度贴吧 | 聚合 API（newsnow） | ✅ 可用 | 30 条，直连需登录态 |
| 华尔街见闻热榜 | 聚合 API（newsnow） | ✅ 可用 | 10 条 |
| 财联社热门 | 聚合 API（newsnow） | ✅ 可用 | 13 条 |
| 澎湃新闻 | 聚合 API（newsnow） | ✅ 可用 | 20 条 |
| 凤凰网 | 聚合 API（newsnow） | ✅ 可用 | 12 条 |
| AnySearch·AI 与大模型 | 统一搜索引擎（可选） | ✅ 可用 | 2026-10-25 实测 10 条 / 2.8s，需 `ANYSEARCH_API_KEY`（或匿名） |
| AnySearch·芯片与半导体 | 统一搜索引擎（可选） | ✅ 可用 | 2026-10-25 实测一次采集 2 条查询 → 20 条入库 / 0 失败 |
| arXiv | Atom | ❌ 默认关闭 | 本机 TLS 证书被中间人拦截（CERTIFICATE_VERIFY_FAILED） |
| Reddit | JSON | ❌ 默认关闭 | 本机 403 Blocked |
| Bluesky | API | ❌ 默认关闭 | 本机 403 / 超时 |
| X / 小红书 / Instagram / Threads / TikTok | — | ⛔ 未接入 | 无合规免费公开入口，**不绕过登录/验证码/访问控制** |

一次完整采集（24 个启用源）实测：**23 成功 / 1 失败（GDELT 429），582 条材料，约 55 秒**。

### 2.1 三种接入方式

```
直连适配器 ──┐
聚合 API ────┼─► radar_items ─► 聚类 ─► 故事 ─► 日报
搜索引擎 ────┘
```

* **直连适配器**：自己解析站点页面 / RSS / 官方 API。数据自持、有正文、可按需加字段，代价是每个平台都要单独写和维护。
* **聚合 API（newsnow）**：第三方把各平台公开热榜聚合成一个 JSON 接口。一个适配器覆盖抖音、知乎、贴吧、华尔街见闻、财联社、澎湃、凤凰；代价是依赖第三方可用性，且只返回标题与链接。
* **搜索引擎（AnySearch，可选）**：按查询召回，覆盖 RSS 和热榜都覆盖不到的长尾。见 2.2。

聚合源的约束：

| 约束 | 做法 |
|---|---|
| 数据可信度 | 每条链接校验 `expected_domain`，https + 域名匹配才入库，否则丢弃 |
| 可用性 | 与其他源一样计入熔断，连续失败 3 次降频、6 次停用 |
| 深度 | 没有正文，只作为「热点发现」，不参与事件调查的正文摘录 |
| 可关闭 | `sources: {"douyin": {"enabled": false}}`，或改 `newsnow.api_url` 指向自建实例 |

### 2.2 搜索引擎源（AnySearch）

实测的接口事实：

| 项 | 实测值 |
|---|---|
| 搜索接口 | `POST https://api.anysearch.com/v1/search`，`{query, max_results, tag, zone, language}` |
| 单次条数 | **上限 10 条**（多给无效） |
| 限流 | 响应头 `X-Ratelimit-Limit: 20`，超额 429；额度用尽 402 |
| 鉴权 | `Authorization: Bearer <key>`；**不配 key 也能用**（匿名通道，额度更低） |
| 返回字段 | `title` / `url` / `snippet` / `content`（清洗后的片段，不是全文） |
| 抓正文 | `POST /v1/extract`，请求体**只能有 `url` 一个字段**（多传字段 → 400） |
| 抓正文实测 | `go.dev` 36,395 字；财联社 / EET-China → **422 抓不到**（付费墙 / 反爬） |
| 子能力 | 17 个域：`general`、`academic`、`code`、`finance`、`security`、`travel`、`health`、`legal`、`gaming`、`film`、`business`、`ip`、`energy`、`environment`、`agriculture`、`resource`、`social_media` |

**垂直域要注意必填参数**：只给 `tag` 不给必填参数会被后端判 400。带必填参数的子域实测：

| 子域 | 必填参数 |
|---|---|
| `finance.quote` / `.news` / `.calendar` / `.macro` / `.screen` | `type`（如 `stock`） |
| `finance.fundamental` | `type`、`symbol`、`cn_code` |
| `security.intel` / `.scan` | `ioc`；`.noise` 要 `ip`；`.vuln` 要 `type` + `value` |
| `travel.flight` | `departure`、`arrival`、`date` |
| `travel.flight_status` | `departure`、`flight_number` |
| `health.drug` | `type` |
| `code.doc` | `library` |
| `gaming.esports` | `type` |
| `academic.citation` | `id` |
| `academic.search`、`business.*`、`legal.*`、`ip.global` 等 | 全部可选 |

参数表用 `GET https://api.anysearch.com/v1/sub-domains?domain=finance` 查（**不占额度**）。配置写法：

```json
{ "id": "stock", "name": "AAPL 行情", "query": "AAPL",
  "tag": "finance.quote", "params": { "type": "stock", "symbol": "AAPL" } }
```

实测返回的是垂直索引结果，和通用网页搜索明显不同：`finance.quote` 给的是行情页（Yahoo Finance / CNN Markets），`academic.search` 给的是机构仓储论文（HKUST、Lingnan）。

两种用法：

| 用法 | 配置 | 说明 |
|---|---|---|
| 发现源 | `anysearch.queries[]` | 每条查询 = 一个源，和 RSS 源平级；实测 2 条查询 → 20 条入库 |
| 调查取证 | `investigation.external_search` | 本地材料少于 `min_local_evidence` 时补外部材料，`external_extract_top` 控制抓几条正文 |

额度保护：

* 发现源带 `min_interval_minutes`（默认 240），距上次成功不足这个时间就整轮跳过，实测跳过时不发请求；
* 抓正文失败不重试（`retries=0`），422 直接退回搜索摘要；
* 错误信息**只保留 HTTP 状态码**：402 的响应体里可能带自动注册生成的账号密码，不能进日志。

**搜索引擎材料不计入"独立来源"**：结果标记为 `source_type=search`，`_source_role` 对这类条目返回空的 `independent_group`，因此它不会把一条说法顶到 `multi_support`（"3 个独立来源内容一致"），也不会消掉"独立来源不足"的警告——同一篇通稿的几家转载站很容易凑出 3 个域名。报告里会单独写"另有 N 条搜索引擎材料（M 个域名）未计入独立来源"。

### 2.3 URL 归一化与去重

入库前去重键不是原始 URL，而是**归一化之后**的 URL：

| 处理 | 例子 |
|---|---|
| 剥掉排名位参数 | 微博 `?q=x&band_rank=6&Refer=top` → `?q=x` |
| 剥掉追踪参数 | `utm_*`、`spm`、`from`、`gclid` |
| 剥掉每次抓取都变的参数 | 今日头条 `log_pb={…impr_id:"2026…"}`、`style_id` |
| 保留内容标识 | `id`、`q`、`wd`、`keyword`、`topic_id` 等 |
| 去掉 fragment、排序参数 | `#section`、参数按字母序 |

不这么做的话，同一条新闻每轮都会因为链接里的动态参数被算成新条目——实测今日头条一条热搜一天内会重复入库 12 次。

> 失败是正常状态，不是 bug。每个源都有独立超时、独立错误记录，任何一个失败都不会影响其他源，也不会让日报整体失败。

---

## 2. 快速使用

```bash
# 界面（含雷达）
./run.sh                      # Windows: 双击 start.bat

# 命令行
python app.py radar collect              # 采集 + 聚类 + 生成日报
python app.py radar report               # 只重建日报
python app.py radar investigate <event_id>
python app.py radar query 芯片 突破
```

页面上六个子页面：

| 子页面 | 内容 |
|---|---|
| 每日热点 | 今日重点热点、中文网络热点、国际热点、科技与 AI、调查更新、数据源覆盖说明 |
| 事件调查 | 输入事件/人物/公司/链接，或从任意事件点「深入调查」 |
| 科技突破核验 | 触发科技专题的事件与 11 段式核验报告 |
| 数据源管理 | 每个源的启用状态、最近成功/失败、延迟、条数、失败原因 |
| 历史记录 | 历史日报、运行记录 |
| 监控设置 | 执行时间、主题/关键词/排除词、评分权重、推送开关、数据源开关 |

---

## 3. 评分模型

热度是 0–100 的**相对分**（当日最强事件映射到约 92），不是平台官方指标。

```
heat = 30% 热度增速 + 20% 跨平台传播 + 20% 互动强度 + 20% 新闻/官方佐证 + 10% 兴趣匹配
```

- 互动强度**按平台分别归一化**，不跨平台比较原始点赞数
- 增速对比的是同一事件昨天的快照；首次出现时按当日条数估算
- 来源集中（单一来源 ≥70%）或单一平台刷屏会触发**降权**并在指标里标记
- 当所有材料都是旧内容时标记 `旧闻重传` 并降权
- `importance` 是与 `heat` 分开的第二个分数，避免只看流量
- 结论置信度只有三档：`verified_multi`（≥3 个独立媒体域名）、`reported`（有媒体报道）、`unverified`（仅热榜/社交）

权重可在「监控设置」或 `social_radar.json` 的 `scoring` 段修改。

---

## 4. 事件调查做了什么

对任一事件（或自由输入的查询）执行：

1. **起源追溯**：按发布时间排序，给出「当前可查最早来源」，并明确写「这不等于事件真正起源」；找不到就写「尚未找到最早来源」
2. **时间线**：每条材料标注时间、平台、来源类型、是否一手材料、是否引用了更早来源
3. **主张拆解**：把事件拆成可分别核验的句子，而不是整篇判真假
4. **双向取证**：同时收集支持材料（`support`）、反驳材料（`refute`）、更正材料（`correction`）和背景材料（`context`）
5. **状态判定**：多项独立证据支持 / 有直接证据支持 / 部分支持 / 争议中 / 证据不足 / 与现有证据矛盾 / 已被更正或推翻
6. **证据缺口**：明确列出「没有媒体报道」「独立来源不足 3 个」等限制
7. **指控与推测**：命中「指控/涉嫌/据传/可能」等语气时自动追加谨慎说明，不写成确定事实

**没有调用任何大模型**（`llm.enabled=false`）。结论全部由本地规则 + 已采集材料生成，每条都指回具体 URL。系统不会补写缺失的事实，也不会编造引文。

### 科技突破核验

事件命中 `investigation.tech_keywords`（芯片、大模型、量产、首次实现…）时自动触发，输出 11 段报告：

1. 发布事件摘要 2. 核心主张 3. 原始材料与指标 4. 国内报道 5. 国外评价 6. 独立验证与可比性 7. 支持证据 8. 限制性证据 9. 技术成熟度 10. 已确认/争议/缺口 11. 原文与来源链接

其中「可比性检查」强制列出 7 条禁止的对比方式（不同测试条件、实验室 vs 量产、论文 ≠ 领先、宣传参数 ≠ 第三方测试、外媒好评 ≠ 真实性证明、外媒没报道 ≠ 不存在、某方质疑 ≠ 抹掉已有数据）。

---

## 5. 配置

配置文件：`social_radar.json`（项目根目录）。缺失或部分缺失都能启动，缺项使用内置默认值。

```json
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "schedule": {
    "daily_time": "08:00",
    "collect_interval_minutes": 60,
    "retry_after_minutes": 10
  },
  "limits": { "max_candidates": 600, "max_events": 220, "source_timeout_seconds": 12 },
  "scoring": { "velocity": 0.3, "cross_platform": 0.2, "engagement": 0.2,
               "verification": 0.2, "interest": 0.1 },
  "report": { "top_events": 12 },
  "push": { "enabled": false, "items": 5, "alert_threshold": 82 },
  "interests": { "topics": ["AI 与大模型", "芯片与半导体"], "keywords": [], "exclude": [] },
  "filters": { "rules": ["[科技]", "+芯片", "!抽奖", "@8"] },
  "rss": { "feeds": [{ "id": "hn", "name": "Hacker News", "url": "https://hnrss.org/frontpage" }] },
  "investigation": { "min_heat": 70, "tech_auto_check": true },
  "sources": { "weibo_hot": { "enabled": false } }
}
```

### 5.1 时间线

| 动作 | 频率 | 说明 |
|---|---|---|
| 采集 | 每 `collect_interval_minutes`（默认 60 分钟） | 只抓取入库，不生成日报、不推送 |
| 出日报 | 每天 `daily_time`（默认 08:00）一次 | 聚类 → 故事 → 摘要 → 落库 |
| 推送 | 随日报一起，每天一次 | 同一天不会重复发 |

GitHub 模块同样改成每小时刷新、08:00 推送一次，两个模块的微信简报都在早上 8 点发出。

### 5.2 关键词语法

规则写在 `filters.rules`，一行一条：

| 写法 | 含义 |
|---|---|
| `[组名]` | 新开一组 |
| `+词` | 必须命中（组内多个 `+` 满足任一即可） |
| `!词` | 命中即排除，优先级最高 |
| `/正则/` | 正则匹配，可写 `/pattern/ => 别名` |
| `@数字` | 该组最多保留多少条（0 = 不限）|

解析失败的正则会被忽略并打印警告，不会让整份配置失效。

### 5.3 自定义 RSS 源

`rss.feeds` 里加一项就多一个源，不用改代码：

```json
{ "id": "hn", "name": "Hacker News", "url": "https://hnrss.org/frontpage",
  "platform": "HN", "language": "en", "source_type": "news",
  "max_items": 20, "enabled": true }
```

只有 `id/name/url` 是必填，其余走默认值。源 ID 会变成 `rss_<id>`，可以像内置源一样在「数据源管理」里单独开关。

环境变量（可选）：

| 变量 | 说明 |
|---|---|
| `RADAR_CONFIG` | 配置文件路径 |
| `RADAR_ENABLED` | 总开关 |
| `RADAR_DAILY_TIME` | 覆盖生成时间 |
| `RADAR_PUSH_ENABLED` | 覆盖推送开关 |
| `RADAR_SERVERCHAN_SENDKEY` | 雷达专用 SendKey；未设置时回退 `SERVERCHAN_SENDKEY` |
| `HTTPS_PROXY` | 访问被重置的源（如 BBC）时可设置本地代理 |

**只需启用部分数据源完全没问题**：把不用的源设为 `enabled: false` 即可，缺少任何平台都不影响其余流程。

---

## 6. 存储

新增 12 张表，全部以 `radar_` 前缀，不与原有表冲突：

| 表 | 用途 |
|---|---|
| `radar_sources` | 数据源健康状态、失败原因、延迟 |
| `radar_items` | 统一格式的原始材料（含 `url_hash` 去重、`content_hash` 跨源去重） |
| `radar_events` | 事件、热度/重要度、置信度、时间范围 |
| `radar_event_snapshots` | 每日快照，用于判断增速与持续发酵 |
| `radar_rank_history` | 每次采集的榜单位次，用来算「↑ 升温 / ↓ 降温 / 持平」 |
| `radar_stories` | 故事层（多角度合并后的结果） |
| `radar_summaries` | 摘要缓存（模型 + 内容哈希） |
| `radar_claims` | 事实主张与核验状态 |
| `radar_evidence` | 证据（来源、摘录、时间、是否一手） |
| `radar_claim_evidence` | 主张与证据的关系（支持/反驳/更正/背景） |
| `radar_reports` | 日报与调查报告存档 |
| `radar_push_log` | 推送去重与重试记录 |
| `radar_runs` | 每次运行的统计与错误 |

另外新增一张跨模块的 `seen_items` 表（`kind, key, seen_at`），记录你在两个模块里点开过什么，用来控制 NEW 标签。

增量采集：`radar_items.url_hash` 唯一，重复内容只更新 `last_seen_at`，不重复入库。
保留策略：`retention.days`（默认 90 天）只清理原始材料与运行记录，事件与日报永久保留。

---

## 7. 稳定性与安全

- 每个源独立超时（默认 12s）、最多 1 次重试、指数退避；4xx 不重试
- 单一源失败 → 记录到 `radar_sources`，继续采集其他源
- 采集整体失败 → 仍用已有材料生成日报，不会丢日报
- 雷达的所有入口都包在 `try/except` 里，异常不会传播到 GitHub 的接口和调度线程
- 外部文本一律视为不可信输入：去控制字符、截断长度、前端全部转义后渲染；不 `eval`、不拼 SQL
- 密钥只从环境变量读取，不写入代码、日志或前端
- 推送成功后写 `radar_push_log`，同一天同一通道不会重复发送；失败最多重试 3 次

---

## 8. 测试

```bash
python -m unittest discover -s tests -v
```

当前结果：**51 个测试全部通过**（离线运行，不访问网络，使用独立临时数据库）。

覆盖内容：

- 聚类：同内容合并、不同事件不误合、不丢条目
- 评分：平台分别归一化、无媒体佐证不得标为已验证、来源集中降权
- 主张状态：证据不足/部分/多方独立/争议/矛盾/更正 六种路径
- 调查：找不到来源时明确说明、时间线取最早条目、科技指标无法比较时明确标注
- 数据源：适配器齐全、无付费必需、未接入平台永不启用、单源失败不影响其他源
- 推送：默认关闭、缺 key 不抛异常
- 回归：原有 12 张表、13 个接口、调度线程名、`digest`/`prefs` 行为均未改变；`radar` 表与 `radar_*` 代码不得出现在原有 schema 中

---

## 10. 聚类、故事层、价值过滤与摘要（v2）

### 10.1 两级聚合：事件 → 故事

```
材料(items) ──聚类──► 事件(event) ──聚故事──► 故事(story) ──► 日报
```

- **事件**：同一批材料里措辞接近的条目合并。阈值 `cluster.within_run_threshold`（默认 0.30）。
- **跨轮匹配**：新一轮的条目与已有事件比较，阈值 `cluster.match_threshold`（默认 **0.35**，
  旧版写死 0.55，导致相似度 0.53 的同一件事被拆成两个事件）。同一轮新建的事件会立刻进入比对池。
- **故事**：事件之上再聚一层。两条规则：
  1. 标题包含系数 ≥ `cluster.story_threshold`（默认 0.42）；
  2. **稀有词共现**：共享 ≥2 个当日少见的关键词（地名、机构、专有名词）。
  
  第二条能把「西藏7.0级地震 / 那曲为近5年最大 / 地震局启动二级响应 / 亲历者讲述」合成一个故事，
  同时用停用片段（人民日报、新华社、记者、报道…）避免把「人民日报评星巴克」和
  「人民日报评综艺」误并成同一件事。

故事 id 由**成员事件集合**的哈希生成，不再由标题生成，避免两个不同故事撞 id 后成员互相污染。

### 10.2 价值过滤与信源权重

`filters` 配置里有三类规则，在入库前执行：

| 规则 | 默认 | 作用 |
|---|---|---|
| `noise_keywords` | 12 个词 | 丢弃娱乐段子、抽奖、鬼畜等噪音 |
| `min_engagement` | B站 view≥50000、HN points≥40 | 低于门槛的条目不进库 |
| `limits.hotlist_top_n` | 10 | 热榜类每个源只取前 10 条，避免单一榜单按条数刷屏 |
| `limits.news_top_n` | 40 | 新闻类保留更多 |

实测：一轮采集 447 条 → 过滤 110 条（25%）→ 入库 337 条 → 被故事覆盖 257 条，
**未覆盖比例从 72% 降到 24%**。

### 10.3 数据源熔断

| 连续失败 | 行为 |
|---|---|
| ≥3 次 | 降频：每 `backoff.degrade_interval_minutes`（默认 60 分钟）才重试一次 |
| ≥6 次 | 自动停用，状态变为 `paused`，日报里单独列出 |
| 任意成功一次 | 计数清零，恢复正常 |

数据源管理页有「重置失败计数」按钮（`POST /api/radar/sources/reset`），手动解除停用。

### 10.4 摘要：可选大模型 + 抽取式回退

默认**不调用任何模型**，此时用抽取式摘要：直接引用 2–3 条措辞不同的原文标题，格式为
「据今日头条：…；据微博：…」，关键数字只保留带单位的（`7.0级`、`400公里`、`75%`），
不再输出「2 个平台，3 条相关报道」这种模板句。

#### 在 Web 界面里配置（推荐）

大模型是全局配置，入口在左侧导航 **全局设置**（GitHub 项目解读与雷达摘要共用）：

![全局设置](llm-settings.png)

1. 「接入大模型生成摘要」选 **启用**
2. 「接口类型」选三类之一：**OpenAI 兼容**（OpenAI / DeepSeek / OpenRouter）、**Anthropic (Claude)**、**本地模型**
3. 「接口地址」按服务商填：`https://openrouter.ai/api/v1`、`https://api.deepseek.com`、`https://api.anthropic.com`、本地 `http://127.0.0.1:11434`
4. 「模型名」填 OpenRouter 上的模型，例如 `openai/gpt-4o-mini`
5. 在 **API Key** 里粘贴 `sk-or-...`，点 **保存设置**
6. 点 **测试连接**，会真的发一次请求并显示：是否返回、耗时、模型回显、返回内容片段

「测试连接」只做连通性验证（要求模型回一个 JSON），**不会**影响已生成的日报。

#### 密钥是怎么存的

- 只写入 `data/ai_secrets.json`（已在 `.gitignore` 里），**不会**写进 `app_settings.json`
- 任何接口都只返回「是否已配置」和掩码（如 `sk-o…7890`），不回传明文
- 环境变量 `RADAR_LLM_API_KEY` 优先于文件，方便用 `.env` 或系统环境管理
- 备用接口的 Key 单独存：环境变量 `RADAR_LLM_API_KEY_FALLBACK` 或文件里的 `llm_api_key_fallback`
- 监控设置里可以一键「清除密钥」

#### 主接口额度用尽怎么办

在全局设置里填「备用接口」，主接口返回 429 / 402 / `free-models-per-day` 时会自动切换，
并把切换状态写到 `data/ai_state.json`（记录「到哪天为止用备用」）；**第二天自动切回主接口**。
页面会显示当前用的是哪个接口、什么时候切回，也可以手动「立即切回主接口」。

另外还有本地 token 记账（`data/ai_usage.json`）：

| 情况 | 行为 |
|---|---|
| 主接口 token 用量达到 `llm.token_limits.primary` | 不等报错直接切备用 |
| 备用接口也达到上限，或也返回额度错误 | 暂停 AI `window_hours`（默认 24 小时）|
| 暂停期间调用 | 直接返回 `code: paused`，不发请求 |
| 到期或手动恢复 | 清零重新计数，从主接口开始 |

全局设置页的「Token 用量」面板会显示主/备各自的已用/上限进度条，以及最近 15 天的消耗折线图（横轴日期、纵轴 token，主接口实线、备用接口虚线）。
默认上限 50 万 tokens/天，`0` 表示不限。

#### 也可以直接改配置文件

```json
"llm": {
  "enabled": true,
  "provider": "local",
  "base_url": "http://127.0.0.1:11434",
  "model": "qwen2.5:7b"
}
```

- 密钥只从环境变量或本地 secrets 文件读取，不进代码、日志、前端
- 结果按 `(事件, 模型, 内容哈希)` 缓存到 `radar_summaries`，同一事件不会重复调用
- 模型超时、报错、返回非法 JSON → **自动回退到抽取式摘要**，日报不受影响
- 调用次数与 token 数可以在 `/api/radar/status` 的 `summary_backend` 里看到

已验证的路径：模型可用（mock 端点）、缓存命中、模型不可用回退、未启用、
Web 端保存/读取/掩码/清除密钥、测试连接的四种结果（可用 / 离线 / 未启用 / 无 key）。
真实模型的摘要质量需要你接入自己的模型后自行评估。

---

## 11. 已知限制

1. **故事层仍以标题为主**：两条报道如果标题用词差异很大（一个说「余震」，一个说「救援」），
   可能不会被合并；单角度故事仍占 87%
2. **不调用大模型时**，摘要只是原文标题的拼接，不会做跨来源归纳
3. 热榜类来源即使做了截断，仍然是数量最多的来源类型
4. 事件聚类是 O(n²) 的两两比较，候选上限 400 条以内没问题，再大需要改用索引或向量化
5. 任务调度依赖服务常驻，机器睡眠/关机期间不会采集

