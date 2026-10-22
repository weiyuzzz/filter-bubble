"""Digest generation: collect → enrich → translate → rank → persist."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import config, db, follows, github, notify, prefs, rank
from .translate import Translator

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(config.TIMEZONE)
except Exception:  # noqa: BLE001
    TZ = dt.datetime.now().astimezone().tzinfo

_STATE_LOCK = threading.Lock()
STATE: Dict[str, Any] = {
    "running": False,
    "stage": "",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}
_thread: Optional[threading.Thread] = None


def local_today() -> dt.date:
    return dt.datetime.now(TZ).date()


def local_now() -> dt.datetime:
    return dt.datetime.now(TZ)


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(STATE)


def _set_stage(message: str) -> None:
    with _STATE_LOCK:
        STATE["stage"] = message


def _merge(groups: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    rich_fields = ("topics", "created_at", "pushed_at", "homepage", "license", "avatar")
    for group in groups:
        for raw in group:
            item = dict(raw)
            key = item["full_name"]
            if key not in merged:
                item["sources"] = list(dict.fromkeys(item.get("sources") or []))
                merged[key] = item
                continue
            dst = merged[key]
            for src in item.get("sources") or []:
                if src not in dst["sources"]:
                    dst["sources"].append(src)
            for field in ("stars_today", "stars_week", "daily_rank", "weekly_rank"):
                if item.get(field) and not dst.get(field):
                    dst[field] = item[field]
            if item.get("daily_rank") and dst.get("daily_rank"):
                dst["daily_rank"] = min(dst["daily_rank"], item["daily_rank"])
            if item.get("weekly_rank") and dst.get("weekly_rank"):
                dst["weekly_rank"] = min(dst["weekly_rank"], item["weekly_rank"])
            for field in rich_fields:
                if item.get(field) and not dst.get(field):
                    dst[field] = item[field]
            if len(item.get("description") or "") > len(dst.get("description") or ""):
                dst["description"] = item["description"]
            dst["stars"] = max(int(dst.get("stars") or 0), int(item.get("stars") or 0))
            dst["forks"] = max(int(dst.get("forks") or 0), int(item.get("forks") or 0))
    return list(merged.values())


def _pre_score(item: Dict[str, Any]) -> float:
    today = int(item.get("stars_today") or 0)
    week = int(item.get("stars_week") or 0)
    stars = int(item.get("stars") or 0)
    age = rank.age_days(item.get("created_at")) or 365
    velocity = today or (week / 7.0) or (stars / max(age, 1))
    bonus = 1.4 if item.get("daily_rank") else 0
    return velocity * 2.0 + stars ** 0.5 * 3.0 + bonus


def _histories(conn, names: List[str]) -> Dict[str, List[int]]:
    if not names:
        return {}
    since = (local_today() - dt.timedelta(days=14)).isoformat()
    marks = ",".join("?" for _ in names)
    rows = conn.execute(
        "SELECT full_name, stars FROM snapshots WHERE date>=? AND full_name IN (%s) ORDER BY date ASC" % marks,
        [since] + names,
    ).fetchall()
    out: Dict[str, List[int]] = {}
    for row in rows:
        out.setdefault(row["full_name"], []).append(int(row["stars"] or 0))
    return out


def _readme_block(conn, items: List[Dict[str, Any]], translator: Translator) -> None:
    deadline = time.time() + 30
    for item in items:
        row = conn.execute("SELECT * FROM readmes WHERE full_name=?", (item["full_name"],)).fetchone()
        excerpt, zh = "", None
        if row:
            try:
                stamped = dt.datetime.fromisoformat(row["created_at"])
                fresh = (dt.datetime.now().astimezone() - stamped).days < 7
            except (ValueError, TypeError):
                fresh = False
            if fresh:
                excerpt = row["excerpt"]
        if not excerpt and time.time() < deadline:
            markdown = github.fetch_readme(item["full_name"])
            excerpt = github.readme_excerpt(markdown)
            if excerpt:
                conn.execute(
                    "INSERT OR REPLACE INTO readmes(full_name, excerpt, created_at) VALUES(?,?,?)",
                    (item["full_name"], excerpt, db.now_iso()),
                )
                conn.commit()
        if not excerpt:
            continue
        item["readme_en"] = excerpt
        if translator.remaining > 320:
            zh = translator.translate(excerpt)
        if zh:
            item["readme_zh"] = zh


def _final_items(ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cap to MAX_ITEMS while keeping every daily-chart repo on the page."""
    items = ranked[: config.MAX_ITEMS]
    item_names = {c["full_name"] for c in items}
    missing_daily = [
        c for c in ranked
        if "daily" in (c.get("sources") or []) and c["full_name"] not in item_names
    ]
    for candidate in missing_daily:
        for idx in range(len(items) - 1, -1, -1):
            if "daily" not in (items[idx].get("sources") or []):
                items[idx] = candidate
                break
    items.sort(key=lambda x: (-x["score"], -x["heat"]))
    return items


def generate(progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    said = progress or (lambda _m: None)
    conn = db.connect()
    github.RATE_REMAINING = None
    try:
        said("拉取 GitHub Trending 日榜与周榜")
        daily = github.fetch_trending("daily")
        weekly = github.fetch_trending("weekly")
        fallback = not daily and not weekly

        said("搜索最近 7 天与 30 天的新项目")
        fresh7 = github.search_recent(7, 25)
        fresh30 = github.search_recent(30, 30)
        if fallback and not (fresh7 or fresh30):
            raise RuntimeError("GitHub Trending 与 Search 均暂不可达，已保留上一份日报")

        candidates = _merge([daily, weekly, fresh7, fresh30])
        for item in candidates:
            item["age_days"] = rank.age_days(item.get("created_at"))
            item["sources"] = item.get("sources") or []

        said("读取项目详情")
        trending = [c for c in candidates if any(s in ("daily", "weekly") for s in c["sources"])]
        trending.sort(key=lambda x: (x.get("daily_rank") or 99, -(x.get("stars_week") or 0)))
        deadline = time.time() + 100
        for item in trending[:26]:
            if time.time() > deadline or github.RATE_REMAINING == 0:
                break
            enriched = github.enrich(item, conn)
            item.update({k: v for k, v in enriched.items() if v not in (None, "", [], 0)})
            if "daily" in item["sources"]:
                item["daily_rank"] = next(
                    (d["daily_rank"] for d in daily if d["full_name"] == item["full_name"]), None
                )
            if "weekly" in item["sources"]:
                item["weekly_rank"] = next(
                    (d["weekly_rank"] for d in weekly if d["full_name"] == item["full_name"]), None
                )
            item["age_days"] = rank.age_days(item.get("created_at"))

        # Ensure all daily trending repos are in the running, then add fresh blood.
        daily_names = {d["full_name"] for d in daily}
        weekly_only = [c for c in candidates if "weekly" in c["sources"] and "daily" not in c["sources"]]
        weekly_only.sort(key=lambda x: int(x.get("stars_week") or 0), reverse=True)
        fresh = [c for c in candidates if "daily" not in c["sources"] and "weekly" not in c["sources"]]
        fresh.sort(key=lambda x: _pre_score(x), reverse=True)
        seen = set()
        selected: List[Dict[str, Any]] = []
        for pool, limit in (([c for c in candidates if c["full_name"] in daily_names], 30),
                            (weekly_only, 9), (fresh, 12)):
            added = 0
            for item in pool:
                if added >= limit:
                    break
                if item["full_name"] in seen:
                    continue
                seen.add(item["full_name"])
                selected.append(item)
                added += 1
        if len(selected) < 24:
            rest = sorted([c for c in candidates if c["full_name"] not in seen], key=_pre_score, reverse=True)
            selected.extend(rest[: 24 - len(selected)])

        said("翻译项目简介")
        translator = Translator(conn)
        by_heat = sorted(selected, key=_pre_score, reverse=True)
        for item in by_heat[:30]:
            if translator.remaining < 40:
                break
            if item.get("description"):
                zh = translator.translate(item["description"])
                if zh:
                    item["summary_zh"] = zh

        said("读取 README 摘要")
        _readme_block(conn, by_heat[:4], translator)

        today = local_today().isoformat()
        for item in candidates:
            db.save_snapshot(conn, item, today)

        weights, bias = prefs.load_model(conn)
        profile = prefs.profile(conn)
        names = [c["full_name"] for c in selected]
        following = {o.lower() for o in follows.owners(conn)}
        ranked = rank.build(
            selected, weights, bias, profile["ready"], db.feedback_map(conn),
            db.bookmarks(conn), _histories(conn, names), following=following
        )
        items = _final_items(ranked)

        for idx, item in enumerate(items, 1):
            item["rank"] = idx

        source_counts = {
            "fallback": fallback,
            "daily": len(daily),
            "weekly": len(weekly),
            "fresh": len({c["full_name"] for c in fresh7 + fresh30}),
            "total_pool": len(candidates),
        }
        payload = {
            "date": today,
            "generated_at": db.now_iso(),
            "timezone": config.TIMEZONE,
            "stats": rank.stats(items, profile),
            "profile": profile,
            "source_counts": source_counts,
            "items": items,
            "notes": [
                "数据源：GitHub Trending 日榜 / 周榜 + GitHub Search（近 7 / 30 天新项目）"
                + ("｜Trending 暂不可达，本期由 Search 兜底" if fallback else ""),
                "热度 = 增速 55% + 项目体量 25% + 新鲜度 20%，每日快照会让「猛增」判断越来越准",
            ],
        }
        conn.execute(
            "INSERT OR REPLACE INTO digests(date, payload, generated_at) VALUES(?,?,?)",
            (today, json.dumps(payload, ensure_ascii=False), payload["generated_at"]),
        )
        db.set_meta(conn, "last_generated_date", today)
        db.set_meta(conn, "last_refresh_failed_at", "")
        db.set_meta(conn, "profile_updated", db.now_iso())
        conn.commit()
        said("更新关注发布者的动态")
        try:
            follows.refresh(conn, max_seconds=15)
        except Exception as exc:  # noqa: BLE001
            print("[digest] follow refresh failed:", exc)
        if config.PUSH_ENABLED:
            said("推送微信简报")
            try:
                result = notify.send_daily(payload, conn=conn)
                if not result.get("ok"):
                    print("[push] failed:", result.get("reason") or result.get("detail"))
            except Exception as exc:  # noqa: BLE001
                print("[push] error:", exc)
        return payload
    finally:
        conn.close()


def _run(progress=None) -> None:
    with _STATE_LOCK:
        if STATE["running"]:
            return
        STATE.update({"running": True, "stage": "开始", "started_at": db.now_iso(), "last_error": None})
    try:
        generate(progress=_set_stage)
        with _STATE_LOCK:
            STATE["finished_at"] = db.now_iso()
            STATE["stage"] = "完成"
    except Exception as exc:  # noqa: BLE001
        print("[digest] generation failed:", exc)
        try:
            failed_conn = db.connect()
            try:
                db.set_meta(failed_conn, "last_refresh_failed_at", db.now_iso())
                failed_conn.commit()
            finally:
                failed_conn.close()
        except Exception:  # noqa: BLE001
            pass
        with _STATE_LOCK:
            STATE["last_error"] = str(exc)
            STATE["stage"] = "失败"
    finally:
        with _STATE_LOCK:
            STATE["running"] = False


def refresh_async() -> bool:
    global _thread
    with _STATE_LOCK:
        if STATE["running"]:
            return False
        if _thread and _thread.is_alive():
            return False
    _thread = threading.Thread(target=_run, name="gitpulse-refresh", daemon=True)
    _thread.start()
    return True


def latest(conn) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        return None
    payload = json.loads(row["payload"])
    payload["_stored_date"] = row["date"]
    followed = {o.lower() for o in follows.owners(conn)}
    for item in payload.get("items", []):
        owner = (item.get("owner") or item["full_name"].split("/")[0]).lower()
        item["following"] = owner in followed
    return payload


def _save(conn, payload: Dict[str, Any]) -> None:
    payload.pop("_stored_date", None)
    conn.execute(
        "INSERT OR REPLACE INTO digests(date, payload, generated_at) VALUES(?,?,?)",
        (payload["date"], json.dumps(payload, ensure_ascii=False), payload["generated_at"]),
    )
    conn.commit()


def rerank(conn) -> Dict[str, Any]:
    payload = latest(conn)
    if not payload:
        raise RuntimeError("还没有日报")
    profile = prefs.profile(conn)
    weights, bias = prefs.load_model(conn)
    names = [i["full_name"] for i in payload["items"]]
    following = {o.lower() for o in follows.owners(conn)}
    items = _final_items(rank.build(
        payload["items"], weights, bias, profile["ready"], db.feedback_map(conn),
        db.bookmarks(conn), _histories(conn, names), following=following
    ))
    for idx, item in enumerate(items, 1):
        item["rank"] = idx
    payload["items"] = items
    payload["profile"] = profile
    payload["stats"] = rank.stats(items, profile)
    _save(conn, payload)
    return payload
