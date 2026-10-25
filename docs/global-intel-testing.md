# 全球态势监控 · 测试与手工验收

> 全部测试**离线**运行，沿用 `tests/_bootstrap.py` 的临时数据库与临时配置，
> 绝不写入用户的真实 `data/filter-bubble.db`。

---

## 1. 跑测试

```bash
python -m unittest discover -s tests -t tests
```

最近一次结果（2026-10-26）：

```
Ran 339 tests in 10.646s
OK
```

| 组成 | 数量 | 说明 |
|---|---|---|
| 阶段一/二基线 | 288 | 原有 GitHub / 雷达 / 论文 / AI 模块 |
| 阶段三新增 `tests/test_geo.py` | 51 | 地理、投影、USGS、主题分类、前端契约 |
| **合计** | **339** | 全绿 |

---

## 2. 新增测试覆盖（对应任务要求的 14 项）

| # | 要求 | 测试用例 |
|---|---|---|
| 1 | 地名映射与国家代码标准化 | `TestCountryMapping.*`（含 `normalise("cn"/"CHN"/"中国")`、主权国优先于属地、质心有效性） |
| 2 | 歧义地名不当作准确位置 | `TestGazetteer.test_longest_match_wins`、`TestGeocodeDiscipline.test_multiple_cities_same_country_falls_back_to_country` |
| 3 | 无可靠信息返回 null | `TestGeocodeDiscipline.test_no_place_signal_returns_none`、`test_multi_country_title_is_not_pinned` |
| 4 | 报道来源国 ≠ 事件发生国 | `TestOriginVersusEventCountry.*`（断言两者不同、多源分歧时留空） |
| 5 | 经纬度转换与投影 | `TestProjectionAndValidation.*`（四角、中心、往返、±180° 回绕、非法坐标） |
| 6 | 聚合标记关联真实事件 ID | `TestIntelGeoPayload.test_map_points_carry_real_event_ids`、`test_map_contains_only_geocoded_events` |
| 7 | USGS 解析与幂等 | `TestUsgsLayer.test_fixture_parses_into_valid_points`、`test_upsert_is_idempotent`、`test_invalid_features_are_skipped`、`test_broken_payloads_raise` |
| 8 | 外部源失败不影响新闻采集 | `TestUsgsLayer.test_upstream_failure_does_not_break_news_collection`（断言只记录失败、源状态为 error、不抛异常） |
| 9 | 五个面板字段与后端一致 | `TestIntelGeoPayload.test_five_panels_expose_required_fields` |
| 10 | 地图/时间线/情报/国家联动筛选 | `TestIntelGeoPayload.test_filters_are_consistent_between_map_and_feed`、`test_feed_and_map_share_ids` |
| 11 | `test_no_external_assets` 继续通过 | `TestFrontendContract.test_no_external_assets_still_holds`（另含 `test_web.py` 原用例） |
| 12 | `web/*.css` 只有一份 | `TestFrontendContract.test_only_one_stylesheet` |
| 13 | `radar.js` 的 id 契约 | `test_web.py::test_radar_has_every_id_radar_js_uses`（原用例）+ 新增 `test_intel_ids_exist_in_radar_html` |
| 14 | 基线 + 新增全绿 | 339 / 339 |

补充：`TestThemeClassification.*` 覆盖 9 个主题与"体育冲突不是冲突"；
`TestFrontendContract.test_intel_styles_use_design_tokens` 确保只用 `--fb-*` 令牌且仅在末尾追加。

---

## 3. 真实库副本上的接口验收（不写生产库）

```bash
python app.py                              # 或直接调用 radar_api.handle_get
curl "http://127.0.0.1:8791/api/radar/intel?days=7"
```

实测（2026-10-26，真实库）：

| 项目 | 实测值 |
|---|---|
| `GET /api/radar/intel` | **200**，`module=radar-intel`，`version=2` |
| 顶层字段 | `window / filters / coverage / threat_timeline / today / strategic / intelligence_feed / live_news / country_index / map / geo / missing_inputs / degraded / semantics / llm / disclaimer` |
| 地图 | `available=true`，**241 个新闻事件** + **159 个地震点** |
| 地图点位与数据库一致性 | 逐条比对 `radar_events.geo_lat/geo_lon`，**不一致 0 条** |
| 越界坐标 | **0 条** |
| 每个点位带 `event_id` | **是** |
| 按置信度 | `medium=107`，`low=134`，`high=0` |
| 按图层 | `usgs_quake=159`（窗口内）；库内共 192 |
| `theme=disaster` 过滤 | 27 点，全部 `theme=disaster` |
| `country=FR` 过滤 | 16 点，全部 `country_code=FR` |
| 情报流与日报 `story_id` | 集合完全一致（12 条） |
| 情报流含坐标条目 | 4 / 12 |
| 时间线 | `history_days=3`，`confidence=insufficient`，`comparability=legacy_only`，缺失日期 `available=false` |
| 战略风险 | `observed_activity=43.1`（`basis=legacy_caliber`），`estimate=39.7`，标注为估算 |
| 既有接口 | `/status /report /events /sources /history` 全部 **200**，旧字段齐全 |

> 注意：接口返回 200 **不代表**数据一定被正确处理。上表中的"逐条比对""越界 0 条""幂等"三项
> 就是为了避免只看状态码。

---

## 4. 地理识别质量（人工标注 200 条）

样本文件：`tests/fixtures/geo-labelled-sample.tsv`
评估脚本：`python scripts/eval-geocode.py --json /tmp/eval.json`

样本构成（**真实样本，未凑数**）：

* 从 886 条事件中分层抽样：**已定位 150 条 + 未定位 50 条 = 200 条**；
* 人工标注：有单一国家依据 **132 条**；应判为未知 **68 条**（标题无地名、跨国、球队/赛制等）；
* 标注口径：`expected_country` 只在"标题提供了可核验的单一国家依据"时填写，否则填 `-`。

结果：

| 口径 | 精确率 | 召回率 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|
| **标题级**（`resolve_title`） | **87.2%** | **99.2%** | 129 | 19 | 1 | 51 |
| **生产级**（标题+成员条目） | **85.0%** | **100%** | 130 | 23 | 0 | 47 |

各置信等级准确率（生产级）：

| 置信度 | 样本数 | 准确率 |
|---|---|---|
| `medium`（地名匹配） | 73 | **86.3%** |
| `low`（国家质心） | 80 | **83.8%** |
| `high` | 0 | 不适用（当前语料无来源坐标） |

**主题分类准确率：72.0%（200 条）**。

### 23 条错误/偏差的成因（已逐条看过）

| 类型 | 数量 | 例子 |
|---|---|---|
| 以城市命名的球队/俱乐部 | 11 | `广东3将回暖`、`北京2迷将齐爆`、`辽宁铁人`、`大连英博`、`国乒vs韩国` |
| 主体国 ≠ 发生地 | 3 | `中国女孩赴泰…被绑往泰缅边境`（锚到 CN，应为 TH） |
| 跨国评论被单国锚定 | 4 | `美以密谋…伊朗最高领袖`、`俄乌…美国中期选举` |
| 城市指代非事件地 | 2 | `上海12岁少年登顶乞力马扎罗`（事件在坦桑尼亚） |
| 行政区代码粒度差异 | 1 | `香港中乐团` → HK（人工标 CN，两者都说得通） |
| 条目级补充命中 | 2 | 标题无地名，但成员条目里有 |

**结论**：现有地理识别可用于"地图上大致看分布"，**不足以做国家级别的精确统计**。
这一点直接体现在国家面板上：标注为 `coverage=media_only`、`partial=true`、
并明示"不是官方冲突烈度指数"。

---

## 5. 手工验收清单

在浏览器打开 `http://127.0.0.1:8791/radar.html` → 「全球态势」：

- [ ] 顶部 5 个指标有值（活动 / 高优先级 / 已定位 / 来源健康 / 更新时间），无硬编码数字
- [ ] 地图显示陆地与国界；断网刷新后**底图仍在**（本地 `world-110m.js`）
- [ ] 地图上能看到新闻点位（彩色圆）与地震点位（空心圈）
- [ ] 图例可读；`国家级近似` 项为虚线样式
- [ ] 滚轮缩放、拖拽平移、`+ / − / ⌂` 按钮可用；缩小时点位会聚合成数字气泡
- [ ] 点击点位 → 下方详情出现标题/主题/位置/定位方式/严重度/依据
- [ ] 点击情报卡 → 地图上对应点位高亮，卡片滚动到可视区
- [ ] 点击「深入调查」→ 跳转到「事件调查」并按标题发起调查（复用既有能力）
- [ ] 图层面板：`USGS 地震` 可开关；`GDACS / EONET` 显示「未接入」且计数 0
- [ ] 时间线：活动（红实线）与可信度（蓝虚线）分开；虚线圆圈表示反推口径
- [ ] 时间线显示「历史深度不足」与真实可用天数，**没有插值曲线**
- [ ] 点击时间线某一天 → 地图与情报流按该日筛选；再点一次或「清除」恢复
- [ ] 战略风险：显示估算值 + 分项条 + 权重 + 缺失输入 + 「非官方指数」标注
- [ ] 国家面板：显示「部分可用」；样本不足的国家显示"样本不足"而非 0
- [ ] 实时新闻：无发布时间的条目显示「无发布时间」；热榜条目显示「热榜发现·无正文」
- [ ] 「加载更早」用 `before_id` 翻页，无重复条目
- [ ] 切到其它标签页再切回来，实时新闻恢复增量轮询（`since_id`）
- [ ] 浏览器控制台 **0 报错**
- [ ] 原有 6 个标签页（每日热点/事件调查/科技突破核验/数据源管理/历史记录/监控设置）功能正常
- [ ] 移动端窄屏（<560px）单列布局，无横向滚动

---

## 6. 不做什么（防回归）

* 不新增 `.css` 文件（`web/*.css` 仍只有 `styles.css`）；
* 不在页面里引用任何 `http(s)://` 资源；
* 不在 `shell.js` 里发起业务接口请求；
* 不改 `radar.js` 既有函数体（只加一行 `RBIntel.ensureLoaded()`）；
* 不新增一级页面（「全球态势」是 `radar.html` 内的第 7 个 tab）。

---

# 阶段四 A：质量提升与真实浏览器验收（2026-10-26）

## 7. 测试总览

```
python -m unittest discover -s tests -t tests     → Ran 356 tests / OK
node scripts/browser-test.mjs                     → 22/22 通过
```

| 组成 | 数量 |
|---|---|
| 阶段三基线 | 339 |
| 阶段四新增（地理类别回归 + 精度表达） | 17 |
| **Python 合计** | **356** |
| 真实浏览器用例（独立运行，不在 unittest 内） | 22 |

## 8. 真实浏览器测试（零新增依赖）

用 **Edge/Chrome 的 DevTools Protocol** 驱动，Node 内置 `fetch` + `WebSocket`，
**没有引入 Playwright / Puppeteer / Selenium**，所以不会成为生产运行时依赖。

```bash
node scripts/browser-test.mjs              # headless
node scripts/browser-test.mjs --headful     # 需要肉眼观察时
```

* 服务端用「真实库副本 + 临时配置（关闭雷达/论文调度）」启动，**不写生产库**；
* 端口默认 8899 / CDP 9333，可用 `--port` `--cdp-port` 覆盖。

覆盖的 22 项：启动与 7 个 tab、切到「全球态势」、底图与真实 API 数据、点位详情、
情报卡联动定位、聚合气泡点击放大、主题/国家/时间/图层/精度筛选、时间线按日筛选、
Live News 分页与 `since_id` 轮询无重复、1366×768 与 390×844 布局无横向溢出、
主接口失败仍可用、空数据不出现 NaN、全程 0 未捕获异常、**页面不请求任何外部主机**、
阻断常见 CDN/瓦片后地图仍完整。

> 该套测试在本阶段**抓出了 3 个真实缺陷**（详见 §10），这正是它相对 `node --check` 的价值。

## 9. 地理识别：调参集 vs 独立验证集

| 数据集 | 用途 | 精确率 | 召回率 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|
| `geo-labelled-sample.tsv`（200） | **调参集**（规则在此上调） | **93.98%** | **96.15%** | 125 | 8 | 5 | 62 |
| `geo-validation-sample.tsv`（100） | **独立验证集** | **90.0%** | **91.84%** | 45 | 5 | 4 | 46 |

各置信等级（生产级）：

| 置信度 | 调参集样本 / 准确率 | 验证集样本 / 准确率 |
|---|---|---|
| `medium`（地名匹配） | 65 / 93.9% | 24 / 87.5% |
| `low`（国家质心） | 68 / 94.1% | 26 / 92.3% |
| `high`（来源坐标） | 0 | 0（当前语料无来源坐标） |

与阶段三相比：生产级精确率 **85.0% → 93.98%**，错误数 **23 → 13**；
已定位事件 **241 → 196（27.2% → 22.1%）** —— 这是**用召回换精确**的自觉取舍。

### 独立验证的可信度说明（重要）

* 验证集 100 条**从未参与规则调参**，标签在规则固定后才写；
* 但它在评估后被我**逐条查看过错误**（用于写本报告），因此严格说它已经"被看过一次"；
* 两个集的**标注严格度不完全一致**：验证集按"只有一个国家且事件确实主要发生在该国"更严格地标 `-`，
  所以两集数字不能直接相减当作"泛化损失"；
* 结论只对**这批中文新闻语料**成立，**不能外推到所有未来新闻**。

## 10. 本阶段修掉的真实缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | 主题下拉框只有一个静态 `<option>`，`renderFilters` 用 `options.length` 判断是否已填充 → 永远不填充 | **主题筛选完全失效**（选什么都等于"全部"） | 每次重建选项并保留当前值 |
| 2 | 聚合气泡没有点击处理 | 默认缩放下 241 个事件挤成 16 个气泡，点击**毫无反应**（交互死区） | 气泡可点击 → 放大到成员展开并居中 |
| 3 | 情报卡"地图定位"只设置选中态 | 选中点若在聚合气泡内，**看不到任何高亮** | 定位时居中并把缩放提到展开阈值（≥2.4） |
| 4 | `--force` 补全只重算"未标注"事件 | 规则收紧后**存量数据不会更新**，评估口径与代码不一致 | `force=True` 时选取全部事件 |
| 5 | 英文国名用原大小写匹配小写文本 | `Ukraine` 等**永不命中** | 匹配前统一 `lower()` |
| 6 | 繁体写法缺失 | `美國`/`烏克蘭` 等不命中 | 补常见繁体别名 |

## 11. 逐类回归（对应"已知 23 类偏差"）

`tests/test_geo.py::TestGeoRegressionByCategory` 覆盖：

| 类别 | 规则 | 用例 |
|---|---|---|
| 球队/俱乐部/体育赛事 | 体育语境下**必须**有"在地线索"（举行/开赛/开跑/现场…）才保留地名；另加俱乐部名词表与"地名+人数"模式 | 辽宁铁人、大连英博、广东3将、北京男篮、国乒vs韩国、国乒vs日本 |
| 主体国≠发生地 | 只给低置信度，绝不标 `high` | 中国女孩赴泰遇险 |
| 籍贯被当发生地 | "地名+年龄/人数"不算地点 | 上海12岁少年登顶乞力马扎罗 |
| 跨国评论/外交表态 | 跨国缩写（中美/俄乌/美以…）与知名行为体（领导人/企业/驻华/对华）与地名国家**冲突**时，丢弃国家级命中 | 美以…伊朗、俄乌…美国选举、商务部…美国防部、中比贺信、金正恩访问丹东 |
| 跨国但有明确城市 | 保留城市级证据 | 俄乌冲突升级，基辅遭袭 → UA |
| 歧义/机构名 | 返回未知 | 上海合作组织、瑞士轮、美国警告中国影响日本 |
| 只有国家级证据 | 国家质心 + `low` | 中国科学家取得突破 |
| 无依据 | `None` | 折叠屏手机发布 |

> **没有**采用"凡含球队/城市/国家就一律不定位"的粗暴做法：真实赛事
> （"2026天津马拉松开跑"）因为带在地线索仍保留为 CN/medium。

## 12. 主题分类

| 数据集 | 阶段三 | 阶段四 | 变化 |
|---|---|---|---|
| 调参集（200） | 72.0% | **81.0%** | +9.0 |
| 独立验证（100） | 65.0% | **81.0%** | +16.0 |

主要修复：中国党政人事（省委/市委/常委/书记/当选/被查…）、
灾害救援（震区/震中/救援/灾区/烟霾…）、公共安全（火灾/间谍/国际刑警）、
武装冲突（袭击/武装/炸弹/交火/胡塞）、社会动荡（暴乱/抗争）、
科技别名（微软/苹果/谷歌/特斯拉/SpaceX/手机/手表/DRAM…）。

混淆矩阵（调参集，行=人工，列=自动）：

| expected \ auto | 冲突 | 动荡 | 政治 | 安全 | 灾害 | 基建 | 科技 | 综合 |
|---|---|---|---|---|---|---|---|---|
| 冲突 | **4** | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| 动荡 | 1 | **5** | 0 | 0 | 0 | 0 | 0 | 2 |
| 政治 | 1 | 0 | **10** | 0 | 0 | 0 | 1 | 23 |
| 安全 | 0 | 0 | 0 | **1** | 0 | 0 | 0 | 1 |
| 灾害 | 0 | 0 | 0 | 0 | **21** | 0 | 0 | 3 |
| 基建 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| 科技 | 0 | 0 | 0 | 0 | 0 | 0 | **13** | 7 |
| 综合 | 0 | 0 | 1 | 0 | 0 | 0 | 9 | **90** |

已知偏差方向：

* **政治 → 综合**（23）：大量人事/例行表态标题没有强政治词，属于"漏判"而非"误伤"；
* **综合 → 科技**（9）：AI/芯片类市场与科普稿件容易落进科技，属可接受的模糊地带；
* **冲突/安全几乎没有被过度分类**：这是有意的——体育"冲突"、政治"抨击"都被抑制，
  宁可漏判也不制造高风险标签。

> 主题准确率 81% **不等于可以当事实分类使用**；歧义标题仍应看 `evidence` 与原文。

## 13. 地图位置表达（§5）

* 三种置信等级用**不同形状**，不只是颜色：
  * `high`（来源坐标）：实心圆 + 外环
  * `medium`（地名匹配）：实心圆
  * `low`（国家质心）：**菱形 + 虚线 + 半透明**
* 新增「位置精度」筛选：全部 / 仅中高置信 / 仅国家级近似；
* 详情面板固定显示「定位方式」「位置精度」「依据（geo_source）」，`low` 额外显示
  「国家级近似位置：只确认了国家，坐标取国家质心，不是事件精确地点」；
* 图例与「导出当前视图」的 CSV 都保留精度字段与说明行；
* 地图下方常驻底图来源与许可证说明。

## 14. 仍未完成 / 已知限制

1. **无基线的国家面板**：`coverage=media_only`、`partial=true`，不是官方冲突烈度指数。
2. **地理覆盖降到 22.1%**：精确率优先的必然结果；补地名库比放宽规则更可靠。
3. **体育队名仍有遗漏**（"北京2迷将"这类无任何体育词的标题）。
4. **主题的"政治→综合"漏判**仍占最大比重（23/200）。
5. **前端只有一套浏览器测试**（Chromium/Edge），未覆盖 Firefox/Safari。
6. **中国境内公开展示的边界合规**未处理，见 `docs/global-intel-data-sources.md`。
7. 真实外部 API（USGS）只做**可选**手动验证，未纳入离线自动测试。

---

# 阶段四 B：数据源扩展的测试记录（2026-10-26）

## 15. 测试总览

```
python -m unittest discover -s tests -t tests     → Ran 383 tests / OK
node scripts/browser-test.mjs                     → 22/22 通过
```

| 组成 | 数量 |
|---|---|
| 阶段四 A 基线 | 356 |
| 阶段四 B 新增 `tests/test_geo_layers.py` | 27 |
| **合计** | **383** |

阶段四 B 改写了 2 个阶段三用例（行为确实变化，不是放宽标准）：

* `test_layer_catalog_is_honest_about_unimplemented_layers`：
  原来断言"gdacs/eonet 必须 connected=false"，现在断言**不变量**——
  凡 `IMPLEMENTED_LAYERS` 内的必须 `connected=true`，其余必须 `false`；
* `test_upstream_failure_does_not_break_news_collection`：
  原来写死"只有 1 个地理源失败"，现在按**实际启用数**断言全部地理源各自失败、
  各自记录健康状态（新源接入后固定值必然过时）。

## 16. 新增离线测试覆盖（对应 §10 的 12 项）

| # | 要求 | 用例 |
|---|---|---|
| 1 | EONET 代表性解析 | `TestEonetParsing.test_parses_captured_response`（真实抓取样本 6 事件） |
| 2 | GDACS 代表性解析 | `TestGdacsParsing.test_parses_captured_response`（真实抓取样本 12 要素、6 种灾害） |
| 3 | 点/面几何区分 | `test_point_geometry_uses_source_coordinates`、`test_polygon_is_area_not_a_precise_point` |
| 4 | 唯一 ID 与重复更新 | `TestGeoPointIdempotency.test_repeat_capture_updates_instead_of_duplicating`、`test_unique_id_combines_type_event_episode` |
| 5 | 缺失/无效坐标 | `test_invalid_or_missing_geometry_is_skipped`、`test_invalid_coordinates_are_skipped`（含 999/99 与 (0,0)） |
| 6 | 429/超时/坏 JSON/空结果 | `test_broken_payloads_raise`（两处）、`TestUpstreamFailures.test_http_429_...`、`test_empty_result_is_not_an_error` |
| 7 | 单源失败不影响其它 | `TestUpstreamFailures.test_http_429_is_recorded_and_does_not_stop_others` |
| 8 | 不污染新闻聚类 | `test_geo_points_never_become_news_items`（断言 `radar_items`/`radar_events` 均为 0） |
| 9 | 国家字段语义 | `TestMapIntegration.test_geo_points_carry_no_fabricated_country` |
| 10 | 源状态/更新时间/错误 | 同上 429 用例断言 `status/ok/fail/last_error` |
| 11 | 地图与五面板继续正常 | `test_map_payload_exposes_hazards_and_geometry`、`test_hazard_filter_narrows_points`、`test_api_exposes_hazard_parameter`、`test_main_endpoint_keeps_all_panels`、`test_duplicate_hints_flag_but_do_not_merge` |
| 12 | 基线继续通过 | 383 / 383，浏览器 22 / 22 |

## 17. Fixture 来源与诚实边界

| 文件 | 来源 | 说明 |
|---|---|---|
| `tests/fixtures/eonet_events.json` | **真实抓取**（2026-10-26，`events?status=open&days=7`） | 6 个事件，每条 geometry 截到最后 3 条，6 KB |
| `tests/fixtures/gdacs_events.json` | **真实抓取**（同日 `geteventlist/SEARCH`） | 12 个要素，覆盖 6 种 eventtype，28 KB |
| `tests/fixtures/eonet_polygon.json` | **构造样本** | 实时接口只有 Point（实测 308/308），因此按官方 v3 结构构造以覆盖 Polygon 分支；文件内标注 `CONSTRUCTED FIXTURE`，**不声称存在线上面数据** |

## 18. 在线集成验证（可选，手动执行，不在自动测试内）

```
EONET   GET /api/v3/events?status=open&days=7&limit=200
        HTTP 200 · Content-Type 谎报 application/rss+xml，实为 JSON · 8 事件 · ~1.4 s
GDACS   GET gdacsapi/api/events/geteventlist/SEARCH
        HTTP 200 · FeatureCollection 86 要素 · ~3.3 s
USGS    GET .../4.5_week.geojson
        HTTP 200 · 191 条（0 新增 / 191 更新，验证幂等）
RSS     BBC 30 / Guardian 30 / Al Jazeera 25 / NPR 10 = 95 条，全部 language=en
GDELT   GET doc/doc → HTTP 429（"limit requests to one every 5 seconds"），维持 paused
```

复现：

```bash
python scripts/collect-now.py --only usgs_quake,eonet,gdacs,bbc_world,guardian_world,aljazeera,npr_world
```

## 19. 仍未覆盖

1. **没有真实 Polygon 样本**（线上全是 Point）。
2. **GDACS 与 EONET 的"同一事件"关系没有权威关联 ID**，只做邻近提示，不做合并。
3. **在线集成测试未纳入自动化**（要求离线，且外部源不稳定）。
4. **国家基线未实施**（见 `docs/global-intel-data-sources.md` §11）。
