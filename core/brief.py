"""Human-readable project briefs: who it is for, how to use it, what it solves.

These are deliberately generated from facts we already have (description,
language, topics, category) rather than invented product claims.  The UI shows
a small "以 README 为准" caveat alongside every brief.
"""
from __future__ import annotations

from typing import Any, Dict, List

GUIDES: Dict[str, Dict[str, Any]] = {
    "AI / 大模型": {
        "audience": "AI 应用开发者、想把模型能力接进产品/内部流程的团队",
        "problem": "把模型能力接进真实工作流，少写一层 RAG、Agent 或工具调用的胶水代码",
        "scenarios": ["给现有产品加一个 AI 助手", "搭建团队内部的 Agent / 知识库工作流"],
    },
    "开发者工具": {
        "audience": "每天在终端、编辑器或 CI 里工作的开发者",
        "problem": "减少重复命令、上下文切换和手工操作，把常见流程收成一个可复用工具",
        "scenarios": ["本地开发提效", "把团队常用流程固化成 CLI / 插件"],
    },
    "Web / 前端": {
        "audience": "前端工程师、独立开发者和需要快速做产品原型的团队",
        "problem": "更快搭出可交互的 Web 界面，减少从零写组件和状态管理的时间",
        "scenarios": ["快速验证一个产品原型", "给现有站点加交互或新组件"],
    },
    "基础设施 / 云": {
        "audience": "后端、平台工程和运维/自托管爱好者",
        "problem": "降低部署、网络、数据或自托管服务的复杂度，让服务更容易跑在自己的机器上",
        "scenarios": ["自建服务替代一项 SaaS", "把内部服务部署到自己的服务器"],
    },
    "数据 / 分析": {
        "audience": "数据分析师、数据工程师和需要做内部报表的团队",
        "problem": "把分散的数据变成可查询、可分析、可展示的结果，减少手工整理",
        "scenarios": ["搭一条轻量数据管道", "做内部数据看板或分析工具"],
    },
    "安全 / 隐私": {
        "audience": "安全工程师、需要在发布前做检查的研发团队",
        "problem": "更早发现漏洞、权限或隐私风险，把人工审计变成可重复的检查",
        "scenarios": ["上线前做一次安全审计", "排查依赖、二进制或配置风险"],
    },
    "移动端": {
        "audience": "iOS / Android 开发者和做跨端应用的团队",
        "problem": "减少移动端重复搭建、调试和适配的成本",
        "scenarios": ["快速做移动端原型", "给现有 App 加一项系统能力"],
    },
    "学习 / 资源": {
        "audience": "正在系统学习、转行或准备面试的开发者",
        "problem": "把零散的知识和资料组织成一条可执行的学习路径，减少“不知道下一步学什么”",
        "scenarios": ["按路线补基础", "面试前快速复习或查漏补缺"],
    },
    "效率 / 自动化": {
        "audience": "希望把日常重复工作自动化的个人和小团队",
        "problem": "把重复的收集、整理、提醒和操作交给工具，腾出注意力",
        "scenarios": ["自动化日常信息收集", "把个人工作流串成一个本地工具"],
    },
    "创意 / 媒体": {
        "audience": "设计师、内容创作者和需要处理音视频/图片的开发者",
        "problem": "在本地完成素材处理、生成或编辑，减少对在线工具的依赖",
        "scenarios": ["批量处理图片或视频素材", "做一个自己的创作/生成工具"],
    },
    "系统 / 底层": {
        "audience": "系统工程师、编译器/内核/嵌入式方向的学习者",
        "problem": "理解或改造更底层的运行机制，处理性能、资源或平台适配问题",
        "scenarios": ["研究一个运行时/编译器的实现", "给底层工具做定制或排障"],
    },
}

DEFAULT_GUIDE = {
    "audience": "开发者、技术团队和愿意读 README 的技术用户",
    "problem": "把一类重复的技术工作变成可复用的工具或参考实现",
    "scenarios": ["快速验证一个技术想法", "把工具接进自己的开发流程"],
}

TOPIC_SCENARIOS = {
    "agent": "给编码 Agent 增加一项可复用能力",
    "agent-skills": "给编码 Agent 增加一项可复用技能",
    "ai-agents": "搭建或扩展一个 Agent 工作流",
    "mcp": "把外部工具/数据接进支持 MCP 的客户端",
    "llm": "快速验证一个模型应用想法",
    "rag": "给团队文档搭一个可问答的知识库",
    "memory": "给 Agent 增加跨会话的长期记忆",
    "graph": "把代码库/文档变成可查询的知识图谱",
    "binary-analysis": "分析来源不明的二进制或闭源程序",
    "security": "上线前做一次自动化安全检查",
    "audit": "把人工审计变成可重复的检查",
    "image-editing": "在本地批量处理图片素材",
    "photo-editing": "在本地批量处理照片素材",
    "voice": "给视频/播客做配音或语音处理",
    "audio": "在本地处理音频素材",
    "dubbing": "给视频做多语言配音",
    "adblock": "给家庭网络或浏览器加广告过滤",
    "self-hosted": "用自托管替代一项 SaaS 服务",
    "homelab": "把家庭实验室里的服务串起来",
    "cli": "把重复的命令收进一个 CLI",
    "vscode": "把能力嵌进日常编辑器",
    "neovim": "把能力嵌进 Neovim 工作流",
    "pdf": "批量解析或处理 PDF 文档",
    "database": "管理或查询自己的数据",
    "sql": "用 SQL 直接分析手头的数据",
    "scraping": "把网页信息整理成结构化数据",
    "crawler": "批量采集和整理网页内容",
    "game": "做一个小游戏或游戏工具",
    "music": "处理音乐或生成音频",
    "video": "处理视频素材或生成视频",
}

TOPIC_BRIEFS = {
    "binary-analysis": {
        "audience": "安全工程师、逆向工程师，以及需要审计闭源程序的团队",
        "problem": "把二进制/闭源程序的分析过程变快，让 Agent 输出可复核的发现",
    },
    "agent-skills": {
        "audience": "使用 Claude Code / Codex 等编码 Agent 的开发者",
        "problem": "给编码 Agent 补上领域技能，让它按固定流程完成复杂任务",
    },
    "ai-agents": {
        "audience": "正在搭建 Agent 工作流的开发者与小团队",
        "problem": "把多个工具/步骤编排成可重复的 Agent 流程，减少手工串联",
    },
    "image-editing": {
        "audience": "设计师、内容创作者和需要批量处理图片的开发者",
        "problem": "在本地批量完成图片编辑，减少对在线工具和重复手工操作的依赖",
    },
    "voice": {
        "audience": "视频/播客创作者、需要本地语音能力的团队",
        "problem": "在本地完成语音克隆、配音或转写，减少对云端语音服务的依赖",
    },
    "adblock": {
        "audience": "想给家庭网络或浏览器加过滤能力的用户",
        "problem": "在网络入口统一过滤广告和跟踪请求，减少逐台设备配置",
    },
    "security": {
        "audience": "安全工程师和需要在上线前做检查的研发团队",
        "problem": "把安全审计变成可重复的自动检查，更早发现可修复的问题",
    },
}

RESOURCE_TOPICS = {"awesome", "tutorial", "course", "roadmap", "interview", "learning", "cheatsheet", "book"}
CLI_TOPICS = {"cli", "terminal", "tui", "shell"}
EDITOR_TOPICS = {"vscode", "neovim", "editor", "ide", "vim"}
HOST_TOPICS = {"self-hosted", "docker", "homelab", "container", "deploy"}
BROWSER_TOPICS = {"browser", "web", "frontend", "web-app", "website"}
SDK_TOPICS = {"sdk", "api", "library", "framework"}


def _topic_set(item: Dict[str, Any]) -> set:
    return {str(t).lower() for t in (item.get("topics") or [])}


def _pick_usage(item: Dict[str, Any], category: str) -> str:
    topics = _topic_set(item)
    language = (item.get("language") or "").strip()
    if topics & RESOURCE_TOPICS or category == "学习 / 资源":
        return "按 README 或清单阅读、挑选即可，通常不需要安装运行。"
    if topics & {"agent-skills", "ai-agents"}:
        return "把这项技能/配置接入你的编码 Agent，在具体任务里调用；不用单独启动一个应用。"
    if "mcp" in topics:
        return "在支持 MCP 的客户端里配置这个 server，让 Agent 调用它提供的外部工具。"
    if "binary-analysis" in topics:
        return "对目标二进制或程序运行分析，让 Agent/工具输出可读的逆向或审计结果。"
    if topics & {"image-editing", "photo-editing"}:
        return "导入图片素材，在本地完成编辑/批处理，再导出结果。"
    if topics & {"voice", "audio", "dubbing"}:
        return "在本地启动后导入音频或视频，完成克隆、配音、转写等处理。"
    if "graph" in topics:
        return "把代码库、文档或数据导入，生成可查询的知识图谱后按问题检索。"
    if "adblock" in topics:
        return "把它部署到网络入口或浏览器，按规则过滤广告和跟踪请求。"
    if topics & CLI_TOPICS:
        return "安装后通过命令行运行，适合嵌进脚本、CI 或终端工作流。"
    if topics & HOST_TOPICS:
        return "用 Docker 或本地进程自托管，数据和服务都留在自己的机器上。"
    if topics & EDITOR_TOPICS:
        return "作为编辑器插件/扩展安装，在写代码的界面里直接调用。"
    if topics & BROWSER_TOPICS:
        return "启动本地服务后在浏览器打开，也可以部署到自己的站点。"
    if topics & SDK_TOPICS:
        return "作为 SDK / API 接入现有服务，通常不单独运行。"
    if category == "安全 / 隐私":
        return "在发布前或排查时运行，输出审计/扫描结果，再根据结果修复。"
    if category == "AI / 大模型":
        return "按 README 配置模型或 API Key 后，作为 CLI、SDK 或本地服务接入现有流程。"
    if language:
        return "按 README 准备 %s 环境、安装依赖后运行；通常以 CLI 或本地服务方式使用。" % language
    return "按 README 安装依赖后运行；通常以 CLI 或本地服务方式使用。"


def _pick_difficulty(item: Dict[str, Any], category: str) -> str:
    topics = _topic_set(item)
    if topics & RESOURCE_TOPICS or category == "学习 / 资源":
        return "阅读即可"
    if topics & CLI_TOPICS:
        return "需要命令行基础"
    if topics & HOST_TOPICS:
        return "需要 Docker / 部署基础"
    language = (item.get("language") or "").strip()
    if language:
        return "需要 %s 运行环境" % language
    return "按 README 上手"


def _what(item: Dict[str, Any], category: str) -> str:
    text = (item.get("summary_zh") or item.get("description") or "").strip()
    if text:
        return text[:180].rstrip()
    return "一个面向%s的开源项目，具体能力以 README 为准。" % (category or "技术团队")


def _flow_for(item: Dict[str, Any], category: str) -> List[str]:
    topics = _topic_set(item)
    language = (item.get("language") or "").strip()
    if topics & RESOURCE_TOPICS or category == "学习 / 资源":
        return [
            "打开项目 README / 清单，先看目录和适用方向。",
            "按自己的目标筛选 2–3 个条目，不要从头到尾硬啃。",
            "顺着条目里的链接阅读或练习，并记录可复用的部分。",
            "把真正有用的内容加入自己的笔记 / 收藏。",
        ]
    if topics & {"agent-skills", "ai-agents"}:
        return [
            "按 README 把技能或配置安装到支持的 Agent 客户端。",
            "在客户端里启用该技能，准备好任务需要的文件或目标。",
            "在具体任务中调用它，先跑一个小样本检查输出。",
            "把稳定的用法写进团队提示词或工作流。",
        ]
    if "mcp" in topics:
        return [
            "按 README 安装并启动 MCP server。",
            "在支持 MCP 的客户端里填写 server 配置。",
            "让 Agent 调用一个只读工具，确认连接和权限正常。",
            "再逐步放开写入/高风险操作。",
        ]
    if topics & HOST_TOPICS:
        return [
            "准备 Docker / 目标机器和持久化目录。",
            "按 README 配置环境变量、端口和密钥。",
            "启动容器或服务，检查日志和健康状态。",
            "接入自己的数据或客户端，并设置备份/更新策略。",
        ]
    if topics & CLI_TOPICS:
        return [
            "按 README 安装依赖或下载可执行文件。",
            "配置必要的 token / API Key / 数据目录。",
            "先运行 --help 或最小示例，确认环境正常。",
            "把命令接进脚本、CI 或日常终端流程。",
        ]
    if topics & EDITOR_TOPICS:
        return [
            "在编辑器插件市场安装，或按 README 手动加载。",
            "打开插件设置，配置密钥、模型或语言服务。",
            "在一个小文件/小项目里试一次核心功能。",
            "确认快捷键和团队配置后，再推广到主项目。",
        ]
    if topics & BROWSER_TOPICS:
        return [
            "按 README 安装依赖并启动本地服务。",
            "在浏览器打开地址，先完成一次最小配置。",
            "导入自己的数据或连接目标服务。",
            "需要长期使用时，再配置部署、域名和备份。",
        ]
    if topics & SDK_TOPICS:
        return [
            "按 README 安装 SDK / 引入依赖。",
            "获取 API Key 或配置服务端地址。",
            "运行官方最小示例，确认请求和返回结构。",
            "封装进自己的业务代码，并加上错误处理。",
        ]
    if category == "安全 / 隐私":
        return [
            "明确扫描/审计的目标和授权范围。",
            "按 README 安装并配置扫描参数。",
            "先跑只读检查，阅读报告并标注高风险项。",
            "修复后复测，把检查接进发布流程。",
        ]
    if language:
        return [
            "准备 %s 运行环境，按 README 安装依赖。" % language,
            "配置项目需要的密钥、数据或服务地址。",
            "运行最小示例，确认输出符合预期。",
            "再接入自己的数据/流程，并补充日志和错误处理。",
        ]
    return [
        "阅读 README 的安装、配置和示例部分。",
        "准备运行环境与必要凭证。",
        "先跑官方最小示例，确认能正常启动。",
        "再接入自己的数据或工作流，并观察输出。",
    ]


def build(item: Dict[str, Any], categories: List[str]) -> Dict[str, Any]:
    category = categories[0] if categories else ""
    guide = GUIDES.get(category, DEFAULT_GUIDE)
    topics = _topic_set(item)
    audience = guide.get("audience", DEFAULT_GUIDE["audience"])
    problem = guide.get("problem", DEFAULT_GUIDE["problem"])
    for topic in item.get("topics") or []:
        override = TOPIC_BRIEFS.get(str(topic).lower())
        if override:
            audience = override["audience"]
            problem = override["problem"]
            break

    scenarios: List[str] = []
    for topic in item.get("topics") or []:
        scenario = TOPIC_SCENARIOS.get(str(topic).lower())
        if scenario and scenario not in scenarios:
            scenarios.append(scenario)
    for scenario in guide.get("scenarios", []):
        if scenario not in scenarios:
            scenarios.append(scenario)

    return {
        "what": _what(item, category),
        "audience": audience,
        "problem": problem,
        "usage": _pick_usage(item, category),
        "flow": _flow_for(item, category),
        "scenarios": scenarios[:3],
        "difficulty": _pick_difficulty(item, category),
        "category": category or "技术项目",
        "note": "根据项目简介、语言与 topics 自动归纳，具体命令与限制以 README 为准。",
    }
