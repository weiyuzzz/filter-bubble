"""Heuristic ranking: heat, momentum, freshness + personal taste, with diversity."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List, Optional

from . import brief, prefs

LANG_COLORS = {
    "python": "#3572A5", "javascript": "#f1e05a", "typescript": "#3178c6", "rust": "#dea584",
    "go": "#00ADD8", "java": "#b07219", "c": "#555555", "c++": "#f34b7d", "c#": "#178600",
    "ruby": "#701516", "php": "#4F5D95", "swift": "#F05138", "kotlin": "#A97BFF",
    "dart": "#00B4AB", "shell": "#89e051", "html": "#e34c26", "css": "#563d7c",
    "vue": "#41b883", "svelte": "#ff3e00", "zig": "#ec915c", "lua": "#000080",
    "jupyter notebook": "#DA5B0B", "scala": "#c22d40", "haskell": "#5e5086", "elixir": "#6e4a7e",
    "clojure": "#db5855", "ocaml": "#3be133", "nim": "#ffc200", "perl": "#0298c3",
    "r": "#198CE7", "solidity": "#AA6746", "assembly": "#6E4C13", "objective-c": "#438eff",
}


def _pct(values: List[float], v: float) -> float:
    if not values:
        return 0.5
    return sum(1 for x in values if x <= v) / float(len(values))


def parse_dt(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(created_at: Optional[str]) -> Optional[int]:
    parsed = parse_dt(created_at)
    if not parsed:
        return None
    return max(0, (dt.datetime.now(dt.timezone.utc) - parsed).days)


def _badge(item: Dict[str, Any], is_fastest: bool, is_newest: bool, is_sustained: bool) -> str:
    if item.get("daily_rank") == 1:
        return "日榜第一"
    if is_fastest:
        return "增速冠军"
    if is_newest:
        return "新星崛起"
    if item.get("daily_rank") and item["daily_rank"] <= 3:
        return "日榜前三"
    if is_sustained:
        return "持续走热"
    if int(item.get("surge") or 0) >= 78:
        return "上升中"
    return "值得一看"


def _insight(item: Dict[str, Any]) -> str:
    age = item.get("age_days")
    stars = int(item.get("stars") or 0)
    today = int(item.get("stars_today") or 0)
    week = int(item.get("stars_week") or 0)
    if age is not None and age <= 60 and age >= 1:
        return "%d 天新项目 · 日均 +%s star" % (age, format(int(stars / max(age, 1)), ","))
    if week:
        return "本周 +%s star · 持续升温" % format(week, ",")
    if today:
        return "今日 +%s star · 累计 %s" % (format(today, ","), format(stars, ","))
    return "累计 %s star" % format(stars, ",")


def build(candidates: List[Dict[str, Any]], model: Dict[str, float], bias: float,
          ready: bool, feedback: Dict[str, int], bookmarks: set,
          histories: Optional[Dict[str, List[int]]] = None,
          following: Optional[set] = None) -> List[Dict[str, Any]]:
    if not candidates:
        return []

    for c in candidates:
        c["age_days"] = age_days(c.get("created_at"))
        daily = int(c.get("stars_today") or 0)
        weekly = int(c.get("stars_week") or 0)
        age = c.get("age_days")
        if daily:
            c["velocity"] = float(daily)
        elif weekly:
            c["velocity"] = weekly / 7.0
        elif age:
            c["velocity"] = int(c.get("stars") or 0) / max(age, 1)
        else:
            c["velocity"] = 0.0
        c["features"] = prefs.features_for(c)

    velocities = [float(c["velocity"]) for c in candidates]
    star_logs = [math.log1p(int(c.get("stars") or 0)) for c in candidates]
    max_daily = max([int(c.get("stars_today") or 0) for c in candidates] + [1])

    for c in candidates:
        stars = int(c.get("stars") or 0)
        age = c.get("age_days")
        v_pct = _pct(velocities, float(c["velocity"]))
        s_pct = _pct(star_logs, math.log1p(stars))
        freshness = math.exp(-(age if age is not None else 500) / 540.0)
        rank_bonus = 0.0
        if c.get("daily_rank"):
            rank_bonus = max(0.0, 1.0 - (c["daily_rank"] - 1) / 14.0)
        heat = 100.0 * (
            0.52 * v_pct + 0.23 * s_pct + 0.15 * freshness + 0.10 * rank_bonus
        ) + 4.0
        c["heat"] = int(max(30, min(99, round(heat))))
        c["velocity_pct"] = round(v_pct * 100)

        today = int(c.get("stars_today") or 0)
        week = int(c.get("stars_week") or 0)
        if today and stars:
            relative = min(1.0, (today / float(stars)) / 0.08)
        elif week and stars:
            relative = min(1.0, (week / float(stars)) / 0.30)
        elif age:
            relative = min(1.0, (stars / float(max(age, 1))) / 500.0)
        else:
            relative = 0.0
        surge = 100.0 * (0.55 * v_pct + 0.25 * relative + 0.20 * s_pct)
        c["surge"] = int(max(0, min(100, round(surge))))
        c["relative_gain"] = round(relative * 100)
        c["star_per_day"] = int(stars / max(age, 1)) if age else None

    fastest_name = None
    if max_daily > 0:
        daily_candidates = [c for c in candidates if int(c.get("stars_today") or 0) > 0]
        if daily_candidates:
            fastest_name = max(daily_candidates, key=lambda x: int(x["stars_today"]))["full_name"]

    new_names = [c["full_name"] for c in candidates if (c.get("age_days") or 9999) <= 30]
    newest_name = None
    if new_names:
        newest_name = max(
            [c for c in candidates if c["full_name"] in new_names],
            key=lambda x: (int(x.get("stars_today") or 0), int(x.get("stars") or 0)),
        )["full_name"]

    for c in candidates:
        sources = c.get("sources") or []
        sustained = "weekly" in sources and "daily" not in sources
        c["badge"] = _badge(c, c["full_name"] == fastest_name, c["full_name"] == newest_name, sustained)
        c["insight"] = _insight(c)
        c["language_color"] = LANG_COLORS.get((c.get("language") or "").lower(), "#8b949e")
        c["match"] = int(round(prefs.score(c["features"], model, bias) * 100)) if ready else None
        reason, matched = prefs.reason_for(c["features"], model, ready)
        c["reason"] = reason
        c["matched_labels"] = matched
        c["explore"] = bool(ready and not matched)
        match_base = c["match"] if c["match"] is not None else 50
        base = (0.56 * c["heat"] + 0.44 * match_base) if ready else (c["heat"] + match_base * 0.04)
        c["score"] = round(base + (4.0 if c["explore"] else 0.0), 2)
        c["categories"] = [label for _key, label in prefs.categories_for(c)]
        c["brief"] = brief.build(c, c["categories"])
        owner_key = (c.get("owner") or c["full_name"].split("/")[0]).lower()
        c["following"] = owner_key in (following or set())
        c["feedback"] = feedback.get(c["full_name"], 0)
        c["bookmarked"] = c["full_name"] in bookmarks
        c["spark"] = (histories or {}).get(c["full_name"], [])

    # Sort, then gently diversify owners and dominant categories.
    ordered = sorted(candidates, key=lambda x: (-x["score"], -x["heat"]))
    picked: List[Dict[str, Any]] = []
    owner_count: Dict[str, int] = {}
    cat_count: Dict[str, int] = {}
    for c in ordered:
        owner = c.get("owner") or c["full_name"].split("/")[0]
        primary_cat = c["categories"][0] if c["categories"] else "other"
        if owner_count.get(owner, 0) >= 2 or cat_count.get(primary_cat, 0) >= 10:
            continue
        picked.append(c)
        owner_count[owner] = owner_count.get(owner, 0) + 1
        cat_count[primary_cat] = cat_count.get(primary_cat, 0) + 1
    if len(picked) < len(ordered):
        chosen = {c["full_name"] for c in picked}
        for c in ordered:
            if c["full_name"] not in chosen:
                picked.append(c)
    return picked


def stats(items: List[Dict[str, Any]], feedback_profile: Dict[str, Any]) -> Dict[str, Any]:
    total_today = sum(int(i.get("stars_today") or 0) for i in items)
    total_week = sum(int(i.get("stars_week") or 0) for i in items)
    hottest = max(items, key=lambda x: x["heat"], default=None)
    fastest = max(items, key=lambda x: int(x.get("stars_today") or 0), default=None)
    new_count = sum(1 for i in items if (i.get("age_days") or 9999) <= 30)
    return {
        "total": len(items),
        "stars_today": total_today,
        "stars_week": total_week,
        "new_count": new_count,
        "avg_heat": int(round(sum(i["heat"] for i in items) / max(len(items), 1))),
        "hottest": {
            "full_name": hottest["full_name"], "heat": hottest["heat"], "url": hottest["url"]
        } if hottest else None,
        "fastest": {
            "full_name": fastest["full_name"], "stars_today": int(fastest.get("stars_today") or 0),
            "url": fastest["url"],
        } if fastest else None,
        "feedback_count": feedback_profile.get("samples", 0),
        "taste_ready": feedback_profile.get("ready", False),
    }
