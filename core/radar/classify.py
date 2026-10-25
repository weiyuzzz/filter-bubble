"""事件主题分类（离线规则，可解释）。

九个主题：``conflict`` / ``unrest`` / ``politics`` / ``security`` / ``cyber`` /
``disaster`` / ``infrastructure`` / ``technology`` / ``general_news``。

设计要点：
* **不覆盖** 既有 ``category``（那是"命中用户哪个兴趣主题"），主题写入独立的 ``theme`` 列；
* 每个结果都带 ``evidence``（命中的词），可回溯、可复核；
* **刻意保守**：体育比赛里的"冲突"、政治人物的"攻击性言论"、商业"制裁"等
  不得被判成真实地缘冲突；歧义大就落 ``general_news``；
* 规则与测试都在仓库里，改动可审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

CONFLICT = "conflict"
UNREST = "unrest"
POLITICS = "politics"
SECURITY = "security"
CYBER = "cyber"
DISASTER = "disaster"
INFRASTRUCTURE = "infrastructure"
TECHNOLOGY = "technology"
GENERAL = "general_news"
UNKNOWN = "unknown"

THEMES: List[str] = [CONFLICT, UNREST, POLITICS, SECURITY, CYBER, DISASTER,
                     INFRASTRUCTURE, TECHNOLOGY, GENERAL]

LABELS: Dict[str, str] = {
    CONFLICT: "武装冲突", UNREST: "社会动荡", POLITICS: "政治外交", SECURITY: "公共安全",
    CYBER: "网络安全", DISASTER: "自然灾害", INFRASTRUCTURE: "基础设施",
    TECHNOLOGY: "科技动态", GENERAL: "综合新闻", UNKNOWN: "未分类",
}

# 命中这些上下文就**取消**冲突/安全判定：体育、娱乐、商业口水战最容易误判
SPORTS_HINTS = (
    "比赛", "球队", "球员", "联赛", "夺冠", "赛季", "世界杯", "奥运", "足球", "篮球",
    "网球", "马拉松", "决赛", "半决赛", "进球", "转会", "南美解放者杯", "nba", "cba",
    "世预赛", "亚洲杯", "欧洲杯", "拳击", "格斗", "电竞", "季后赛",
)
ENTERTAINMENT_HINTS = (
    "演员", "歌手", "综艺", "电视剧", "电影票房", "演唱会", "粉丝", "明星", "剧组",
    "选秀", "脱口秀", "网红", "直播带货",
)
RHETORIC_HINTS = (
    "言论", "表态", "抨击", "炮轰", "指责", "回应称", "喊话", "口water", "口水战",
    "批评", "声称", "警告称", "威胁称", "放话", "呛声",
)

RULES: List[Dict[str, Any]] = [
    {"theme": CONFLICT, "words": (
        "战争", "开战", "交战", "武装冲突", "军事冲突", "空袭", "空战", "导弹袭击",
        "火箭弹", "炮击", "轰炸", "入侵", "停火", "战线", "前线", "军事行动", "部队",
        "装甲", "无人机袭击", "导弹", "作战", "阵亡", "战场", "叛军", "武装分子",
        "袭击", "武装", "胡塞", "炸弹", "交火", "军演",
        "war", "invasion", "airstrike", "missile", "ceasefire", "offensive",
        "troops", "military operation", "artillery", "frontline", "militant",
    )},
    {"theme": UNREST, "words": (
        "抗议", "示威", "游行", "骚乱", "罢工", "暴动", "静坐", "示威者", "镇压",
        "罢工潮", "封路", "冲击议会", "宵禁", "动乱", "冲突升级", "暴乱", "抗争",
        "protest", "riot", "demonstration", "strike", "unrest", "crackdown",
    )},
    {"theme": POLITICS, "words": (
        "选举", "大选", "议会", "制裁", "外交", "峰会", "条约", "谈判", "总统",
        "首相", "内阁", "决议", "联合国", "关税", "外交部", "访问", "会晤", "公报",
        "法案", "投票", "执政党", "反对党", "任命", "辞职", "弹劾", "双边关系",
        # 中国党政人事与外交例行表达（语料里出现极多）
        "省委", "市委", "常委", "书记", "当选", "出任", "任职", "被查", "双开",
        "免职", "卸任", "组织部", "党中央", "国务院", "人大", "政协", "贺信",
        "建交", "答记者问", "记者问", "驻军", "撤离", "绿卡", "民调", "候选",
        "国债", "评级", "查处", "边境", "移民", "社会主义", "民主",
        "特朗普", "拜登", "普京", "马克龙", "泽连斯基",
        "election", "sanction", "diplomatic", "summit", "treaty", "parliament",
        "tariff", "veto", "cabinet", "tensions", "border",
    )},
    {"theme": SECURITY, "words": (
        "恐怖袭击", "枪击", "爆炸", "暗杀", "绑架", "人质", "枪手", "持刀",
        "爆炸案", "袭击事件", "自杀式", "劫持", "踩踏", "凶案", "谋杀", "缉毒",
        "火灾", "间谍", "国际刑警", "刑警", "纵火",
        "terror", "shooting", "bombing", "assassination", "hostage", "kidnap",
        "interpol", "espionage",
    )},
    {"theme": CYBER, "words": (
        "网络攻击", "勒索软件", "勒索病毒", "数据泄露", "漏洞", "黑客", "木马",
        "钓鱼邮件", "零日", "僵尸网络", "入侵系统", "漏洞利用", "供应链攻击",
        "ransomware", "malware", "breach", "zero-day", "exploit", "phishing",
        "cyberattack", "cve-", "ddos",
    )},
    {"theme": DISASTER, "words": (
        "地震", "余震", "震级", "台风", "飓风", "洪水", "暴雨", "山洪", "山火",
        "野火", "火山", "海啸", "干旱", "滑坡", "泥石流", "灾害", "极端高温",
        "风暴", "龙卷风", "冰雹", "雪灾", "溃坝",
        # 震后救援类报道常不出现"地震"二字，但依然是灾害事件
        "震区", "震中", "震后", "抗震", "灾区", "救援", "应急物资", "灾情",
        "烟霾", "雾霾", "空气污染",
        "earthquake", "typhoon", "hurricane", "flood", "wildfire", "volcano",
        "tsunami", "drought", "landslide", "heatwave",
    )},
    {"theme": INFRASTRUCTURE, "words": (
        "桥梁坍塌", "坍塌事故", "管道爆炸", "电网故障", "停电", "铁路事故",
        "机场瘫痪", "大坝", "泄漏事故", "炼油厂", "天然气管道", "通信中断",
        "数据中心故障", "供水中断", "隧道", "塌方", "交通事故", "坠机", "沉船",
        "outage", "pipeline", "derailment", "blackout", "collapse",
    )},
    {"theme": TECHNOLOGY, "words": (
        "ai", "人工智能", "大模型", "模型", "算法", "芯片", "半导体", "光刻",
        "制程", "晶圆", "机器人", "量子", "航天", "卫星", "火箭", "发射",
        "操作系统", "软件", "开源", "5g", "6g", "算力", "自动驾驶", "新能源",
        "电池", "半导体设备", "数据中心", "云计算", "英伟达", "nvidia", "台积电",
        "deepseek", "openai", "chatgpt", "gpu", "llm", "semiconductor", "quantum",
        "satellite", "rocket", "robotics",
        # 消费电子与常见科技公司（语料里高频）
        "微软", "苹果", "谷歌", "亚马逊", "特斯拉", "支付宝", "iphone", "ipad",
        "鸿蒙", "安卓", "手机", "手表", "平板", "耳机", "存储", "dram",
        "microsoft", "apple", "google", "amazon", "tesla", "spacex", "meta",
        "robot", "chip",
    )},
]

# 只在"确实没有更强主题信号"时才生效的兜底词
WEAK_CONTEXT = ("国际新闻", "关注", "回应")


def _text(title: str, keywords: Optional[Sequence[str]] = None) -> str:
    parts = [title or ""]
    if keywords:
        parts.extend(str(k) for k in keywords)
    return " ".join(parts).lower()


def _hits(text: str, words: Sequence[str]) -> List[str]:
    return [w for w in words if w and w in text]


def classify(title: str, keywords: Optional[Sequence[str]] = None,
             body: str = "") -> Dict[str, Any]:
    """返回 ``{theme, label, evidence, ambiguous, suppressed_by}``。"""
    text = _text(title, keywords) + (" " + (body or "").lower() if body else "")
    sports = _hits(text, SPORTS_HINTS)
    entertainment = _hits(text, ENTERTAINMENT_HINTS)
    rhetoric = _hits(text, RHETORIC_HINTS)

    scored: List[Dict[str, Any]] = []
    for rule in RULES:
        matched = _hits(text, rule["words"])
        if matched:
            scored.append({"theme": rule["theme"], "hits": matched, "score": len(matched)})

    if not scored:
        return {"theme": GENERAL, "label": LABELS[GENERAL], "evidence": [],
                "ambiguous": False, "suppressed_by": []}

    # 体育/娱乐语境下取消 conflict / security：这类"冲突""袭击"几乎都是比喻
    suppressed: List[str] = []
    if sports or entertainment:
        keep = [row for row in scored if row["theme"] not in (CONFLICT, SECURITY)]
        if len(keep) != len(scored):
            suppressed = [row["theme"] for row in scored
                          if row["theme"] in (CONFLICT, SECURITY)]
            scored = keep
    # "攻击性言论/抨击/制裁威胁"这类措辞取消 conflict
    if rhetoric and not sports:
        keep = [row for row in scored if row["theme"] != CONFLICT]
        if len(keep) != len(scored):
            suppressed = sorted(set(suppressed) | {CONFLICT})
            scored = keep

    if not scored:
        return {"theme": GENERAL, "label": LABELS[GENERAL], "evidence": [],
                "ambiguous": bool(suppressed), "suppressed_by": sorted(set(suppressed))}

    priority = {theme: index for index, theme in enumerate(THEMES)}
    scored.sort(key=lambda row: (-row["score"], priority.get(row["theme"], 99)))
    best = scored[0]
    tied = [row for row in scored if row["score"] == best["score"]]
    return {
        "theme": best["theme"],
        "label": LABELS[best["theme"]],
        "evidence": best["hits"][:6],
        "ambiguous": len(tied) > 1,
        "suppressed_by": sorted(set(suppressed)),
    }


def classify_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """事件级封装：标题 + keywords（keywords 是聚类产出的关键词，可提供额外信号）。"""
    keywords = event.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    return classify(str(event.get("title") or ""), keywords)


def metadata() -> Dict[str, Any]:
    return {
        "themes": THEMES,
        "labels": LABELS,
        "rule_count": sum(len(r["words"]) for r in RULES),
        "suppression": {"sports": list(SPORTS_HINTS)[:8], "entertainment": list(ENTERTAINMENT_HINTS)[:6],
                        "rhetoric": list(RHETORIC_HINTS)[:8]},
        "note": "歧义或仅命中体育/娱乐语境时落 general_news；不覆盖既有 category。",
    }