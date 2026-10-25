"""全球态势监控：只读聚合层（阶段一 · 基础后端）。

本模块的硬约束（对应 docs/global-intel-design.md 与阶段一修正要求）：

* **只读**：只读既有 ``radar_*`` 表，不采集、不联网、不调用任何大模型；
* **不覆盖 category**：``category`` 继续表示"命中用户哪个兴趣主题"，语义不变；
  事件类型与地域走独立的 ``theme`` / ``region`` 字段（本阶段只预留，不启用）；
* **两个指标分开**：``activity``（威胁活动，来自可观测事件、固定口径强度与实际变化）
  与 ``credibility``（情报可信度，来自未验证比例/来源集中/缺少证据）各自独立，
  不合成一个"危险程度"。未经验证的新闻数量不得被解释为真实危险程度；
* **历史只用真实快照**：历史日期一律读 ``radar_threat_daily`` 已物化的真实输入，
  或用当时写入的 ``radar_event_snapshots`` 反推（口径 0）；
  绝不用"当前的 raw_heat"回填历史，也绝不把按日重标定的 ``heat`` 当作跨日可比强度；
* **缺什么就说什么**：任何输入缺失都返回 ``missing_inputs``，不插值、不填 0 冒充真实值；
* **不编造国家分数**：国家风险本阶段明确 ``available=false``。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import seen as seen_store
from . import config as radar_config
from . import store

# ---------- 口径版本 ----------
# 1 = 本次开始记录的固定口径（全部输入均为当次真实值）
# 0 = 由 radar_event_snapshots 反推的历史点（缺少绝对强度与全部可信度输入）
CALIBER_CURRENT = 1
CALIBER_LEGACY = 0

CALIBER_NOTE = (
    "口径版本 1：由当次真实输入（绝对事件强度 raw_heat、事件/条目量、平台广度、"
    "与前一日快照对比的实际增长、置信度分布、来源集中标记、缺少媒体报道数）计算。"
    "口径版本 0：由当时写入的 radar_event_snapshots 反推，只有条目量与平台广度，"
    "缺少绝对强度与可信度输入，因此不与版本 1 直接比较。"
)

SEMANTICS = [
    "威胁活动（activity）只描述公开信息中可观测到的活动强度及其变化，"
    "不等于现实世界的冲突、战争或危险程度。",
    "情报可信度（credibility）单独计算：未经多方验证的比例、来源集中、缺少媒体报道。",
    "未经验证的新闻条数不得被解读为真实危险程度。",
    "数据源停用、API 额度不足、国际新闻覆盖偏弱属于监控盲区，"
    "计入 monitoring 维度，不并入威胁活动。",
]

DEFAULT_ACTIVITY_WEIGHTS = {"intensity": 0.40, "volume": 0.25, "breadth": 0.20, "growth": 0.15}
DEFAULT_CREDIBILITY_WEIGHTS = {"verified_share": 0.45, "concentration_free": 0.25,
                               "evidence_ok": 0.30}
DEFAULT_REFS = {"event_ref": 200, "item_ref": 400, "platform_ref": 12, "evidence_gap_ref": 5}
DEFAULT_STRATEGIC_WEIGHTS = {"observed_activity": 0.55, "evidence_uncertainty": 0.25,
                            "monitoring_blind_spot": 0.20}

ESTIMATE_LABEL = "系统自评估算（非官方指数，含监控盲区，不可解释为现实冲突风险）"


# ---------- 小工具 ----------

def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _intel_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = cfg or radar_config.cached()
    return cfg.get("intel") or {}


def _refs(cfg: Dict[str, Any]) -> Dict[str, float]:
    refs = dict(DEFAULT_REFS)
    refs.update({k: v for k, v in (_intel_cfg(cfg).get("activity_refs") or {}).items()
                 if v is not None})
    out = {}
    for key, value in refs.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(DEFAULT_REFS[key])
        out[key] = number if number > 0 else float(DEFAULT_REFS[key])
    return out


def _weights(cfg: Dict[str, Any], key: str, defaults: Dict[str, float]) -> Dict[str, float]:
    raw = _intel_cfg(cfg).get(key) or {}
    out = dict(defaults)
    for name, value in raw.items():
        if name in defaults:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            out[name] = max(0.0, number)
    if sum(out.values()) <= 0:
        return dict(defaults)
    return out


def today(cfg: Optional[Dict[str, Any]] = None) -> str:
    """沿用雷达自己的时区口径（Asia/Shanghai）。"""
    try:
        from . import service
        return service.today()
    except Exception:  # noqa: BLE001 - 只是取日期，绝不因此中断
        return dt.date.today().isoformat()


def _now_iso() -> str:
    try:
        from . import service
        return service.now_iso()
    except Exception:  # noqa: BLE001
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _date_range(days: int, end_date: str) -> List[str]:
    days = max(1, min(90, int(days or 7)))
    try:
        end = dt.date.fromisoformat(end_date)
    except ValueError:
        end = dt.date.today()
    return [(end - dt.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _json_load(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback


def _weighted(parts: Dict[str, Optional[float]], weights: Dict[str, float]) -> Dict[str, Any]:
    """按权重把分项合成 0–100。

    值为 ``None`` 的分项表示"这个输入拿不到"：它不参与求和，也不被当作 0，
    权重只在可用分项之间重新归一化；缺失项原样记入 ``missing``。
    """
    used = {name: _clamp01(value) for name, value in parts.items() if value is not None}
    missing = sorted(name for name, value in parts.items() if value is None)
    total = sum(weights.get(name, 0.0) for name in used)
    if not used or total <= 0:
        return {"value": None, "components": {}, "weights": {}, "missing": missing,
                "renormalized": False}
    raw = 100.0 * sum(weights.get(name, 0.0) * value for name, value in used.items()) / total
    return {
        "value": round(_clamp01(raw / 100.0) * 100.0, 1),
        "components": {name: round(value, 4) for name, value in used.items()},
        "weights": {name: weights.get(name) for name in used},
        "missing": missing,
        "renormalized": bool(missing),
    }


# ---------- 每日快照：记录与读取 ----------

def _previous_snapshot_date(conn, date: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) d FROM radar_event_snapshots WHERE date < ?", (date,)).fetchone()
    return (row["d"] if row else None) or None


def _platform_universe(items_rows: List[Any]) -> Tuple[int, int]:
    platforms = set()
    sources = set()
    for row in items_rows:
        if row["platform"]:
            platforms.add(row["platform"])
        if row["source_id"]:
            sources.add(row["source_id"])
    return len(platforms), len(sources)


def record_daily_snapshot(conn, date: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按固定口径（版本 1）算出 ``date`` 的态势快照并 upsert。

    只做本地计算，**不联网、不等待外部请求**。调用方负责提交事务
    （雷达流水线把它放在取报的同一个事务里，避免第二条写入路径）。
    """
    cfg = cfg or radar_config.cached()
    refs = _refs(cfg)
    activity_weights = _weights(cfg, "activity_weights", DEFAULT_ACTIVITY_WEIGHTS)
    credibility_weights = _weights(cfg, "credibility_weights", DEFAULT_CREDIBILITY_WEIGHTS)

    events = store.events_for_date(conn, date, limit=2000)
    story_row = conn.execute(
        "SELECT COUNT(1) c FROM radar_stories WHERE date=?", (date,)).fetchone()
    story_count = int((story_row["c"] if story_row else 0) or 0)

    prev_date = _previous_snapshot_date(conn, date)
    prev_counts: Dict[str, int] = {}
    if prev_date:
        for row in conn.execute(
                "SELECT event_id, item_count FROM radar_event_snapshots WHERE date=?", (prev_date,)):
            prev_counts[row["event_id"]] = int(row["item_count"] or 0)

    row = {
        "date": date,
        "caliber_version": CALIBER_CURRENT,
        "activity": 0.0,
        "credibility": None,
        "activity_components": {},
        "credibility_components": {},
        "event_count": len(events),
        "story_count": story_count,
        "item_count": 0,
        "platform_count": 0,
        "source_count": 0,
        "unverified_share": None,
        "concentration_share": None,
        "evidence_gap_count": None,
        "continuing_event_count": 0,
        "grew_event_count": 0,
        "top_activity_heat_mean": None,
        "inputs_available": [],
        "missing_inputs": [],
        "computed_at": _now_iso(),
    }

    if not events:
        row["missing_inputs"] = ["events"]
        store.save_threat_daily(conn, row)
        return row

    heats: List[float] = []
    item_total = 0
    platform_set = set()
    unverified = 0
    concentrated = 0
    evidence_gap = 0
    continuing = 0
    grew = 0
    metrics_missing = 0

    for event in events:
        metrics = event.get("metrics")
        if isinstance(metrics, str):
            metrics = _json_load(metrics, {})
        if not isinstance(metrics, dict):
            metrics = {}
        if not metrics:
            metrics_missing += 1

        item_total += int(event.get("item_count") or 0)
        for platform in (metrics.get("platforms") or []):
            if platform:
                platform_set.add(platform)

        raw_heat = metrics.get("raw_heat")
        # 只用绝对强度 raw_heat。event.heat 是按当日最强事件重标定过的相对值，
        # 拿它当强度会污染跨日比较——所以这里刻意不做回退，缺失就记为缺失。
        try:
            heats.append(float(raw_heat))
        except (TypeError, ValueError):
            pass

        if str(event.get("confidence") or "") == "unverified":
            unverified += 1
        if metrics.get("flags"):
            concentrated += 1
        if int(metrics.get("news_domains") or 0) == 0:
            evidence_gap += 1

        event_id = event.get("id") or ""
        previous = prev_counts.get(event_id)
        if previous:
            continuing += 1
            if int(event.get("item_count") or 0) > previous:
                grew += 1

    heats.sort(reverse=True)
    top_heats = heats[:5]
    top_mean = (sum(top_heats) / float(len(top_heats))) if top_heats else None

    item_rows = conn.execute(
        "SELECT DISTINCT i.platform, i.source_id FROM radar_items i "
        "JOIN radar_events e ON i.event_id = e.id WHERE e.date = ?", (date,)).fetchall()
    platform_count, source_count = _platform_universe(item_rows)
    if not platform_count:
        platform_count = len(platform_set)

    activity_parts = {
        "intensity": (top_mean / 100.0) if top_mean is not None else None,
        "volume": min(1.0, item_total / refs["item_ref"]),
        "breadth": min(1.0, platform_count / refs["platform_ref"]),
        "growth": (grew / float(continuing)) if continuing else None,
    }
    activity = _weighted(activity_parts, activity_weights)

    verified_share = 1.0 - (unverified / float(len(events)))
    concentration_free = 1.0 - (concentrated / float(len(events)))
    evidence_ok = 1.0 - min(1.0, evidence_gap / refs["evidence_gap_ref"])
    credibility_parts = {
        "verified_share": verified_share,
        "concentration_free": concentration_free,
        "evidence_ok": evidence_ok,
    }
    credibility = _weighted(credibility_parts, credibility_weights)

    if metrics_missing:
        # metrics 缺失会让 intensity/可信度分项失真，如实记录而不是静默当成 0
        activity["missing"] = sorted(set(activity["missing"]) | {"metrics"})

    row.update({
        "activity": float(activity["value"] or 0.0),
        "credibility": credibility["value"],
        "activity_components": {
            "value": activity["value"],
            "inputs": activity["components"],
            "weights": activity["weights"],
            "missing": activity["missing"],
            "renormalized": activity["renormalized"],
        },
        "credibility_components": {
            "value": credibility["value"],
            "inputs": credibility["components"],
            "weights": credibility["weights"],
            "missing": credibility["missing"],
            "renormalized": credibility["renormalized"],
        },
        "item_count": item_total,
        "platform_count": platform_count,
        "source_count": source_count,
        "unverified_share": round(unverified / float(len(events)), 4),
        "concentration_share": round(concentrated / float(len(events)), 4),
        "evidence_gap_count": evidence_gap,
        "continuing_event_count": continuing,
        "grew_event_count": grew,
        "top_activity_heat_mean": None if top_mean is None else round(top_mean, 2),
        "inputs_available": sorted(set(activity["components"]) | set(credibility["components"])),
        "missing_inputs": sorted(set(activity["missing"]) | set(credibility["missing"])),
    })
    store.save_threat_daily(conn, row)
    return row


def _legacy_point(conn, date: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """由 ``radar_event_snapshots`` 反推的历史点（口径 0）。

    这些行是**当时真实写入**的，可信；但它们只有 item_count / platform_count / heat，
    没有绝对强度、没有置信度分布、没有来源集中标记，所以：
    强度分项与全部分项可信度一律记为缺失，绝不用其它数字顶替。
    ``heat`` 是当日相对重标定值，只作为诊断信息给出，不参与任何跨日比较。
    """
    refs = _refs(cfg)
    activity_weights = _weights(cfg, "activity_weights", DEFAULT_ACTIVITY_WEIGHTS)
    row = conn.execute(
        "SELECT COUNT(1) n, SUM(item_count) items, AVG(platform_count) pl, AVG(heat) heat "
        "FROM radar_event_snapshots WHERE date=?", (date,)).fetchone()
    count = int((row["n"] if row else 0) or 0)
    point: Dict[str, Any] = {
        "date": date,
        "available": False,
        "caliber_version": CALIBER_LEGACY,
        "activity": None,
        "credibility": None,
        "event_count": count,
        "story_count": None,
        "item_count": int((row["items"] if row else 0) or 0),
        "platform_count": None,
        "source_count": None,
        "unverified_share": None,
        "concentration_share": None,
        "evidence_gap_count": None,
        "activity_components": {},
        "credibility_components": {},
        "missing_inputs": ["absolute_intensity", "unverified_share", "concentration",
                           "evidence_gap", "platform_universe"],
        "diagnostics": {
            "basis": "radar_event_snapshots",
            "relative_attention_mean": (round(float(row["heat"]), 2)
                                        if row and row["heat"] is not None else None),
            "relative_attention_comparable": False,
            "note": "heat 是当日按最强事件重标定的相对值，不做跨日比较，也不参与威胁活动口径。",
        },
    }
    if not count:
        point["available"] = False
        point["missing_inputs"] = ["no_snapshot"]
        return point

    prev_date = _previous_snapshot_date(conn, date)
    continuing = 0
    grew = 0
    if prev_date:
        grow_row = conn.execute(
            "SELECT COUNT(1) cont, "
            "SUM(CASE WHEN cur.item_count > prev.item_count THEN 1 ELSE 0 END) grew "
            "FROM radar_event_snapshots cur "
            "JOIN radar_event_snapshots prev "
            "  ON prev.event_id = cur.event_id AND prev.date = ? "
            "WHERE cur.date = ?", (prev_date, date)).fetchone()
        continuing = int((grow_row["cont"] if grow_row else 0) or 0)
        grew = int((grow_row["grew"] if grow_row else 0) or 0)

    activity_parts = {
        "intensity": None,
        "volume": min(1.0, point["item_count"] / refs["item_ref"]),
        "breadth": (min(1.0, (float(row["pl"]) if row and row["pl"] is not None else 0.0)
                           / refs["platform_ref"]) if count else None),
        "growth": (grew / float(continuing)) if continuing else None,
    }
    activity = _weighted(activity_parts, activity_weights)
    point["available"] = True
    point["activity"] = activity["value"]
    point["activity_components"] = {
        "value": activity["value"],
        "inputs": activity["components"],
        "weights": activity["weights"],
        "missing": activity["missing"],
        "renormalized": activity["renormalized"],
    }
    point["missing_inputs"] = sorted(set(activity["missing"]) | set(point["missing_inputs"]))
    point["continuing_event_count"] = continuing
    point["grew_event_count"] = grew
    return point


def _point_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把物化行转成序列点。

    关键：**当天没有任何事件时，这一天不是"活动为 0"，而是"没有观测"**。
    activity 字段的 0 只表示"对零个事件求和"，所以这里把 available 置为 False、
    活动与可信度置为 None，并把 ``events`` 记进 missing_inputs——避免把一个尚未采集的
    日子读成"今天风平浪静"。
    """
    event_count = int(row.get("event_count") or 0)
    observed = event_count > 0
    missing = list(row.get("missing_inputs") or [])
    if not observed and "events" not in missing:
        missing = sorted(set(missing) | {"events"})
    return {
        "date": row["date"],
        "available": observed,
        "caliber_version": int(row.get("caliber_version") or CALIBER_CURRENT),
        "activity": (None if not observed or row.get("activity") is None
                     else round(float(row["activity"]), 1)),
        "credibility": (None if not observed or row.get("credibility") is None
                        else round(float(row["credibility"]), 1)),
        "event_count": event_count,
        "story_count": int(row.get("story_count") or 0),
        "item_count": int(row.get("item_count") or 0),
        "platform_count": int(row.get("platform_count") or 0),
        "source_count": int(row.get("source_count") or 0),
        "unverified_share": row.get("unverified_share"),
        "concentration_share": row.get("concentration_share"),
        "evidence_gap_count": row.get("evidence_gap_count"),
        "continuing_event_count": int(row.get("continuing_event_count") or 0),
        "grew_event_count": int(row.get("grew_event_count") or 0),
        "top_activity_heat_mean": row.get("top_activity_heat_mean"),
        "activity_components": row.get("activity_components") or {},
        "credibility_components": row.get("credibility_components") or {},
        "missing_inputs": missing,
        "inputs_available": list(row.get("inputs_available") or []),
        "computed_at": row.get("computed_at"),
    }


def threat_timeline(conn, days: int = 7, end_date: Optional[str] = None,
                    cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """过去 N 天的威胁活动与情报可信度序列（历史只用真实快照）。"""
    cfg = cfg or radar_config.cached()
    intel_cfg = _intel_cfg(cfg)
    days = max(1, min(90, int(days or intel_cfg.get("window_days") or 7)))
    end_date = end_date or today(cfg)
    dates = _date_range(days, end_date)

    materialized = {row["date"]: row for row in store.threat_daily_range(conn, dates[0], dates[-1])}

    points: List[Dict[str, Any]] = []
    missing_union: set = set()
    for date in dates:
        row = materialized.get(date)
        if row is not None:
            point = _point_from_row(row)
        else:
            point = _legacy_point(conn, date, cfg)
            if not point["available"]:
                point = {"date": date, "available": False, "caliber_version": None,
                         "activity": None, "credibility": None, "event_count": 0,
                         "missing_inputs": ["no_data"],
                         "activity_components": {}, "credibility_components": {}}
        missing_union |= set(point.get("missing_inputs") or [])
        points.append(point)

    available = [p for p in points if p.get("available")]
    caliber_days = {str(CALIBER_CURRENT): 0, str(CALIBER_LEGACY): 0}
    for point in available:
        version = point.get("caliber_version")
        caliber_days[str(version)] = caliber_days.get(str(version), 0) + 1

    current_days = caliber_days.get(str(CALIBER_CURRENT), 0)
    legacy_days = caliber_days.get(str(CALIBER_LEGACY), 0)
    min_days = int(intel_cfg.get("min_days_for_full_confidence") or 7)

    if not available:
        confidence = "none"
    elif len(available) < max(2, min_days - 2):
        confidence = "insufficient"
    elif len(available) < min_days:
        confidence = "fair"
    else:
        confidence = "ok"
    if confidence in ("ok", "fair") and current_days < min_days:
        # 历史点里固定口径偏少时下调，避免把"反推值"当成真实历史
        confidence = "insufficient" if current_days == 0 else "fair"

    if current_days and not legacy_days:
        comparability = "uniform"
    elif current_days and legacy_days:
        comparability = "mixed"
    elif legacy_days:
        comparability = "legacy_only"
    else:
        comparability = "none"

    trend = _trend(points, CALIBER_CURRENT) or _trend(points, CALIBER_LEGACY)

    return {
        "points": points,
        "window": {"days": days, "from": dates[0], "to": dates[-1]},
        "latest": (available[-1] if available else None),
        "history_days": len(available),
        "requested_days": days,
        "caliber_days": caliber_days,
        "history_confidence": confidence,
        "comparability": comparability,
        "trend": trend,
        "caliber_version": CALIBER_CURRENT,
        "caliber_note": CALIBER_NOTE,
        "missing_inputs": sorted(missing_union),
        "notes": _confidence_note(confidence, len(available), days, current_days, legacy_days),
    }


def _trend(points: List[Dict[str, Any]], caliber: int) -> Optional[Dict[str, Any]]:
    """只在同一口径的连续点之间比较活动水平，避免混口径比大小。"""
    series = [p for p in points
              if p.get("available") and p.get("caliber_version") == caliber
              and p.get("activity") is not None]
    if len(series) < 2:
        return None
    first, last = series[-2], series[-1]
    delta = round(float(last["activity"]) - float(first["activity"]), 1)
    if delta > 2.0:
        direction = "up"
    elif delta < -2.0:
        direction = "down"
    else:
        direction = "flat"
    return {
        "activity": direction,
        "delta": delta,
        "from": first["date"],
        "to": last["date"],
        "basis": ("fixed_caliber" if caliber == CALIBER_CURRENT else "legacy_caliber"),
        "caliber_version": caliber,
    }


def _confidence_note(confidence: str, available_days: int, requested: int,
                     current_days: int, legacy_days: int) -> List[str]:
    notes: List[str] = []
    if available_days < requested:
        notes.append("历史数据只有 %d/%d 天，缺失日期不做插值，也不以当前值回填。"
                     % (available_days, requested))
    if confidence == "insufficient":
        notes.append("历史深度不足（固定口径 %d 天），趋势仅供参考。" % current_days)
    if legacy_days and current_days:
        notes.append("序列中 %d 天为反推口径（缺少绝对强度与可信度输入），与固定口径不可直接比较。"
                     % legacy_days)
    elif legacy_days and not current_days:
        notes.append("序列全部为反推口径，只反映条目量与平台广度的变化，不含绝对强度。")
    return notes


# ---------- 监控覆盖（与威胁活动严格分开） ----------

def monitoring_coverage(conn, cfg: Optional[Dict[str, Any]] = None,
                        window_days: int = 7) -> Dict[str, Any]:
    """系统监控覆盖与数据质量。

    这里的一切都属于"我们看得见多少"，**不代表现实世界的风险升高**。
    """
    cfg = cfg or radar_config.cached()
    sources = store.sources(conn)
    total = len(sources)
    enabled = [s for s in sources if s["enabled"]]
    ok = [s for s in sources if s["status"] == "ok"]
    failed = [s for s in sources if s["status"] == "error"]
    paused = [s for s in sources if s["status"] == "paused"]
    unavailable = [s for s in sources if s["kind"] == "unavailable"]
    disabled = [s for s in sources if not s["enabled"]]

    structural = len([s for s in unavailable if s["enabled"]])
    live_base = max(1, len([s for s in enabled if s["kind"] != "unavailable"]))
    source_deficit = _clamp01((len(failed) + len(paused)) / float(live_base))
    unavailable_ratio = _clamp01(len(unavailable) / float(total)) if total else 0.0

    intl_cfg = _intel_cfg(cfg)
    intl_ref = float(intl_cfg.get("intl_share_ref") or 0.30)
    lang_row = conn.execute(
        "SELECT COUNT(1) n, "
        "SUM(CASE WHEN language IS NULL OR language = '' "
        "         OR language LIKE 'zh%' OR language IN ('Chinese') THEN 1 ELSE 0 END) zh "
        "FROM radar_items").fetchone()
    item_total = int((lang_row["n"] if lang_row else 0) or 0)
    zh_total = int((lang_row["zh"] if lang_row else 0) or 0)
    intl_share = None
    if item_total:
        intl_share = round(1.0 - (zh_total / float(item_total)), 4)
    language_gap = None
    if intl_share is not None:
        language_gap = _clamp01(1.0 - min(1.0, intl_share / intl_ref))

    if total == 0:
        # 一个源都不知道，就不能声称"覆盖良好"——如实返回未知
        blind = {"value": None, "components": {}, "weights": {}, "missing": ["sources"],
                 "renormalized": False}
    else:
        parts = {"source_deficit": source_deficit, "platform_gap": unavailable_ratio}
        if language_gap is not None:
            parts["language_gap"] = language_gap
        weights = {"source_deficit": 0.5, "platform_gap": 0.2, "language_gap": 0.3}
        blind = _weighted(parts, weights)
    health = None if blind["value"] is None else round(100.0 - float(blind["value"]), 1)

    blind_spots: List[Dict[str, Any]] = []
    for source in failed:
        blind_spots.append({"kind": "failed", "platform": source["platform"], "name": source["name"],
                            "detail": source.get("last_error") or "采集失败"})
    for source in paused:
        blind_spots.append({"kind": "paused", "platform": source["platform"], "name": source["name"],
                            "detail": source.get("paused_reason") or "连续失败已自动停用"})
    for source in unavailable:
        blind_spots.append({"kind": "unavailable", "platform": source["platform"],
                            "name": source["name"],
                            "detail": source.get("notes") or "无合规免费入口，未接入"})
    if language_gap is not None and language_gap > 0.3:
        blind_spots.append({
            "kind": "language_gap", "platform": "—", "name": "国际来源覆盖",
            "detail": "非中文条目占比约 %.0f%%，低于参考值 %.0f%%，国际视角偏弱。"
                      % (intl_share * 100.0, intl_ref * 100.0)})

    return {
        "coverage_score": health,
        "coverage_definition": "0–100，越高表示监控覆盖越完整（100 = 无失败源、无结构缺口、语言覆盖达标）。",
        "components": {
            "value": blind["value"],
            "inputs": blind["components"],
            "weights": blind["weights"],
            "missing": blind["missing"],
            "renormalized": blind["renormalized"],
        },
        "sources": {"total": total, "enabled": len(enabled), "ok": len(ok),
                    "failed": len(failed), "paused": len(paused),
                    "unavailable": len(unavailable), "disabled": len(disabled),
                    "structural_unavailable_enabled": structural},
        "intl_share": intl_share,
        "intl_share_ref": round(intl_ref, 4),
        "items_in_scope": item_total,
        "blind_spots": blind_spots[:20],
        "missing_inputs": (["sources"] if total == 0 else [])
                          + ([] if intl_share is not None else ["language_field"]),
        "note": "数据源停用、额度不足、国际覆盖偏弱都属于监控盲区，"
                "代表我们看不见，不代表现实世界更危险。",
    }


# ---------- 战略风险（可解释、归一化） ----------

def strategic_overview(conn, timeline: Optional[Dict[str, Any]] = None,
                       cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """战略风险的三个维度分开呈现，再给一个**明确标注的估算值**。"""
    cfg = cfg or radar_config.cached()
    timeline = timeline or threat_timeline(conn, cfg=cfg)
    latest = timeline.get("latest") or {}
    coverage = monitoring_coverage(conn, cfg)

    activity = latest.get("activity")
    credibility = latest.get("credibility")

    parts: Dict[str, Optional[float]] = {
        "observed_activity": (float(activity) / 100.0) if activity is not None else None,
        "evidence_uncertainty": (1.0 - float(credibility) / 100.0
                                 if credibility is not None else None),
        "monitoring_blind_spot": (None if coverage.get("coverage_score") is None
                                 else 1.0 - float(coverage["coverage_score"]) / 100.0),
    }
    weights = _weights(cfg, "strategic_weights", DEFAULT_STRATEGIC_WEIGHTS)
    composite = _weighted(parts, weights)

    drivers: List[str] = []
    if coverage["sources"]["paused"]:
        drivers.append("%d 个源已自动停用（连续失败）" % coverage["sources"]["paused"])
    if coverage["sources"]["unavailable"]:
        drivers.append("%d 个平台无合规免费入口，结构性无法监控"
                       % coverage["sources"]["unavailable"])
    if latest.get("unverified_share") is not None and latest["unverified_share"] > 0.3:
        drivers.append("约 %.0f%% 事件未获多方验证" % (latest["unverified_share"] * 100.0))
    if coverage.get("intl_share") is not None and coverage["intl_share"] < coverage["intl_share_ref"]:
        drivers.append("国际来源占比偏低（非中文条目约 %.0f%%）"
                       % (coverage["intl_share"] * 100.0))
    if not drivers:
        drivers.append("暂无明显异常信号")

    return {
        "observed_activity": {
            "score": (None if activity is None else round(float(activity), 1)),
            "definition": "仅表示公开信息中可观测到的活动强度及其变化，不是危险程度。",
            "date": latest.get("date"),
            "caliber_version": latest.get("caliber_version"),
            "basis": ("fixed_caliber" if latest.get("caliber_version") == CALIBER_CURRENT
                      else ("legacy_caliber" if latest.get("available") else "no_data")),
            "partial": latest.get("caliber_version") == CALIBER_LEGACY,
            "missing_inputs": list(latest.get("missing_inputs") or []),
        },
        "credibility": {
            "score": (None if credibility is None else round(float(credibility), 1)),
            "definition": "0–100，越高表示信息越可信（未验证比例低、来源不集中、有媒体报道）。",
            "unverified_share": latest.get("unverified_share"),
            "concentration_share": latest.get("concentration_share"),
            "evidence_gap_count": latest.get("evidence_gap_count"),
            "missing_inputs": list(latest.get("missing_inputs") or []),
        },
        "monitoring": coverage,
        "estimate": {
            "score": composite["value"],
            "label": ESTIMATE_LABEL,
            "is_estimate": True,
            "components": composite["components"],
            "weights": composite["weights"],
            "missing_inputs": composite["missing"],
            "renormalized": composite["renormalized"],
        },
        "drivers": drivers[:6],
        "missing_inputs": sorted(set(composite["missing"]) | set(coverage.get("missing_inputs") or [])),
        "notes": SEMANTICS,
    }


# ---------- 情报流 ----------

def intelligence_feed(conn, date: Optional[str] = None, limit: int = 20, offset: int = 0,
                      cfg: Optional[Dict[str, Any]] = None, theme: Optional[str] = None,
                      country: Optional[str] = None,
                      min_heat: Optional[float] = None) -> Dict[str, Any]:
    """精选情报事件流：复用日报里已经算好的 highlights，并补上主题/地理字段。

    唯一增补：
    * NEW 标记（与 ``/api/radar/report`` 同一套 ``seen_items`` 机制）；
    * ``theme`` / ``region`` / ``geo`` —— 按 ``event_id`` 从 ``radar_events`` 回填，
      与地图点位、国家面板用的是同一批事件，保证 ``story_id`` / 计数口径一致。
    """
    if date:
        row = conn.execute(
            "SELECT * FROM radar_reports WHERE date=? AND kind='daily'", (date,)).fetchone()
        latest = {"date": row["date"], "generated_at": row["generated_at"],
                  "payload": _json_load(row["payload"], {})} if row else None
    else:
        latest = store.latest_report(conn, "daily")

    if not latest:
        return {"pending": True, "items": [], "total": 0, "offset": offset, "limit": limit,
                "report_date": None, "generated_at": None,
                "missing_inputs": ["daily_report"]}

    payload = latest.get("payload") or {}
    highlights = list(payload.get("highlights") or [])
    try:
        seen_store.annotate("radar", highlights, "story_id")
    except Exception as exc:  # noqa: BLE001 - NEW 标记失败不影响情报流
        print("[intel] seen annotate failed:", exc)

    # 按 event_id 回填主题与地理，和地图共用一个口径
    event_ids = [str(h.get("event_id")) for h in highlights if h.get("event_id")]
    geo_map: Dict[str, Dict[str, Any]] = {}
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        for row in conn.execute(
                "SELECT id, date, theme, region, geo_lat, geo_lon, geo_label, geo_confidence, "
                "geo_method, event_country_code, origin_country_code FROM radar_events "
                "WHERE id IN (%s)" % placeholders, event_ids).fetchall():
            geo_map[str(row["id"])] = dict(row)

    enriched: List[Dict[str, Any]] = []
    for item in highlights:
        row = geo_map.get(str(item.get("event_id"))) or {}
        item = dict(item)
        item["theme"] = row.get("theme") or "general_news"
        item["region"] = row.get("region") or "unknown"
        item["date"] = row.get("date")
        item["country_code"] = row.get("event_country_code")
        item["origin_country_code"] = row.get("origin_country_code")
        item["geo"] = {
            "lat": row.get("geo_lat"), "lon": row.get("geo_lon"),
            "label": row.get("geo_label"), "confidence": row.get("geo_confidence"),
            "method": row.get("geo_method"),
        } if row.get("geo_lat") is not None else None
        enriched.append(item)

    def _keep(item: Dict[str, Any]) -> bool:
        if theme and str(item.get("theme")) != theme:
            return False
        if country and str(item.get("country_code") or "").upper() != str(country).upper():
            return False
        if min_heat is not None:
            try:
                if float(item.get("heat") or 0) < float(min_heat):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    enriched = [item for item in enriched if _keep(item)]

    total = len(enriched)
    limit = max(1, min(200, int(limit or 20)))
    offset = max(0, int(offset or 0))
    items = enriched[offset:offset + limit]

    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
        "report_date": latest.get("date"),
        "generated_at": latest.get("generated_at"),
        "filters": {"theme": theme, "country": country, "min_heat": min_heat},
        "missing_inputs": [],
        "note": "条目与日报 highlights 同源（story_id 一一对应）；"
                "summary_provider=extractive 表示未使用大模型，仅为已采集标题的拼接。",
    }


# ---------- 实时新闻（游标分页） ----------

TREND_SOURCES = ("social_trend",)


def _live_row(row) -> Dict[str, Any]:
    data = dict(row)
    published = str(data.get("published_at") or "").strip()
    collected = str(data.get("collected_at") or "").strip()
    source_type = str(data.get("source_type") or "")
    return {
        "id": int(data.get("id") or 0),
        "title": data.get("title") or "",
        "url": data.get("url") or "",
        "platform": data.get("platform") or "",
        "source_id": data.get("source_id") or "",
        "source_type": source_type,
        "language": data.get("language") or "",
        "published_at": published,
        "published_at_missing": not published,
        "effective_at": published or collected,
        "time_source": "published_at" if published else "collected_at",
        "collected_at": collected,
        "event_id": data.get("event_id"),
        "is_trend_only": source_type in TREND_SOURCES,
        "has_content": bool(str(data.get("raw_content") or "").strip()),
    }


def live_news(conn, limit: int = 50, before_id: Optional[int] = None,
              since_id: Optional[int] = None, q: Optional[str] = None,
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """近实时新闻流。

    两种游标语义严格分开：
    * ``before_id``：取比它更小的 id（向历史翻页），``ORDER BY id DESC``；
    * ``since_id`` ：取比它更大的 id（拉新增），``ORDER BY id ASC`` 后倒序展示，
      从而**先取最老的一批新内容**，不会在限流截断时漏掉中间记录。
    """
    cfg = cfg or radar_config.cached()
    intel_cfg = _intel_cfg(cfg)
    max_limit = max(1, int(intel_cfg.get("live_news_max_limit") or 200))
    limit = max(1, min(max_limit, int(limit or intel_cfg.get("live_news_default_limit") or 50)))

    where: List[str] = []
    params: List[Any] = []
    if since_id is not None:
        where.append("id > ?")
        params.append(int(since_id))
    elif before_id is not None:
        where.append("id < ?")
        params.append(int(before_id))
    if q:
        where.append("title LIKE ?")
        params.append("%" + str(q)[:60] + "%")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    ascending = since_id is not None
    order = "ASC" if ascending else "DESC"

    rows = conn.execute(
        "SELECT * FROM radar_items%s ORDER BY id %s LIMIT ?" % (clause, order),
        params + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    if ascending:
        rows = list(reversed(rows))

    items = [_live_row(row) for row in rows]
    ids = [item["id"] for item in items]
    older: Optional[int] = min(ids) if ids else None
    newer: Optional[int] = max(ids) if ids else None

    return {
        "items": items,
        "returned": len(items),
        "limit": limit,
        "cursor": {
            # before 模式：下一页往更旧的 id 走；本页之后是否还有更旧记录由 has_more 决定
            "before_id": older,
            # 只有本页没有任何更旧记录时才为 False，避免客户端误以为到底了
            "has_more_older": (bool(has_more) if not ascending else False),
            "since_id": newer,
            # since 模式：本页取的是最老的一批新记录，has_more 表示还有更新的没取
            "has_more_newer": (bool(has_more) if ascending else None),
        },
        "filters": {"q": q or "", "since_id": since_id, "before_id": before_id},
        "ordering": {"mode": "since" if ascending else "before", "direction": order},
        "missing_inputs": [],
        "note": "published_at 为空的条目回退使用 collected_at（time_source 字段标明来源）；"
                "热榜类条目只有标题、没有正文（is_trend_only=true）。"
                "since 模式按 id 升序取最老的一批，限流截断时不会漏掉中间记录。",
    }


def live_news_newer_count(conn, since_id: int) -> int:
    row = conn.execute("SELECT COUNT(1) c FROM radar_items WHERE id > ?", (int(since_id),)).fetchone()
    return int((row["c"] if row else 0) or 0)


# ---------- 国家风险（本阶段明确不可用） ----------

def _geo_events(conn, since_date: str, until_date: Optional[str] = None,
                theme: Optional[str] = None, country: Optional[str] = None,
                min_heat: Optional[float] = None,
                limit: int = 600) -> List[Dict[str, Any]]:
    """**唯一**的事件取数入口：地图点位、情报流、国家面板都从这里取，保证口径一致。"""
    from ..geo import countries as geo_countries

    where = ["geo_lat IS NOT NULL", "geo_lon IS NOT NULL",
             "geo_confidence IS NOT NULL", "geo_confidence <> 'none'",
             "date >= ?"]
    params: List[Any] = [since_date]
    if until_date:
        where.append("date <= ?")
        params.append(until_date)
    if theme:
        where.append("theme = ?")
        params.append(theme)
    if country:
        where.append("event_country_code = ?")
        params.append(str(country).upper())
    if min_heat is not None:
        where.append("heat >= ?")
        params.append(float(min_heat))

    rows = conn.execute(
        "SELECT id, date, title, story_id, heat, importance, confidence, item_count, "
        "platform_count, source_count, first_seen_at, last_updated_at, theme, region, "
        "geo_lat, geo_lon, geo_label, geo_confidence, geo_method, geo_source, "
        "event_country_code, origin_country_code, tech_flag "
        "FROM radar_events WHERE " + " AND ".join(where) +
        " ORDER BY (importance * 0.5 + heat * 0.5) DESC LIMIT ?", params + [limit]).fetchall()

    out = []
    for row in rows:
        data = dict(row)
        country_code = data.get("event_country_code")
        out.append({
            "event_id": data["id"],
            "story_id": data.get("story_id"),
            "title": data.get("title") or "",
            "date": data.get("date"),
            "lat": float(data["geo_lat"]),
            "lon": float(data["geo_lon"]),
            "geo_label": data.get("geo_label") or "",
            "geo_confidence": data.get("geo_confidence") or "none",
            "geo_method": data.get("geo_method") or "",
            "geo_source": data.get("geo_source") or "",
            "country_code": country_code,
            "country_name": geo_countries.name(country_code) if country_code else "",
            "origin_country_code": data.get("origin_country_code"),
            "theme": data.get("theme") or "general_news",
            "region": data.get("region") or "unknown",
            "heat": data.get("heat"),
            "importance": data.get("importance"),
            "confidence": data.get("confidence"),
            "item_count": data.get("item_count"),
            "platform_count": data.get("platform_count"),
            "source_count": data.get("source_count"),
            "first_seen_at": data.get("first_seen_at"),
            "last_updated_at": data.get("last_updated_at"),
            "tech_flag": bool(data.get("tech_flag")),
            "severity": round(float(((data.get("importance") or 0) * 0.5)
                                    + ((data.get("heat") or 0) * 0.5)), 1),
        })
    return out


# ---------- 覆盖状态与总装 ----------

def country_index(conn, days: int = 7, cfg: Optional[Dict[str, Any]] = None,
                  theme: Optional[str] = None) -> Dict[str, Any]:
    """国家面板：**基于可观测公开事件的估算**，不是官方冲突烈度指数。

    诚实边界（都会出现在响应里）：
    * 没有国家基线数据（``baseline`` 缺失）→ ``coverage="media_only"``、``partial=True``；
    * 事件太少（<2）的国家**不给分数**，只列计数并标 ``low_sample``，避免读成"零风险"；
    * 只统计**已定位**的事件；未定位事件单独报数，不摊派到任何国家。
    """
    from ..geo import countries as geo_countries

    cfg = cfg or radar_config.cached()
    days = max(1, min(90, int(days or 7)))
    end_date = today(cfg)
    since_date = _date_range(days, end_date)[0]
    events = _geo_events(conn, since_date, theme=theme, limit=2000)

    total_row = conn.execute(
        "SELECT COUNT(1) c FROM radar_events WHERE date >= ?", (since_date,)).fetchone()
    total_events = int((total_row["c"] if total_row else 0) or 0)

    buckets: Dict[str, Dict[str, Any]] = {}
    for event in events:
        code = event.get("country_code")
        if not code:
            continue
        bucket = buckets.setdefault(code, {
            "code": code, "name": geo_countries.name(code) or code,
            "event_count": 0, "conflict": 0, "unrest": 0, "disaster": 0,
            "security": 0, "politics": 0, "other": 0,
            "platforms": set(), "severity_max": 0.0, "severity_sum": 0.0,
            "unverified": 0, "low_confidence_geo": 0,
        })
        bucket["event_count"] += 1
        theme_key = event.get("theme") or "general_news"
        if theme_key in ("conflict", "unrest", "disaster", "security", "politics"):
            bucket[theme_key] += 1
        else:
            bucket["other"] += 1
        bucket["platforms"].add(event.get("event_id"))
        severity = float(event.get("severity") or 0.0)
        bucket["severity_max"] = max(bucket["severity_max"], severity)
        bucket["severity_sum"] += severity
        if str(event.get("confidence") or "") == "unverified":
            bucket["unverified"] += 1
        if str(event.get("geo_confidence") or "") == "low":
            bucket["low_confidence_geo"] += 1

    refs = {"conflict": 4.0, "unrest": 4.0, "disaster": 3.0, "security": 3.0, "volume": 20.0}
    countries_out: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        count = bucket["event_count"]
        components = {
            "conflict": min(1.0, bucket["conflict"] / refs["conflict"]),
            "unrest": min(1.0, bucket["unrest"] / refs["unrest"]),
            "disaster": min(1.0, bucket["disaster"] / refs["disaster"]),
            "security": min(1.0, bucket["security"] / refs["security"]),
            "media_volume": min(1.0, count / refs["volume"]),
        }
        weights = {"conflict": 0.35, "unrest": 0.20, "disaster": 0.15,
                   "security": 0.15, "media_volume": 0.15}
        low_sample = count < 2
        score = None if low_sample else round(
            100.0 * sum(weights[k] * components[k] for k in weights), 1)
        countries_out.append({
            "code": bucket["code"], "name": bucket["name"],
            "score": score,
            "score_label": "可观测紧张度（基于公开媒体事件，非官方指数）",
            "low_sample": low_sample,
            "event_count": count,
            "counts": {"conflict": bucket["conflict"], "unrest": bucket["unrest"],
                       "disaster": bucket["disaster"], "security": bucket["security"],
                       "politics": bucket["politics"], "other": bucket["other"]},
            "components": components,
            "weights": weights,
            "severity_max": round(bucket["severity_max"], 1),
            "severity_mean": round(bucket["severity_sum"] / count, 1) if count else None,
            "unverified_share": round(bucket["unverified"] / count, 4) if count else None,
            "country_centroid_anchors": bucket["low_confidence_geo"],
            "baseline": None,
            "missing_inputs": ["baseline"],
        })

    countries_out.sort(key=lambda row: (row["score"] if row["score"] is not None else -1,
                                        row["event_count"]), reverse=True)
    located = len(events)
    return {
        "available": bool(countries_out),
        "partial": True,
        "coverage": "media_only",
        "reason": None if countries_out else
                  "窗口内没有完成地理编码的事件，因此不产出国家估算。",
        "countries": countries_out[:40],
        "days": days,
        "window": {"from": since_date, "to": end_date},
        "events_located": located,
        "events_total": total_events,
        "geocode_coverage": (round(located / float(total_events), 4) if total_events else None),
        "baseline_note": "缺少国家基线数据（冲突伤亡/治理指标），因此所有权重都落在"
                         "「可观测事件」上；这不是官方冲突烈度指数。",
        "semantics": "只统计已定位事件；未定位事件不摊派给任何国家。"
                     "事件数 < 2 的国家不给分数（low_sample），不等于零风险。",
        "missing_inputs": ["baseline"],
        "origin_vs_event": "event_country_code 是事件发生国；origin_country_code 是报道来源国，"
                           "两者不混用。",
    }


def _attach_duplicate_hints(points: List[Dict[str, Any]],
                            distance_km: float = 165.0,
                            hours: float = 72.0) -> int:
    """跨来源的"疑似同一事件"提示——**只标记，不合并**。

    USGS / EONET / GDACS 可能同时报道同一次地震或同一场风暴。没有经过验证的
    唯一关联关系时，擅自合并会凭空减少灾害数量，所以这里只在点位上挂
    ``related_sources``，让用户自己判断。
    """
    import math

    for point in points:
        point.setdefault("related_sources", [])
    pairs = 0
    rows = [p for p in points
            if p.get("latitude") is not None and p.get("longitude") is not None]
    for index, first in enumerate(rows):
        for second in rows[index + 1:]:
            if first.get("layer") == second.get("layer"):
                continue
            lat1, lon1 = float(first["latitude"]), float(first["longitude"])
            lat2, lon2 = float(second["latitude"]), float(second["longitude"])
            dlat = (lat1 - lat2) * 111.32
            dlon = (lon1 - lon2) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2.0))
            km = math.hypot(dlat, dlon)
            if km > distance_km:
                continue
            gap = None
            try:
                when1 = dt.datetime.fromisoformat(
                    str(first.get("observed_at") or first.get("collected_at")).replace("Z", "+00:00"))
                when2 = dt.datetime.fromisoformat(
                    str(second.get("observed_at") or second.get("collected_at")).replace("Z", "+00:00"))
                if when1.tzinfo is None:
                    when1 = when1.replace(tzinfo=dt.timezone.utc)
                if when2.tzinfo is None:
                    when2 = when2.replace(tzinfo=dt.timezone.utc)
                gap = abs((when1 - when2).total_seconds()) / 3600.0
            except (TypeError, ValueError):
                gap = None
            if gap is not None and gap > hours:
                continue
            pairs += 1
            first["related_sources"].append({
                "layer": second.get("layer"), "external_id": second.get("external_id"),
                "title": second.get("title"), "distance_km": round(km, 1),
                "hours_apart": None if gap is None else round(gap, 1),
                "note": "位置与时间接近，可能为同一事件；未自动合并",
            })
            second["related_sources"].append({
                "layer": first.get("layer"), "external_id": first.get("external_id"),
                "title": first.get("title"), "distance_km": round(km, 1),
                "hours_apart": None if gap is None else round(gap, 1),
                "note": "位置与时间接近，可能为同一事件；未自动合并",
            })
    return pairs


def map_payload(conn, cfg: Optional[Dict[str, Any]] = None, days: int = 7,
                theme: Optional[str] = None, country: Optional[str] = None,
                min_heat: Optional[float] = None,
                layers: Optional[Sequence[str]] = None,
                hazards: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """地图数据：已定位的新闻事件 + 结构化地理点。没有真实坐标的条目一律不出现。"""
    from ..geo import sources as geo_sources

    cfg = cfg or radar_config.cached()
    days = max(1, min(90, int(days or 7)))
    end_date = today(cfg)
    since_date = _date_range(days, end_date)[0]
    since_iso = since_date + "T00:00:00"
    wanted = set(layers or [])
    wanted_hazards = set(hazards or [])

    events = _geo_events(conn, since_date, until_date=end_date, theme=theme,
                         country=country, min_heat=min_heat, limit=800)
    raw_points = store.geo_points(conn, since_iso=since_iso, limit=2000)
    counts = store.geo_point_counts(conn, since_iso=since_iso)
    hazard_counts = store.geo_hazard_counts(conn, since_iso=since_iso)

    catalog = geo_sources.layer_catalog()
    for layer in catalog:
        layer["count"] = int(counts.get(layer["key"], 0))
        layer["hazards"] = layer.get("hazards") or []
    connected = {layer["key"] for layer in catalog if layer.get("connected")}
    points = [p for p in raw_points if p.get("layer") in connected]
    if wanted:
        points = [p for p in points if p.get("layer") in wanted]
    if wanted_hazards:
        points = [p for p in points if (p.get("hazard") or "other") in wanted_hazards]

    by_confidence: Dict[str, int] = {}
    for event in events:
        key = str(event.get("geo_confidence") or "none")
        by_confidence[key] = by_confidence.get(key, 0) + 1

    duplicate_pairs = _attach_duplicate_hints(points)

    last_collect = store.last_geo_collect_at(conn)
    return {
        "available": True,
        "reason": None,
        "projection": {"name": "Web Mercator", "lat_limit": 85.05112878, "aspect": 2.0},
        "window": {"from": since_date, "to": end_date, "days": days},
        "events": events,
        "geo_points": points,
        "layers": catalog,
        "hazard_groups": geo_sources.hazard_groups(),
        "counts": {
            "news_events": len(events),
            "geo_points": len(points),
            "by_layer": {key: int(value) for key, value in counts.items()},
            "by_hazard": {key: int(value) for key, value in hazard_counts.items()},
            "by_confidence": by_confidence,
        },
        "duplicate_hint_count": duplicate_pairs,
        "filters": {"theme": theme, "country": country, "min_heat": min_heat,
                    "layers": sorted(wanted) if wanted else [],
                    "hazards": sorted(wanted_hazards) if wanted_hazards else []},
        "last_collected_at": last_collect,
        "missing_inputs": [] if events else ["located_events"],
        "note": "只显示有真实坐标的事件。geo_confidence=low 的点是国家质心近似；"
                "geometry_type=Polygon 的灾害按面绘制，其中心不作为精确事发点。"
                "跨来源的疑似重复只做提示，不做自动合并。",
    }


def coverage_status(conn, timeline: Optional[Dict[str, Any]] = None,
                    cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or radar_config.cached()
    timeline = timeline or threat_timeline(conn, cfg=cfg)
    latest_report = store.latest_report(conn, "daily")
    item_row = conn.execute(
        "SELECT COUNT(1) n, MAX(collected_at) newest, MIN(collected_at) oldest "
        "FROM radar_items").fetchone()
    event_row = conn.execute("SELECT COUNT(1) n FROM radar_events").fetchone()

    last_collect = None
    try:
        last_collect = store.last_collect_at(conn)
    except Exception:  # noqa: BLE001
        last_collect = None

    freshness = None
    if last_collect:
        try:
            then = dt.datetime.fromisoformat(last_collect)
            now = dt.datetime.now(then.tzinfo) if then.tzinfo else dt.datetime.now()
            freshness = round((now - then).total_seconds() / 60.0, 1)
        except (TypeError, ValueError):
            freshness = None

    return {
        "daily_report_date": (latest_report or {}).get("date"),
        "daily_report_generated_at": (latest_report or {}).get("generated_at"),
        "last_collect_at": last_collect,
        "data_freshness_minutes": freshness,
        "items_total": int((item_row["n"] if item_row else 0) or 0),
        "items_collected_from": (item_row["oldest"] if item_row else None),
        "items_collected_to": (item_row["newest"] if item_row else None),
        "events_total": int((event_row["n"] if event_row else 0) or 0),
        "history": {"days_available": timeline.get("history_days"),
                    "days_requested": timeline.get("requested_days"),
                    "caliber_days": timeline.get("caliber_days")},
        "missing_inputs": list(timeline.get("missing_inputs") or []),
    }


def build_payload(conn, cfg: Optional[Dict[str, Any]] = None, days: int = 7,
                  end_date: Optional[str] = None, feed_limit: int = 20, feed_offset: int = 0,
                  news_limit: int = 50, before_id: Optional[int] = None,
                  since_id: Optional[int] = None, q: Optional[str] = None,
                  theme: Optional[str] = None, country: Optional[str] = None,
                  min_heat: Optional[float] = None,
                  layers: Optional[Sequence[str]] = None,
                  hazards: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """统一聚合：一次请求给五个面板同一份数据（地图/情报/国家共用相同 id 与筛选口径）。"""
    cfg = cfg or radar_config.cached()
    timeline = threat_timeline(conn, days=days, end_date=end_date, cfg=cfg)
    strategic = strategic_overview(conn, timeline=timeline, cfg=cfg)
    coverage = coverage_status(conn, timeline=timeline, cfg=cfg)

    country_block = country_index(conn, days=days, cfg=cfg, theme=theme)
    mapping = map_payload(conn, cfg=cfg, days=days, theme=theme, country=country,
                          min_heat=min_heat, layers=layers, hazards=hazards)

    degraded: List[Dict[str, Any]] = []
    if not country_block["available"]:
        degraded.append({"module": "country_index", "reason": country_block["reason"],
                         "missing_inputs": country_block["missing_inputs"]})
    elif country_block.get("partial"):
        degraded.append({"module": "country_index",
                         "reason": "只有媒体可观测数据，缺少国家基线；分数是估算值。",
                         "missing_inputs": country_block["missing_inputs"]})
    if not mapping["events"]:
        degraded.append({"module": "map",
                         "reason": "当前筛选窗口内没有已定位事件（或地理编码尚未回填）。",
                         "missing_inputs": ["located_events"]})
    if timeline.get("history_confidence") in ("none", "insufficient"):
        degraded.append({"module": "threat_timeline",
                         "reason": "历史快照不足（已有 %s 天 / 需要 %s 天），不做插值。"
                                   % (timeline.get("history_days"), timeline.get("requested_days")),
                         "missing_inputs": ["history_depth"]})

    missing = set(coverage.get("missing_inputs") or [])
    missing |= set(strategic.get("missing_inputs") or [])
    missing |= set(timeline.get("missing_inputs") or [])
    missing |= set(country_block.get("missing_inputs") or [])

    return {
        "module": "radar-intel",
        "version": 2,
        "generated_at": _now_iso(),
        "window": timeline.get("window"),
        "filters": {"days": days, "theme": theme, "country": country,
                    "min_heat": min_heat,
                    "layers": sorted(layers) if layers else []},
        "coverage": coverage,
        "threat_timeline": timeline,
        "today": timeline.get("latest"),
        "strategic": strategic,
        "intelligence_feed": intelligence_feed(conn, date=None, limit=feed_limit,
                                               offset=feed_offset, cfg=cfg, theme=theme,
                                               country=country, min_heat=min_heat),
        "live_news": live_news(conn, limit=news_limit, before_id=before_id, since_id=since_id,
                               q=q, cfg=cfg),
        "country_index": country_block,
        "map": mapping,
        "geo": {
            "coverage": store.geo_tag_stats(conn),
            "levels": ["high", "medium", "low", "none"],
        },
        "missing_inputs": sorted(missing),
        "degraded": degraded,
        "semantics": SEMANTICS,
        "llm": {"used": False,
                "note": "本模块不调用大模型；摘要缺口由 summarize.extractive 的抽取式回退覆盖。"},
        "disclaimer": "所有指标均由本站已采集的公开材料计算，"
                      "不构成对现实世界冲突或安全风险的判断。",
    }