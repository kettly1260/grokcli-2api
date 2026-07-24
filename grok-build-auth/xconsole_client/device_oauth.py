# -*- coding: utf-8 -*-
"""xAI OAuth2 Device Authorization Grant (SSO auto-approve).

Shared library for:
  - registration post-SSO token conversion
  - complete_build_oauth(mode="device")
  - scripts/sso_to_auth_json thin wrapper

Endpoints (HAR / production):
  POST {issuer}/oauth2/device/code
  POST {issuer}/oauth2/device/verify   body: user_code
  POST {issuer}/oauth2/device/approve  body: user_code&action=allow&principal_type=User&principal_id=
  POST {issuer}/oauth2/token           grant_type=device_code
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SCOPES,
    ISSUER,
    OAuthLoginResult,
    _finalize_oauth_token,
)

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover
    curl_requests = None  # type: ignore[assignment]

_DEVICE_FLOW_LOCK = threading.RLock()
_DEVICE_FLOW_LAST_TS = 0.0


def _default_scope_str() -> str:
    # Prefer project OIDC_SCOPES (includes conversations:*) when available so
    # registration token conversion stays behavior-compatible with legacy sso_to_auth_json.
    try:
        from grok2api.config import OIDC_SCOPES  # type: ignore
        s = str(OIDC_SCOPES or "").strip()
        if s:
            return s
    except Exception:
        pass
    env = (os.getenv("GROK2API_OIDC_SCOPES") or "").strip()
    if env:
        return env
    return " ".join(DEFAULT_SCOPES) + " conversations:read conversations:write"


def _scopes_to_str(scopes: str | list[str] | None = None) -> str:
    if scopes is None:
        return _default_scope_str()
    if isinstance(scopes, str):
        return scopes.strip() or _default_scope_str()
    return " ".join(str(s).strip() for s in scopes if str(s).strip()) or _default_scope_str()


def _device_flow_gap_sec() -> float:
    try:
        return max(0.0, float(os.getenv("GROK2API_SSO_DEVICE_GAP_SEC", "0.85") or 0.85))
    except (TypeError, ValueError):
        return 0.85


def _device_flow_retries() -> int:
    try:
        return max(1, min(16, int(os.getenv("GROK2API_SSO_DEVICE_RETRIES", "8") or 8)))
    except (TypeError, ValueError):
        return 6


def _device_flow_backoff_sec(attempt: int) -> float:
    base = 1.4 * (1.45 ** max(0, attempt - 1))
    try:
        override = os.getenv("GROK2API_SSO_DEVICE_BACKOFF_SEC")
        if override:
            base = float(override)
    except (TypeError, ValueError):
        pass
    return max(0.8, min(25.0, base))


def _wait_device_flow_slot() -> None:
    global _DEVICE_FLOW_LAST_TS
    gap = _device_flow_gap_sec()
    with _DEVICE_FLOW_LOCK:
        now = time.time()
        wait = (_DEVICE_FLOW_LAST_TS + gap) - now
        if wait > 0:
            time.sleep(wait)
        _DEVICE_FLOW_LAST_TS = time.time()


def _is_rate_limited_payload(
    text: str | None = None,
    url: str | None = None,
    status: int | None = None,
) -> bool:
    blob = f"{status or ''} {url or ''} {text or ''}".lower()
    return any(
        k in blob
        for k in (
            "slow_down",
            "rate_limited",
            "rate limit",
            "too many",
            "429",
        )
    )


def _http_timeout() -> float:
    try:
        return max(5.0, float(os.getenv("GROK2API_SSO_HTTP_TIMEOUT", "12") or 12))
    except (TypeError, ValueError):
        return 12.0


def _poll_interval_sec(raw: Any = None) -> float:
    env = (os.getenv("GROK2API_SSO_POLL_INTERVAL") or "").strip()
    if env:
        try:
            return max(0.2, min(10.0, float(env)))
        except ValueError:
            pass
    try:
        hinted = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        hinted = 1.0
    return max(0.4, min(hinted, 1.5))


def _proxy_url(proxy: str = "") -> str:
    raw = (proxy or "").strip()
    if not raw:
        raw = (
            os.getenv("GROK2API_XAI_PROXY")
            or os.getenv("GROK2API_PROXY")
            or os.getenv("GROK_CLI_PROXY")
            or ""
        ).strip()
    if "\n" in raw or "\r" in raw:
        raw = next(
            (
                ln.strip()
                for ln in raw.replace("\r", "\n").split("\n")
                if ln.strip() and not ln.strip().startswith("#")
            ),
            "",
        )
    return raw


def _proxy_kwargs(proxy: str = "") -> dict:
    """curl_cffi / requests-compatible proxy kwargs."""
    # Prefer project proxy pool when no explicit proxy is given.
    if not (proxy or "").strip():
        try:
            try:
                from grok2api.upstream.proxy_pool import (
                    resolve_proxy_for_request,
                    curl_proxies_arg,
                    get_outbound_proxy_source,
                    first_working_proxy,
                )
            except Exception:
                from proxy_pool import resolve_proxy_for_request, curl_proxies_arg  # type: ignore
                get_outbound_proxy_source = None  # type: ignore
                first_working_proxy = None  # type: ignore

            url = resolve_proxy_for_request(fallback_env=True)
            if not url and get_outbound_proxy_source is not None:
                src = get_outbound_proxy_source() or {}
                pool = list(src.get("pool") or [])
                url = pool[0] if pool else None
            if not url and first_working_proxy is not None:
                url = first_working_proxy()
            proxies = curl_proxies_arg(url)
            if proxies:
                return {"proxies": proxies}
        except Exception:
            pass
    url = _proxy_url(proxy)
    if url:
        return {"proxies": {"http": url, "https": url}}
    return {}


def _new_session(proxy: str = ""):
    if curl_requests is None:
        return None
    s = curl_requests.Session()
    # proxy applied per-request via kwargs
    _ = proxy
    return s


def request_device_code(
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str | list[str] | None = None,
    session: Any | None = None,
    proxy: str = "",
) -> dict:
    """Request OIDC device code. Raises RuntimeError on failure."""
    form = {"client_id": client_id, "scope": _scopes_to_str(scopes)}
    timeout = _http_timeout()
    retries = _device_flow_retries()
    proxy_kw = _proxy_kwargs(proxy)
    last_err = ""
    for attempt in range(1, retries + 1):
        _wait_device_flow_slot()
        if session is not None:
            try:
                r = session.post(
                    f"{ISSUER}/oauth2/device/code",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=timeout,
                    **proxy_kw,
                )
                code = int(getattr(r, "status_code", 0) or 0)
                body = (getattr(r, "text", None) or "")[:300]
                if code >= 400:
                    last_err = f"HTTP {code}: {body[:200]}"
                    if _is_rate_limited_payload(body, status=code) and attempt < retries:
                        time.sleep(_device_flow_backoff_sec(attempt))
                        continue
                    raise RuntimeError(f"device/code {last_err}")
                data = r.json()
                if not isinstance(data, dict) or not data.get("device_code"):
                    raise RuntimeError(f"device/code invalid payload: {data!r}"[:300])
                return data
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries and _is_rate_limited_payload(str(e)):
                    time.sleep(_device_flow_backoff_sec(attempt))
                    continue
                if attempt < retries:
                    time.sleep(_device_flow_backoff_sec(attempt))
                    continue
                raise RuntimeError(f"device/code: {last_err}") from e

        data_bytes = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            f"{ISSUER}/oauth2/device/code",
            data=data_bytes,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict) or not data.get("device_code"):
                raise RuntimeError(f"device/code invalid payload: {data!r}"[:300])
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            last_err = f"HTTP {e.code}: {body[:200]}"
            if _is_rate_limited_payload(body, status=e.code) and attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            raise RuntimeError(f"device/code {last_err}") from e
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            raise RuntimeError(f"device/code: {last_err}") from e
    raise RuntimeError(f"device/code exhausted retries: {last_err}")


def approve_device_login(
    *,
    user_code: str,
    sso_cookie: str | None = None,
    session: Any | None = None,
    proxy: str = "",
    verification_uri_complete: str | None = None,
) -> None:
    """verify + approve; depends on an SSO cookie session."""
    code = (user_code or "").strip()
    if not code:
        raise ValueError("user_code is required")
    timeout = _http_timeout()
    proxy_kw = _proxy_kwargs(proxy)
    own_session = False
    if session is None:
        if curl_requests is None:
            raise RuntimeError("curl_cffi is required for device approve without session")
        session = curl_requests.Session()
        own_session = True
    sso = (sso_cookie or "").strip()
    if sso:
        try:
            session.cookies.set("sso", sso, domain=".x.ai")
        except Exception:
            pass
    try:
        if verification_uri_complete:
            try:
                session.get(
                    verification_uri_complete,
                    impersonate="chrome",
                    timeout=timeout,
                    **proxy_kw,
                )
            except Exception:
                pass
        r = session.post(
            f"{ISSUER}/oauth2/device/verify",
            data={"user_code": code},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
            **proxy_kw,
        )
        if "consent" not in (getattr(r, "url", None) or ""):
            if _is_rate_limited_payload(getattr(r, "text", None), getattr(r, "url", None), getattr(r, "status_code", None)):
                raise RuntimeError(f"device/verify rate_limited: {getattr(r, 'url', '')}")
            raise RuntimeError(f"device/verify failed: {getattr(r, 'url', '')}")
        r = session.post(
            f"{ISSUER}/oauth2/device/approve",
            data={
                "user_code": code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
            **proxy_kw,
        )
        if "done" not in (getattr(r, "url", None) or ""):
            if _is_rate_limited_payload(getattr(r, "text", None), getattr(r, "url", None), getattr(r, "status_code", None)):
                raise RuntimeError(f"device/approve rate_limited: {getattr(r, 'url', '')}")
            raise RuntimeError(f"device/approve failed: {getattr(r, 'url', '')}")
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def poll_device_token(
    device_code: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    interval: float = 1.0,
    expires_in: int = 1800,
    timeout: float = 45.0,
    session: Any | None = None,
    proxy: str = "",
    immediate: bool = True,
) -> dict:
    """Exchange an approved device_code for tokens. Raises on timeout/error."""
    interval_f = _poll_interval_sec(interval)
    deadline = time.time() + min(float(expires_in or 1800), float(timeout or 45))
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }
    http_timeout = _http_timeout()
    proxy_kw = _proxy_kwargs(proxy)
    first = True
    while time.time() < deadline:
        if not (first and immediate):
            time.sleep(interval_f)
        first = False

        if session is not None:
            try:
                r = session.post(
                    f"{ISSUER}/oauth2/token",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=http_timeout,
                    **proxy_kw,
                )
                code = int(getattr(r, "status_code", 0) or 0)
                if code < 400:
                    data = r.json()
                    if isinstance(data, dict) and data.get("access_token"):
                        return data
                    raise RuntimeError(f"token empty payload: {data!r}"[:300])
                try:
                    err = r.json() if getattr(r, "content", None) else {}
                except Exception:
                    err = {}
                error = str((err or {}).get("error") or "")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval_f = min(10.0, interval_f + 1.0)
                    continue
                raise RuntimeError(f"token: {error or f'HTTP {code}'}")
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                if time.time() >= deadline:
                    raise RuntimeError(f"token network: {e}") from e
                continue

        data_bytes = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            f"{ISSUER}/oauth2/token",
            data=data_bytes,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                data = json.loads(resp.read())
            if isinstance(data, dict) and data.get("access_token"):
                return data
            raise RuntimeError(f"token empty payload: {data!r}"[:300])
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                err = {}
            error = str(err.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval_f = min(10.0, interval_f + 1.0)
                continue
            raise RuntimeError(f"token: {error or e.code}") from e
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            if time.time() >= deadline:
                raise RuntimeError(f"token network: {e}") from e
            continue
    raise RuntimeError("device token poll timed out")


def sso_to_token(
    sso_cookie: str,
    *,
    quiet: bool = False,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str | list[str] | None = None,
    proxy: str = "",
) -> dict | None:
    """SSO cookie → token dict. Returns None on failure (registration-compatible)."""
    log = (lambda *a, **k: None) if quiet else print
    sso = (sso_cookie or "").strip()
    if not sso:
        log("  ❌ empty sso")
        return None
    if curl_requests is None:
        log("  ❌ curl_cffi unavailable")
        return None

    s = curl_requests.Session()
    s.cookies.set("sso", sso, domain=".x.ai")
    timeout = _http_timeout()
    proxy_kw = _proxy_kwargs(proxy)

    try:
        r = s.get(
            "https://accounts.x.ai/",
            impersonate="chrome",
            timeout=timeout,
            **proxy_kw,
        )
    except Exception as e:  # noqa: BLE001
        log(f"  ❌ 网络错误: {e}")
        return None
    if "sign-in" in (getattr(r, "url", "") or "") or "sign-up" in (getattr(r, "url", "") or ""):
        log("  ❌ sso 无效")
        return None
    log("  ✅ sso 有效")

    retries = _device_flow_retries()
    for attempt in range(1, retries + 1):
        log(f"  🔑 Device Flow... (try {attempt}/{retries})")
        try:
            dc = request_device_code(
                client_id=client_id,
                scopes=scopes,
                session=s,
                proxy=proxy,
            )
        except Exception as e:  # noqa: BLE001
            log(f"  ❌ device/code: {e}")
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        log(f"  📋 user_code: {dc.get('user_code')}")
        try:
            approve_device_login(
                user_code=str(dc.get("user_code") or ""),
                sso_cookie=sso,
                session=s,
                proxy=proxy,
                verification_uri_complete=str(dc.get("verification_uri_complete") or "") or None,
            )
            log("  ✅ 授权确认")
        except Exception as e:  # noqa: BLE001
            log(f"  ❌ approve: {e}")
            if _is_rate_limited_payload(str(e)) and attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        try:
            poll_timeout = float(os.getenv("GROK2API_SSO_POLL_TIMEOUT", "45") or 45)
            token = poll_device_token(
                str(dc.get("device_code") or ""),
                client_id=client_id,
                interval=float(dc.get("interval") or 1),
                expires_in=int(dc.get("expires_in") or 1800),
                timeout=poll_timeout,
                session=s,
                proxy=proxy,
                immediate=True,
            )
        except Exception as e:  # noqa: BLE001
            log(f"  ❌ token: {e}")
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        log(
            f"  ✅ access_token (expires_in={token.get('expires_in')}s)"
            + (" + refresh_token" if token.get("refresh_token") else "")
        )
        return token
    return None


def login_with_device(
    *,
    sso_cookie: str,
    email: str = "",
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str | list[str] | None = None,
    proxy: str = "",
    output_dir: Optional[str | Path] = None,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    cliproxyapi_disabled: bool = False,
) -> OAuthLoginResult:
    """SSO → device full flow → OAuthLoginResult."""
    sso = (sso_cookie or "").strip()
    if not sso:
        raise RuntimeError("device mode requires sso cookie")
    token = sso_to_token(
        sso,
        quiet=False,
        client_id=client_id,
        scopes=scopes,
        proxy=proxy,
    )
    if not token or not token.get("access_token"):
        raise RuntimeError("device OAuth failed: no access_token from device flow")
    # Prefer email from caller when userinfo is sparse.
    result = _finalize_oauth_token(
        token,
        client_id=client_id,
        proxy=proxy,
        output_dir=output_dir,
        cliproxyapi_auth_dir=cliproxyapi_auth_dir,
        cliproxyapi_base_url=cliproxyapi_base_url,
        cliproxyapi_disabled=cliproxyapi_disabled,
        redirect_uri="",
    )
    if email and not result.userinfo.get("email"):
        try:
            result.userinfo["email"] = email
        except Exception:
            pass
    return result
