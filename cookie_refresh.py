"""Refresh Weibo cookies through the Sina SSO endpoint.

The refresh flow relies on the long-lived ``ALC`` cookie. A successful SSO
request returns a new ``ALC`` together with new Sina cookies, and the
cross-domain redirects set the corresponding ``.weibo.cn`` cookies used by
the mobile Weibo API.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

SSO_LOGIN_URL = "https://login.sina.com.cn/sso/login.php"
SSO_REFERER = "https://login.sina.com.cn/sso/login.php"
SSO_PROFILE_URL = "https://m.weibo.cn/profile/info"
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 8
UTC8 = timezone(timedelta(hours=8))

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
    "Mobile/15E148 Safari/604.1"
)


def _parse_expiry(value: str, max_age: str = "") -> tuple[str, float | None]:
    """Convert a Set-Cookie expiry into UTC+8 text and an epoch timestamp."""
    if max_age:
        try:
            ts = time.time() + int(max_age)
            return (
                datetime.fromtimestamp(ts, UTC8).strftime("%Y-%m-%d %H:%M:%S"),
                ts,
            )
        except ValueError:
            pass
    if not value:
        return "", None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (
            parsed.astimezone(UTC8).strftime("%Y-%m-%d %H:%M:%S"),
            parsed.timestamp(),
        )
    except (TypeError, ValueError, OverflowError):
        return "", None


def _parse_set_cookie(line: str, request_host: str) -> dict[str, Any] | None:
    """Parse one Set-Cookie header without exposing its value."""
    first = line.split(";", 1)[0]
    if "=" not in first:
        return None
    name, value = first.split("=", 1)
    name = name.strip()
    if not name:
        return None

    attrs: dict[str, str] = {}
    for item in line.split(";")[1:]:
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, attr_value = item.split("=", 1)
            attrs[key.strip().lower()] = attr_value.strip()
        else:
            attrs[item.lower()] = ""

    domain = attrs.get("domain", "").strip().lower() or request_host.lower()
    path = attrs.get("path", "/").strip() or "/"
    expires_text, expires_ts = _parse_expiry(
        attrs.get("expires", ""), attrs.get("max-age", "")
    )
    deleted = attrs.get("max-age", "").strip() == "0" or (
        expires_ts is not None and expires_ts <= time.time()
    )
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expires_text,
        "expires_ts": expires_ts,
        "deleted": deleted,
    }


def _apply_set_cookie(
    jar: dict[str, dict[str, dict[str, Any]]], response: httpx.Response
) -> None:
    """Merge response Set-Cookie headers into the manual cookie jar."""
    request_host = (urlparse(str(response.url)).hostname or "").lower()
    for line in response.headers.get_list("set-cookie"):
        parsed = _parse_set_cookie(line, request_host)
        if not parsed:
            continue
        domain = parsed.pop("domain")
        name = parsed.pop("name")
        if parsed.pop("deleted"):
            jar.get(domain, {}).pop(name, None)
            continue
        jar.setdefault(domain, {})[name] = parsed


def _domain_matches(cookie_domain: str, host: str) -> bool:
    """Return whether a cookie domain applies to a request host."""
    cookie_domain = cookie_domain.lower()
    host = host.lower()
    if cookie_domain.startswith("."):
        bare = cookie_domain[1:]
        return host == bare or host.endswith(cookie_domain)
    return host == cookie_domain


def _path_matches(cookie_path: str, request_path: str) -> bool:
    """Return whether a cookie path applies to a request path."""
    cookie_path = cookie_path or "/"
    request_path = request_path or "/"
    if cookie_path == "/":
        return True
    return request_path == cookie_path or request_path.startswith(
        cookie_path.rstrip("/") + "/"
    )


def _cookie_header(jar: dict[str, dict[str, dict[str, Any]]], url: str) -> str:
    """Build a Cookie header, preferring the most specific domain per name."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    request_path = parsed.path or "/"
    chosen: dict[str, tuple[tuple[int, int], str]] = {}
    for domain, cookies in jar.items():
        if not _domain_matches(domain, host):
            continue
        for name, meta in cookies.items():
            if not _path_matches(str(meta.get("path", "/")), request_path):
                continue
            score = (len(domain.lstrip(".")), len(str(meta.get("path", "/"))))
            previous = chosen.get(name)
            if previous is None or score > previous[0]:
                chosen[name] = (score, str(meta.get("value", "")))

    if not chosen:
        return ""
    return "; ".join(f"{name}={value}" for name, (_, value) in sorted(chosen.items()))


def _find_js_redirect(body: str, base_url: str) -> str:
    """Extract a redirect URL from the HTML/JS responses used by Sina SSO."""
    if not body:
        return ""

    match = re.search(r"location\.replace\(\s*[\"']([^\"']+)", body)
    if match:
        return urljoin(base_url, match.group(1).replace("\\/", "/"))

    match = re.search(
        r"(?:window\.|top\.)?location(?:\.href)?\s*=\s*[\"']([^\"']+)", body
    )
    if match:
        return urljoin(base_url, match.group(1).replace("\\/", "/"))

    match = re.search(
        r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"']\d+;\s*url=([^\"']+)",
        body,
        re.I,
    )
    if match:
        target = match.group(1).replace("&#39;", "").replace("'", "")
        return urljoin(base_url, target)

    match = re.search(r"setCrossDomainUrlList\((.*?)\);", body, re.S)
    if match:
        try:
            payload = json.loads(match.group(1))
            urls = payload.get("arrURL") or []
            if urls:
                return urljoin(base_url, urls[0])
        except (TypeError, ValueError):
            pass
    return ""


def _build_request_headers(url: str, cookie_header: str) -> dict[str, str]:
    """Build headers for one SSO/cross-domain request."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("weibo.cn"):
        headers = {
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://m.weibo.cn/",
        }
    else:
        headers = {
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html,application/json,*/*",
            "Referer": "https://weibo.com/",
        }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _collect_weibo_cn_cookies(
    jar: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Return the cookies that apply to m.weibo.cn."""
    result: dict[str, dict[str, Any]] = {}
    for domain, cookies in jar.items():
        if not _domain_matches(domain, "m.weibo.cn"):
            continue
        for name, meta in cookies.items():
            previous = result.get(name)
            score = len(domain.lstrip("."))
            if previous is None or score > previous["score"]:
                result[name] = {**meta, "score": score}
    for name in list(result):
        result[name].pop("score", None)
    return result


async def refresh_weibo_cookie(
    client: httpx.AsyncClient,
    alc: str,
    alf: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Refresh the Weibo mobile cookies through Sina SSO.

    Args:
        client: Shared HTTP client used by the plugin.
        alc: Current Sina SSO ``ALC`` cookie value.
        alf: Optional Sina ``ALF`` cookie value.
        timeout: Request timeout in seconds.

    Returns:
        A result dict. ``alc`` is always populated when the SSO response
        contains a new value, even if a later redirect fails. ``ok`` is true
        only when new ``.weibo.cn`` SUB and SUBP cookies were obtained.
    """
    result: dict[str, Any] = {
        "ok": False,
        "error_code": "",
        "error": "",
        "alc": alc,
        "sina": {},
        "weibo_cn": {},
        "weibo_cn_cookie": "",
        "alc_expires_at": "",
        "alc_expires_ts": None,
        "subp_expires_at": "",
        "subp_expires_ts": None,
    }
    if not alc:
        result["error_code"] = "alc_missing"
        result["error"] = "未配置 ALC"
        return result

    sso_url = (
        f"{SSO_LOGIN_URL}?url={quote(SSO_PROFILE_URL, safe='')}"
        f"&_rand={time.time()}&gateway=1&service=sinawap&entry=sinawap"
        "&useticket=1&returntype=META&sudaref=&_client_version=0.6.33"
    )
    sso_cookie = f"ALC={alc}"
    if alf:
        sso_cookie += f"; ALF={alf}"
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "text/html,application/json,*/*",
        "Referer": SSO_REFERER,
        "Cookie": sso_cookie,
    }

    try:
        response = await client.get(
            sso_url, headers=headers, timeout=timeout, follow_redirects=False
        )
    except Exception as error:
        result["error_code"] = "network_error"
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    jar: dict[str, dict[str, dict[str, Any]]] = {}
    _apply_set_cookie(jar, response)

    sina: dict[str, str] = {}
    for domain, cookies in jar.items():
        if domain not in {".sina.com.cn", "login.sina.com.cn"}:
            continue
        for name, meta in cookies.items():
            sina[name] = str(meta.get("value", ""))
    if "ALC" in sina:
        result["alc"] = sina["ALC"]
    if "ALF" in sina:
        result["sina"]["ALF"] = sina["ALF"]
    for name in ("SUB", "SUBP", "SCF", "tgc", "LT"):
        if name in sina:
            result["sina"][name] = sina[name]

    alc_meta = jar.get("login.sina.com.cn", {}).get("ALC") or jar.get(
        ".sina.com.cn", {}
    ).get("ALC")
    if alc_meta:
        result["alc_expires_at"] = str(alc_meta.get("expires", ""))
        result["alc_expires_ts"] = alc_meta.get("expires_ts")

    subp_meta = jar.get(".sina.com.cn", {}).get("SUBP") or jar.get(".weibo.cn", {}).get(
        "SUBP"
    )
    if subp_meta:
        result["subp_expires_at"] = str(subp_meta.get("expires", ""))
        result["subp_expires_ts"] = subp_meta.get("expires_ts")

    if response.status_code != 302:
        body = response.text
        if "6102" in body or "login.sina.com.cn/signup/signin" in body:
            result["error_code"] = "alc_invalid"
            result["error"] = "ALC 已失效，需要手动更新 Cookie"
        else:
            result["error_code"] = "sso_error"
            result["error"] = f"SSO 返回异常状态码 {response.status_code}"
        return result

    location = response.headers.get("location", "")
    if not location:
        result["error_code"] = "sso_error"
        result["error"] = "SSO 响应缺少跳转地址"
        return result

    current_url = urljoin(sso_url, location)
    for _ in range(MAX_REDIRECTS):
        cookie_header = _cookie_header(jar, current_url)
        request_headers = _build_request_headers(current_url, cookie_header)
        try:
            response = await client.get(
                current_url,
                headers=request_headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except Exception as error:
            result["error_code"] = "network_error"
            result["error"] = f"{type(error).__name__}: {error}"
            break

        _apply_set_cookie(jar, response)
        next_url = response.headers.get("location", "")
        if next_url:
            current_url = urljoin(current_url, next_url)
            continue
        if response.status_code == 200:
            js_url = _find_js_redirect(response.text, current_url)
            if js_url:
                current_url = js_url
                continue
        break

    weibo_cn = _collect_weibo_cn_cookies(jar)
    if "SUB" in weibo_cn and "SUBP" in weibo_cn:
        result["ok"] = True
        result["weibo_cn"] = weibo_cn
        result["weibo_cn_cookie"] = _cookie_header(jar, "https://m.weibo.cn/")
    else:
        result["error_code"] = "redirect_error"
        result["error"] = "crossdomain 未返回 .weibo.cn 的 SUB/SUBP"

    subp_meta = jar.get(".weibo.cn", {}).get("SUBP")
    if subp_meta:
        result["subp_expires_at"] = str(subp_meta.get("expires", ""))
        result["subp_expires_ts"] = subp_meta.get("expires_ts")

    return result
