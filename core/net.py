"""统一出网层：代理开着、关着、半死着，项目都能跑。

背景：项目里所有请求原来都直接调 ``urllib.request.urlopen``，而 urllib 默认会
跟随系统/环境变量里的代理。一旦那个本地代理端口没人监听（VPN 退出、Clash 重连、
会话级代理失效），**所有**域名都会立刻变成 ``WinError 10061 目标计算机积极拒绝``，
包括本来根本不需要代理的国内站点。

这里的做法是「多路由 + 自动切换」：

* 路由（route）= 一种出口方式
  - ``system``：跟随系统 / 环境变量代理（urllib 默认行为）
  - ``direct``：强制直连（``ProxyHandler({})``）
  - ``manual``：配置里指定的代理
* 模式（mode）
  - ``auto``（默认）：系统有代理就先用代理，失败立刻换直连；反之亦然
  - ``system`` / ``direct`` / ``manual``：只用指定出口
* 只有**连接级**失败（拒绝连接、超时、DNS、TLS、代理 407/5xx）才换路由；
  HTTP 4xx/5xx 是站点给的答复，原样抛给调用方，不重试。
* 失败的路由进入冷却（默认 120 秒），期间优先用别的路由，避免每个请求都白等一次。
* 系统代理每 60 秒重新探测一次，所以运行中开关 VPN 也能自动适配。

配置方式（优先级从高到低）：
1. 代码里调用 ``net.configure(mode, proxy)``（雷达配置加载时会调）
2. 环境变量 ``FB_PROXY_MODE`` / ``FB_PROXY_URL``
3. 默认 ``auto``
"""
from __future__ import annotations

import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

MODES = ("auto", "system", "direct", "manual")

_LOCK = threading.RLock()

_MODE = ""
_MANUAL = ""

_ROUTES: List[Tuple[str, urllib.request.OpenerDirector]] = []
_INDEX: Dict[str, int] = {}
_PREFERRED = 0
_DOWN: Dict[str, float] = {}
_SIGNATURE: Tuple[Any, ...] = ()
_BUILT_AT = 0.0

#: 失败路由的冷却时间（秒）——连接被拒说明出口本身死了，冷却久一点
DOWN_COOLDOWN = 120.0
#: 超时冷却更短：超时往往只是「这个站从这条出口过不去」，不代表出口死了
TIMEOUT_COOLDOWN = 30.0
#: 系统代理重新探测间隔（秒）—— 运行中开/关 VPN 靠这个生效
PROBE_INTERVAL = 60.0


class RouteError(urllib.error.URLError):
    """所有路由都失败时抛出；仍属于 URLError，调用方的旧分支不用改。"""

    def __init__(self, message: str, tried: Optional[List[str]] = None):
        super().__init__(message)
        self.message = message
        self.tried = list(tried or [])

    def __str__(self) -> str:
        return self.message


def configure(mode: str = "", proxy: str = "") -> None:
    """运行时指定出口策略。空值 = 回落到环境变量/默认值。"""
    global _MODE, _MANUAL
    mode = (mode or "").strip().lower()
    if mode and mode not in MODES:
        mode = ""
    with _LOCK:
        if mode == _MODE and (proxy or "") == _MANUAL:
            return
        _MODE = mode
        _MANUAL = (proxy or "").strip()
    refresh()


def mode() -> str:
    with _LOCK:
        return _MODE or (os.environ.get("FB_PROXY_MODE", "").strip().lower() or "auto")


def manual_proxy() -> str:
    with _LOCK:
        return _MANUAL or os.environ.get("FB_PROXY_URL", "").strip()


def _proxies() -> Dict[str, str]:
    try:
        found = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - 探测失败就当没有代理
        return {}
    return {k: v for k, v in found.items()
            if k in ("http", "https", "all") and str(v or "").strip()}


def _opener(proxies: Optional[Dict[str, str]]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies or {}))


def refresh(force: bool = False) -> None:
    """按当前模式重建路由表。系统代理没变时直接复用。"""
    global _ROUTES, _INDEX, _SIGNATURE, _BUILT_AT, _PREFERRED

    current = mode()
    proxies = _proxies()
    manual = manual_proxy()
    signature = (current, tuple(sorted(proxies.items())), manual)
    with _LOCK:
        if not force and signature == _SIGNATURE and _ROUTES:
            return
        routes: List[Tuple[str, urllib.request.OpenerDirector]] = []
        if current == "direct":
            routes.append(("direct", _opener({})))
        elif current == "system":
            routes.append(("system", _opener(proxies)))
        elif current == "manual":
            if manual:
                routes.append(("manual", _opener({"http": manual, "https": manual})))
            else:
                routes.append(("direct", _opener({})))
        else:  # auto
            if proxies:
                routes.append(("system", _opener(proxies)))
            routes.append(("direct", _opener({})))
            if manual:
                routes.append(("manual", _opener({"http": manual, "https": manual})))

        # 同名路由去重（例如 manual 和 system 指向同一个地址）
        seen: Dict[str, int] = {}
        deduped: List[Tuple[str, urllib.request.OpenerDirector]] = []
        for name, opener in routes:
            if name in seen:
                continue
            seen[name] = len(deduped)
            deduped.append((name, opener))

        changed = [n for n, _ in deduped] != [n for n, _ in _ROUTES]
        _ROUTES = deduped
        _INDEX = seen
        _SIGNATURE = signature
        _BUILT_AT = time.time()
        if changed:
            _PREFERRED = 0
            _DOWN.clear()


def _maybe_refresh() -> None:
    if time.time() - _BUILT_AT > PROBE_INTERVAL:
        refresh()


def routes() -> List[str]:
    _maybe_refresh()
    with _LOCK:
        return [name for name, _ in _ROUTES]


def describe() -> Dict[str, Any]:
    """给状态页/诊断用：当前模式、路由顺序、冷却中的路由。"""
    _maybe_refresh()
    now = time.time()
    with _LOCK:
        ordered = [name for name, _ in _ordered_locked(now)]
        return {
            "mode": mode(),
            "routes": [name for name, _ in _ROUTES],
            "order": ordered,
            "preferred": _ROUTES[_PREFERRED][0] if _ROUTES else "",
            "cooling": {name: round(cool - (now - ts), 1)
                        for name, (ts, cool) in _DOWN.items() if now - ts < cool},
            "system_proxy": _proxies(),
        }


def _ordered_locked(now: float) -> List[Tuple[str, urllib.request.OpenerDirector]]:
    if not _ROUTES:
        return []
    start = _PREFERRED % len(_ROUTES)
    order = [start] + [i for i in range(len(_ROUTES)) if i != start]
    fresh, cooling = [], []
    for i in order:
        name = _ROUTES[i][0]
        entry = _DOWN.get(name)
        if entry is not None and now - entry[0] < entry[1]:
            cooling.append(i)
        else:
            fresh.append(i)
    return [_ROUTES[i] for i in fresh + cooling]


def _mark_ok(name: str) -> None:
    global _PREFERRED
    with _LOCK:
        index = _INDEX.get(name)
        if index is not None:
            _PREFERRED = index
        _DOWN.pop(name, None)


def _mark_down(name: str, cooldown: float = DOWN_COOLDOWN) -> None:
    with _LOCK:
        if len(_ROUTES) <= 1:
            return
        _DOWN[name] = (time.time(), cooldown)


def _is_route_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # 407 = 代理要认证；5xx 常见于「代理活着但上游不通」
        return exc.code in (407, 502, 503, 504)
    if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, OSError):
        return True
    return False


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _rebuild(req: urllib.request.Request) -> urllib.request.Request:
    """为每条路由造一个全新的 Request。

    urllib 的 ``ProxyHandler.proxy_open`` 会就地改写 Request 的 host/type/selector
    （``set_proxy``）。同一个对象如果先走代理失败、再拿去直连，它仍然指向代理地址，
    于是「直连兜底」永远连的是那个已经死掉的端口。必须每次重建。
    """
    return urllib.request.Request(
        req.full_url,
        data=req.data,
        headers=dict(req.headers),
        origin_req_host=req.origin_req_host,
        unverifiable=req.unverifiable,
        method=req.get_method(),
    )


def urlopen(req: urllib.request.Request, timeout: Optional[float] = None,
            data: Optional[bytes] = None):
    """打开请求，失败自动换出口。返回值可直接当上下文管理器用。

    只对连接级失败换路由；HTTPError 原样抛出（除 407/5xx 这类代理侧错误）。
    """
    _maybe_refresh()
    with _LOCK:
        candidates = _ordered_locked(time.time())

    if not candidates:
        raise RouteError("没有可用的出网路由")

    tried: List[str] = []
    last: Optional[BaseException] = None
    for name, opener in candidates:
        tried.append(name)
        try:
            resp = opener.open(_rebuild(req), timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (407, 502, 503, 504):
                _mark_down(name)
                last = exc
                continue
            _mark_ok(name)
            raise
        except Exception as exc:  # noqa: BLE001 - 网络错误都是预期内的
            if not _is_route_error(exc):
                raise
            _mark_down(name, TIMEOUT_COOLDOWN if _is_timeout(exc) else DOWN_COOLDOWN)
            last = exc
            continue
        _mark_ok(name)
        return resp

    if isinstance(last, urllib.error.HTTPError):
        raise last
    reason = getattr(last, "reason", last)
    raise RouteError("所有出口都失败（%s）：%s" % ("、".join(tried), reason), tried)


def get_bytes(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


# 导入即探测一次，让第一批请求就拿到正确的路由表
try:
    refresh()
except Exception:  # noqa: BLE001 - 探测失败不该阻断 import
    pass
