# 统一 Web UI 规范

本次重构把 **GitHub 技术雷达** 与 **全球热点雷达** 收进同一个应用外壳，共用一套设计令牌、组件与交互规则。
技术栈保持不变：原生 HTML + CSS + JavaScript，无构建步骤、无框架、无 CDN 依赖。

![统一界面](ui-desktop.png)

<p align="center">
  <img src="ui-dark.png" width="49%" alt="深色主题">
  <img src="ui-mobile.png" width="24%" alt="移动端">
</p>

---

## 1. 页面结构

```
web/
├── index.html      GitHub 技术雷达（原有页面，重写为统一外壳）
├── radar.html      全球热点雷达（原有页面，重写为统一外壳）
├── status.html     系统状态（新增，只读聚合两个模块的既有接口）
├── styles.css      唯一的设计系统样式表（合并了原来的 radar.css）
├── shell.js        外壳行为：主题 / 侧边栏 / 视图切换
├── app.js          GitHub 业务逻辑（未改动业务，仅替换模板与新增展开块）
├── radar.js        雷达业务逻辑（未改动业务）
└── status.js       状态页渲染
```

页面骨架完全一致：

```
<div class="app">
  <aside class="sidebar">   品牌 · 业务模块导航 · 本页导航 · 页脚说明
  <div class="app-main">
    <header class="topbar"> 面包屑 · 页面状态 · 主操作 · 主题切换
    <main class="content">  模块自己的内容
```

三个模块之间的跳转只改 `<a href>`，外壳不重新加载任何状态；主题、侧栏折叠、卡片/列表视图都存在 `localStorage` 里，跨页面保持。

---

## 2. 设计令牌

全部定义在 `styles.css` 顶部，业务组件**不允许**写死颜色、圆角、阴影。

| 类别 | 令牌 | 浅色 | 深色 |
|---|---|---|---|
| 品牌 | `--fb-accent` | `#3452d9` | `#8ea4ff` |
| 背景 | `--fb-bg` | `#f6f7f9` | `#0d1014` |
| 面板 | `--fb-surface` | `#ffffff` | `#151a21` |
| 次面板 | `--fb-surface-2` | `#f2f4f7` | `#1a2029` |
| 边框 | `--fb-border` | `#e3e7ee` | `#262e3a` |
| 主文本 | `--fb-text` | `#14171c` | `#e9edf3` |
| 次文本 | `--fb-text-2` | `#55606f` | `#a7b1c0` |
| 辅助文本 | `--fb-text-3` | `#87909e` | `#7b8696` |
| 成功 | `--fb-success` | `#16794a` | `#5fd18c` |
| 警告 | `--fb-warn` | `#8a6100` | `#e8bb5c` |
| 危险 | `--fb-danger` | `#b93a34` | `#f2847d` |
| 信息 | `--fb-info` | `#1a63b8` | `#79b6f2` |

其他：间距 `--fb-sp-1..8`（4–40px）、圆角 `--fb-r-sm/md/lg/pill`、阴影 `--fb-shadow-1..3`、
动效 `--fb-dur` / `--fb-dur-slow` / `--fb-ease`、版心 `--fb-max-w: 1360px`、断点 1240/1080/860/560。

**状态不只用颜色表达**：状态标签始终带文字（如「今日已生成」「失败」「未接入」），表格健康列同时给出「正常 / 失败」文本。

---

## 3. 主题

- 三种模式：**浅色 / 深色 / 跟随系统**，点击顶栏太阳图标循环切换。
- 选择写入 `localStorage["fb.theme"]`，并在 `<head>` 的内联脚本里于首屏渲染前应用，避免闪烁。
- 也支持 URL 参数 `?theme=light|dark|auto`（便于分享与截图，不写入本地存储）。
- 深色主题是独立的一套令牌，不是简单反色：边框、阴影、状态色都单独调过。

---

## 4. 组件清单

| 组件 | 结构类名 | 说明 |
|---|---|---|
| 按钮 | `.primary-btn` `.ghost-btn` `.danger-btn` `.icon-btn` `.link-btn` `.push-btn` | 统一的 hover / active / focus / disabled |
| 分段控件 | `.seg` `.seg-btn` | 卡片/列表视图切换 |
| 标签页 | `.tabs` `.tab` `.radar-tabs` `.radar-tab` | 底部指示线，横向可滚动 |
| 状态标签 | `.chip` `.badge` `.tag` `.live-pill` | `.chip.ok/.warn/.info/.tech` 语义色 |
| 卡片 | `.card`（GitHub）`.ev-card`（雷达）`.event-card`（关注动态） | 同一套边框、圆角、悬停阴影 |
| 指标块 | `.metric` `.board-grid` `.radar-board` | 数字用等宽字体 |
| 表单 | `.field` `.field-row` `.toggle-grid` | 统一焦点环 |
| 表格 | `.radar-table` | 表头吸顶、行悬停 |
| 时间线 | `.timeline` `.t-time` `.t-title` `.t-meta` | 事件起源追溯 |
| 证据卡 | `.claim` `.claim-head` `.claim-evidence` | 主张 + 状态 + 证据链接 |
| 抽屉 | `.drawer` `.drawer-backdrop` | 口味画像；关闭态 `translateX(100%)` |
| 弹窗 | `.brief-modal` `.brief-backdrop` | 项目速读 |
| 折叠块 | `.card-more` | README 摘要按需展开 |
| 骨架屏 | `.skel-card` | 首屏加载，避免布局跳动 |
| 空 / 错误 | `.empty-state` `.error-state` `.empty-mark` | 带说明与下一步操作 |
| 通知 | `.toasts` `.toast` | 右下角浮层 |

**通用组件与业务组件分界**：`styles.css` 只描述视觉与状态；GitHub 的仓库指标、雷达的事件与证据状态都在各自的 `.card` / `.ev-card` 内部消费同一批基础类，没有把两种业务数据强行统一成同一结构。

---

## 5. 交互

| 交互 | 实现 |
|---|---|
| 主题切换 | `shell.js` 三态循环 + `localStorage` |
| 侧边栏折叠 | 桌面折叠为图标栏（`data-nav="collapsed"`），写入 `localStorage` |
| 移动端抽屉导航 | ≤860px 侧栏变为抽屉，`#navToggle` 打开，`Esc` 或点击遮罩关闭 |
| 卡片 / 列表视图 | 只切换 `#cardList` 的 `is-list` 类，同一份 DOM 重排，不改业务数据 |
| README 展开 | 原生 `<details>`，保留内容可访问性 |
| 抽屉 / 弹窗 | `transform` + `opacity` 过渡（180–260ms） |
| 反馈动画 | 卡片学习后短暂高亮；`FLIP` 重排动画沿用原有实现 |
| 滚动 | 顶栏在滚动后加阴影（`scrolled`），回到顶部按钮在 900px 后出现 |
| 减弱动效 | `@media (prefers-reduced-motion: reduce)` 把所有动画压到 0.001ms，功能不受影响 |

所有动画都只用于状态变化提示，没有无限循环装饰动画，也没有大面积动态背景。

---

## 6. 响应式

| 断点 | 变化 |
|---|---|
| > 1240px | 双栏：内容 + 320px 右栏 |
| ≤ 1240px | 右栏收窄到 288px |
| ≤ 1080px | 单栏，右栏横向铺开；指标 4 列 |
| ≤ 860px | 侧栏变抽屉，出现汉堡按钮；指标 2 列 |
| ≤ 560px | 卡片单列紧凑、按钮只留图标、状态药丸隐藏、表格可横向滚动 |

**中文排版的特殊处理**：因为中文没有空格，flex/grid 子项的 `min-content` 会把容器撑破。
统一给卡片、面板、指标块加了 `min-width: 0`，并给 `.chip` 加了 `max-width: 100%` + 省略号。

---

## 7. 可访问性

- 所有图标按钮都有 `aria-label`；切换类按钮用 `aria-pressed` / `aria-expanded` 表达状态。
- 标签页使用 `role="tablist" / role="tab" / aria-selected`，面板用 `role="tabpanel"`。
- 焦点可见：全局 `:focus-visible` 有 2px 强调色描边。
- 抽屉、弹窗保留 `aria-hidden` 与 `aria-modal`，`Esc` 可关闭。
- 正文对比度按 WCAG AA 选取（浅色 `#14171c` on `#fff` ≈ 16:1；辅助文字 `#55606f` ≈ 6.1:1）。
- 动效可被系统「减少动态效果」关闭，关闭后所有功能仍可用。

---

## 8. 实际浏览器验证

用本机 Chrome（headless + DevTools Protocol，视口模拟）实测，不是只看源码：

| 页面 | 视口 | `scrollWidth / clientWidth` | 渲染结果 | 控制台错误 | 网络错误 |
|---|---|---|---|---|---|
| GitHub 技术雷达 | 1440 | 1440 / 1440 | 24 张卡片 | 0 | 0 |
| GitHub 技术雷达（深色） | 1440 | 1440 / 1440 | 24 张卡片 | 0 | 0 |
| GitHub 技术雷达 | 1024 | 1024 / 1024 | 24 张卡片 | 0 | 0 |
| GitHub 技术雷达 | 768 | 768 / 768 | 24 张卡片 | 0 | 0 |
| GitHub 技术雷达 | 390 | 390 / 390 | 24 张卡片 | 0 | 0 |
| 全球热点雷达 | 1440 | 1440 / 1440 | 12 个事件卡 | 0 | 0 |
| 全球热点雷达（深色） | 1440 | 1440 / 1440 | 12 个事件卡 | 0 | 0 |
| 全球热点雷达 | 390 | 390 / 390 | 12 个事件卡 | 0 | 0 |
| 系统状态 | 1440 | 1440 / 1440 | 两块状态卡 + 运行记录 | 0 | 0 |
| 系统状态 | 390 | 390 / 390 | 单列 | 0 | 0 |

验证过程中发现并修复的真实问题：

1. 口味抽屉没有关闭态样式，首屏盖住正文 → 补 `translateX(100%)` 关闭态。
2. 自动更新时间显示成 `08:00Shanghai` → 补 `.schedule-time small` 样式。
3. 手机端顶栏溢出 → 隐藏面包屑尾部与日期药丸、按钮只留图标。
4. 手机端侧栏开关在抽屉内部，打开后点不到 → 顶栏新增 `#navToggle`。
5. 雷达页 390px 下溢出 23px → 数据源覆盖区的长 `.chip` 撑破布局，加 `max-width: 100%`。

早期截图曾显示「内容被切掉」，经 CDP 读取 `scrollWidth/clientWidth` 确认是 headless 默认视口与 `--window-size` 不一致造成的假象，不是真实溢出；改用 `Emulation.setDeviceMetricsOverride` 后数据一致。

---

## 9. 新增第四…第 N 个模块的接入方法

1. 复制任一页面的外壳（`<aside class="sidebar">` + `<header class="topbar">` + `<main class="content">`）。
2. 在侧栏「业务模块」里加一条 `.nav-item`，其它页面同步加（保持三处导航一致）。
3. 新建 `module.html` + `module.js`，在 `<head>` 放进主题引导脚本，`<body>` 末尾先加载 `/shell.js` 再加载业务脚本。
4. 业务脚本只做三件事：取数、渲染、绑定事件；**不要**自己实现按钮、标签、空状态、错误状态，直接用现有类名。
5. 需要新的视觉语义时，先加设计令牌，再写组件样式，不要把颜色写进页面。
6. 在 `tests/test_web.py` 里补一条「新页面的 DOM id 覆盖脚本所需 id」的断言。

外壳层（`shell.js`）不访问任何业务接口，因此新增模块不需要改它。

---

## 10. 已知限制

1. `status.html` 目前只聚合两个模块的状态与运行记录，没有做历史趋势图——`radar_runs` 里的数据还不足以支撑有意义的曲线。
2. 列表视图是纯 CSS 重排，没有独立的分页；一次渲染当前筛选下的全部条目（本地数据量级可接受）。
3. 主题切换是循环三态，没有做成下拉菜单；无障碍上通过 `aria-label` 播报当前模式。
4. 没有引入自动化视觉回归（截图 diff），视觉验证是人工看图 + 尺寸断言。
5. 移动端雷达的 6 个子页签是横向滚动容器，不换行。
