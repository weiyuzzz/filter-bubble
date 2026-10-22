# 学术论文模块

在现有「全球热点雷达」里新增的第四个功能页：**学术论文**。入口 `/radar.html` → 「学术论文」标签。

数据表用 `paper_` 前缀，与 `radar_*`、GitHub 的表完全隔离；HTTP、SQLite、大模型、调度都复用现有实现，没有另起服务。

---

## 1. 数据源实测结果

测试时间：2026-10-23 04:16（Asia/Shanghai），本机直连，回溯 3 天，每源最多 30 条。

| 来源 | 状态 | 记录 | 摘要覆盖 | 耗时 | 说明 |
|---|---|---|---|---|---|
| Crossref | ✅ 可用 | 30 | **100%** | ~7.5s | 按 6 个学科分别检索 `has-abstract:true`，标题/日期/DOI 齐全 |
| PubMed | ✅ 可用 | 29 | 76% | ~1.8s | E-utilities（esearch + efetch），部分条目无摘要 |
| Europe PMC | ✅ 可用 | 30 | 90% | ~1.7s | 生物医学补充，含部分预印本 |
| bioRxiv | ✅ 可用 | 30 | **100%** | ~2.3s | 生物学预印本 |
| medRxiv | ✅ 可用 | 30 | **100%** | ~1.9s | 医学预印本 |
| HuggingFace daily papers | ✅ 可用 | 30 | **100%** | ~0.6s | AI/CS 论文，摘要来自 arXiv 元数据 |
| OpenAlex | ⚠️ 需 Key | — | — | — | 无 Key 时共享 IP 预算返回 **HTTP 429**（`Insufficient budget`）|
| arXiv RSS | ❌ 不可用 | — | — | — | **TLS 证书链缺中间证书**（CA 为 Certainly），`CERTIFICATE_VERIFY_FAILED` |

### arXiv 为什么放弃接入

单独诊断过：

```
rss.arxiv.org   -> 199.232.163.42
export.arxiv.org-> 146.75.115.42
验链失败：unable to get local issuer certificate
服务端只发 leaf 证书，issuer = Certainly Intermediate R1
把 Windows 系统根证书（62 张）也加载进来后仍然失败 → 缺的是中间证书，不是根证书
```

按规范没有关闭证书校验，也没有使用 `http://` 绕行（arXiv 会 301 到 https）。因此 arXiv 明确标记为**暂不可用**，CS/物理/数学/工程改由 Crossref + HuggingFace + OpenAlex（配 Key 后）覆盖。

### OpenAlex 怎么启用

免费 Key 在 <https://openalex.org/settings/api> 申请，然后：

```bash
set OPENALEX_API_KEY=你的key
```

或写进 `.env`。配置里打开来源即可：

```json
"papers": { "sources": { "openalex": true } }
```

---

## 2. 学科覆盖

| 学科 | 主要来源 | 实测篇数（首次采集） |
|---|---|---|
| 计算机科学与人工智能 | HuggingFace + Crossref | 41 |
| 医学与生物医学 | PubMed + Europe PMC + medRxiv | 88 |
| 生物学 | bioRxiv + Europe PMC | 46 |
| 工程与电子信息 | Crossref | 7 |
| 物理学 | Crossref | 5 |
| 数学 | Crossref | 5 |
| 金融与经济学 | Crossref | 5 |
| 全部（去重后） | 以上全部 | **178** |

首次采集 178 篇，其中 **168 篇有摘要（94%）**。

分类逻辑在 `core/papers/classify.py`：

1. 来源自带领域（PubMed → 医学，bioRxiv → 生物，HuggingFace → CS）加 2 分；
2. 期刊/会议名命中领域词加 3 分；
3. 标题+摘要特征词：**摘要命中 ≥2 个或标题命中 ≥2 个**才计分，避免单个普通词误判；
4. 只保留分数 ≥ 最高分 60% 的学科，允许一文多学科。

---

## 3. 数据表

| 表 | 用途 |
|---|---|
| `paper_items` | 论文主表：标题、原始摘要、链接、DOI/PMID/arXiv、学科、日期、多来源列表 |
| `paper_ai_overviews` | AI 概况：状态、模型、提示词版本、内容指纹、错误 |
| `paper_source_runs` | 每个来源每次采集的结果、耗时、错误 |

初始化是**向前兼容**的：`CREATE TABLE IF NOT EXISTS` + 启动时把 `2026-Oct-21` 这类日期就地归一为 ISO，不动任何既有表。

### 去重

| 优先级 | 键 | 例子 |
|---|---|---|
| 1 | DOI | `doi:10.1000/xyz` |
| 2 | PMID | `pmid:42903173` |
| 3 | arXiv ID（去掉版本号） | `arxiv:2410.12345` |
| 4 | 规范化标题 | `title:<sha1>` |

合并规则：

* 同一篇被多个来源收录 → 只留一行，`sources_json` 记录全部来源；
* 已有摘要不会被空摘要覆盖；
* 已有链接/日期/学科不会被空值覆盖；
* 每次采集重复出现只更新来源列表，**不会重复入库**。

---

## 4. 数据流

```
来源适配器 ─┐
            ├─► 分类 ─► 去重入库(paper_items) ─► AI 队列 ─► paper_ai_overviews
            │                                              │
        失败记录(paper_source_runs)                        ▼
                                                    /api/radar/papers
                                                          ▼
                                                   论文页卡片
```

* **采集**：`core/papers/sources.py` 每个来源一个函数，互不影响；失败写 `paper_source_runs`。
* **分类**：采集后立刻按统一映射打分，不在前端判断。
* **入库**：`store.upsert_papers`，按上面的优先级合并。
* **AI**：只给"需要展示"的论文排队（最多 `ai.max_per_run`，默认 24），有界队列 + 2 线程，失败标记 `failed` 可重试。

---

## 5. AI 概况

代码在 `core/papers/ai.py`，复用全局大模型配置（`/settings.html`），不新建设置入口。

提示词要求模型输出 5 个字段：研究内容 / 核心方法 / 主要发现 / 研究意义 / 信息局限，并且：

* 只依据标题与摘要，**禁止补写摘要里没有的结果**；
* 摘要没提的内容必须写「摘要未提供足够信息，无法判断」；
* 原始摘要与 AI 概况**分开存储**，前端分区展示，不会混淆；
* 不做 PDF 全文下载，也不声称读过全文。

缓存键是 `(标题, 摘要, 来源)` 的指纹 + 提示词版本：

* 刷新页面 / 切换学科 / 重复采集同一篇 → 命中缓存，不再调用模型；
* 摘要变了或提示词版本升级 → 才会重新分析；
* 失败可单篇重试，也可「重试全部失败」。

---

## 6. 配置

写在 `social_radar.json`（缺省有安全默认值）：

```json
"papers": {
  "enabled": true,
  "daily_time": "08:30",
  "lookback_days": 3,
  "fetch_per_source": 30,
  "per_discipline_limit": 10,
  "sources": {
    "crossref": true, "pubmed": true, "europepmc": true,
    "biorxiv": true, "medrxiv": true, "huggingface": true,
    "openalex": false
  },
  "openalex_api_key_env": "OPENALEX_API_KEY",
  "crossref_mailto": "",
  "ai": { "enabled": true, "max_per_run": 24, "concurrency": 2 }
}
```

| 字段 | 含义 |
|---|---|
| `daily_time` | 每天几点自动采集论文（与雷达 08:00 的日报错开） |
| `lookback_days` | 回溯天数，覆盖数据源更新延迟 |
| `fetch_per_source` | 每个来源每次最多取多少条 |
| `per_discipline_limit` | 每个学科最多展示多少篇 |
| `ai.max_per_run` | 每次任务最多分析多少篇（控制模型额度） |

---

## 7. 命令

```bash
# 实测所有数据源（真实请求，打印状态/字段完整率/错误）
python app.py radar papers test-sources

# 只测一个来源
python app.py radar papers test-sources crossref

# 采集一次并分析（CLI 会等 AI 队列跑完）
python app.py radar papers collect

# 只把待分析论文补进队列
python app.py radar papers analyze

# 查看状态
python app.py radar papers status
```

页面操作：**全球热点雷达 → 学术论文** → 右上角「采集论文」/「刷新列表」，卡片上的「重试」。

---

## 8. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/radar/papers?discipline=cs&limit=10&offset=0` | 论文列表（按日期倒序） |
| GET | `/api/radar/papers/status` | 状态、来源健康、学科计数、队列长度 |
| POST | `/api/radar/papers/collect` | 触发采集（异步） |
| POST | `/api/radar/papers/retry` `{"id": "..."}` | 重试单篇 AI |
| POST | `/api/radar/papers/retry-failed` | 重试全部失败 |
| POST | `/api/radar/papers/analyze` | 把待分析论文补进队列 |
| POST | `/api/radar/papers/test-sources` | 实时测试数据源 |

参数都会校验：未知学科回退到 `all`，`limit` 夹在 1–50。

---

## 9. 测试

```bash
# 全部自动化测试（默认离线，不依赖外网）
python -m unittest discover -s tests

# 只跑论文模块
python -m unittest tests.test_papers -v

# 真实数据源连通性（会发真实请求）
python app.py radar papers test-sources
```

覆盖范围（`tests/test_papers.py`）：

* 分类：来源提示、期刊名、关键词阈值、单关键词不误判、多学科、未知返回空；
* 去重：同 DOI、跨来源、arXiv 版本号、无标识回退标题、空摘要不覆盖、空标题跳过；
* 学科筛选：只返回匹配项、`all` 返回全部；
* AI：缺字段拒绝、超长截断、未启用模型、无摘要不调用、缓存命中不重复；
* 日期归一；
* API 参数校验与 404。

---

## 10. 已知限制

1. **arXiv 不可用**：本机 TLS 证书链缺中间证书，按规范没有绕过；等 arXiv 修好证书链或本机证书库更新后可直接启用（适配器已写好结构）。
2. **OpenAlex 需要 Key**：无 Key 时共享预算经常 429，默认关闭。
3. **AI 概况依赖模型额度**：OpenRouter 免费模型有每日上限（`free-models-per-day`）。额度用尽时卡片显示「AI 概况生成失败」+ 具体原因 + 重试按钮，论文原文不受影响。
4. **Crossref 摘要来自出版商**：少数条目摘要带 HTML 标签（已清洗）或只有一句简介。
5. **可能有"未来日期"**：Crossref 的 ahead-of-print 记录会给出正式出版日期，看起来比采集时间晚，这是来源数据而非计算错误。
6. **每个学科最多 10 篇是展示上限**，不是采集上限；调整 `fetch_per_source` 与 `per_discipline_limit` 可以扩大。
