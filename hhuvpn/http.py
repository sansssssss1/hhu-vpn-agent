"""薄 HTTP 层：cookie 会话、手动重定向链、编码嗅探。

为什么不用 requests：
  - 上游项目「零依赖」的取舍；
  - 认证类抓取真正需要的恰恰是 urllib 默认不给的东西：**看得见重定向链**
    （哪一跳被弹到统一身份认证、ticket 在哪一跳下发），urllib 手动处理反而更清楚。
"""

from __future__ import annotations

import gzip
import http.cookiejar
import io
import json as _json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Iterable

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

REDIRECT_CODES = (301, 302, 303, 307, 308)

_META_CHARSET = re.compile(rb'<meta[^>]+charset=["\']?([\w-]+)', re.I)


class HttpError(RuntimeError):
    def __init__(self, message: str, url: str = "", status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


class Response:
    def __init__(self, status, headers, body: bytes, url: str, chain: list[dict] | None = None):
        self.status = status
        self.headers = headers or {}
        self.body = body or b""
        self.url = url
        self.chain = chain or []

    # ---- 头 ----
    def header(self, name: str, default: str = "") -> str:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    @property
    def location(self) -> str:
        return self.header("Location")

    @property
    def content_type(self) -> str:
        return self.header("Content-Type")

    # ---- 正文 ----
    @property
    def text(self) -> str:
        if getattr(self, "_text", None) is None:
            self._text = decode_body(self.body, self.content_type)
        return self._text

    def json(self):
        return _json.loads(self.text)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Response {self.status} {self.url[:80]} {len(self.body)}B>"


def decode_body(body: bytes, content_type: str = "") -> str:
    """尽力还原文本：HTTP 头 charset → meta charset → utf-8 → gbk。"""
    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    m2 = _META_CHARSET.search(body[:4096])
    if m2:
        try:
            candidates.append(m2.group(1).decode("ascii", "ignore"))
        except Exception:  # noqa: BLE001
            pass
    candidates += ["utf-8", "gbk", "latin-1"]
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def _decompress(body: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001
        return body
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpSession:
    """带 cookie 的会话。所有请求都自己走重定向，链路上每一跳都留痕。"""

    def __init__(self, timeout: float = 20.0, tls: str = "auto", ua: str = DEFAULT_UA,
                 retries: int = 0, logger=None):
        self.timeout = timeout
        self.tls = (tls or "auto").lower()          # auto | true | false
        self.ua = ua
        self.retries = max(0, int(retries))
        self.log = logger or (lambda *a, **k: None)
        self.jar = http.cookiejar.CookieJar()
        self._tls_verified = self.tls != "false"
        self._ctx_cache: dict[bool, ssl.SSLContext] = {}

    # ---------------------------------------------------------------- TLS
    def _context(self, verify: bool) -> ssl.SSLContext:
        if verify not in self._ctx_cache:
            if verify:
                ctx = ssl.create_default_context()
            else:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ctx_cache[verify] = ctx
        return self._ctx_cache[verify]

    def _opener(self, verify: bool):
        # 强制直连：ProxyHandler({}) 顶掉系统代理。
        # 这一条来自上游 v1.4.1 的阻断级修复（见 docs/field-notes.md §5）：
        # Clash 等代理软件开启系统代理时，urllib 默认会把发往校园门户的内网请求
        # 塞进代理，结果全部 ConnectionRefused，被误判成"不在校园网"，
        # 掉线探测还可能被代理"代答"造成假在线。校园网/WebVPN 必须反映本机真实网络。
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self._context(verify)),
            _NoRedirect(),
        )

    # ---------------------------------------------------------------- 请求
    def request(self, method: str, url: str, data=None, headers: dict | None = None,
                allow_redirects: bool = True, max_redirects: int = 8,
                timeout: float | None = None, body_bytes: bytes | None = None) -> Response:
        hdrs = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)

        payload: bytes | None = body_bytes
        if data is not None and payload is None:
            if isinstance(data, dict):
                payload = urllib.parse.urlencode(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif isinstance(data, str):
                payload = data.encode("utf-8")
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                payload = data

        chain: list[dict] = []
        current, current_method = url, method.upper()
        last_exc: Exception | None = None

        for hop in range(max_redirects + 1):
            resp = None
            for attempt in range(self.retries + 1):
                try:
                    resp = self._once(current_method, current, payload, hdrs, timeout,
                                      verify=self._tls_verified)
                    last_exc = None
                    break
                except ssl.SSLError as exc:
                    last_exc = exc
                    if self.tls == "auto" and self._tls_verified:
                        # 校园设备用自签证书很常见：严格校验失败后降级一次，但明确告警
                        self._tls_verified = False
                        self.log(f"[warn] TLS 校验失败（{exc}），已降级为不校验。"
                                 f"如要强制校验，请在 config.ini 设 webvpn.tls_verify = true")
                        continue
                    if attempt < self.retries:
                        time.sleep(0.6 * (attempt + 1))
                        continue
                except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                        http.client.HTTPException, OSError) as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt < self.retries:
                        time.sleep(0.6 * (attempt + 1))
                        continue
            if resp is None:
                raise HttpError(f"请求失败: {last_exc!r}", url=current)

            chain.append({"url": current, "method": current_method, "status": resp.status,
                          "location": resp.location})
            if allow_redirects and resp.status in REDIRECT_CODES and resp.location:
                nxt = urllib.parse.urljoin(current, resp.location)
                if resp.status == 303 or (resp.status in (301, 302) and current_method == "POST"):
                    current_method, payload = "GET", None
                    hdrs.pop("Content-Type", None)
                current = nxt
                continue
            return Response(resp.status, resp.headers, resp.body, current, chain)

        raise HttpError(f"重定向次数过多（>{max_redirects}）", url=url)

    def _once(self, method: str, url: str, payload: bytes | None, hdrs: dict,
              timeout: float | None, verify: bool):
        req = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
        opener = self._opener(verify)
        try:
            with opener.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return _RawResponse(r.status, dict(r.headers), raw)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                pass
            return _RawResponse(e.code, dict(e.headers or {}), raw)

    # ---- 便捷方法 ----
    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, data=None, **kw) -> Response:
        return self.request("POST", url, data=data, **kw)

    def head(self, url: str, **kw) -> Response:
        kw.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kw)

    # ---------------------------------------------------------------- cookie
    def cookies(self) -> list[dict]:
        out = []
        for c in self.jar:
            out.append({
                "name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/",
                "secure": bool(c.secure),
                "expires": int(c.expires) if c.expires else None,
            })
        return out

    def load_cookies(self, cookies: Iterable[dict]) -> int:
        n = 0
        for c in cookies or []:
            try:
                cookie = http.cookiejar.Cookie(
                    version=0, name=c["name"], value=c.get("value") or "",
                    port=None, port_specified=False,
                    domain=c.get("domain") or "", domain_specified=True, domain_initial_dot=False,
                    path=c.get("path") or "/", path_specified=True,
                    secure=bool(c.get("secure")), expires=c.get("expires"),
                    discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
                )
                self.jar.set_cookie(cookie)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return n

    def clear_cookies(self) -> None:
        self.jar.clear()

    def cookie_header(self, url: str) -> str:
        req = urllib.request.Request(url)
        self.jar.add_cookie_header(req)
        return req.get_header("Cookie", "")

    def cookie_value(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value or ""
        return ""

    def export_netscape(self, path, domain_filter: str | None = None) -> int:
        lines = ["# Netscape HTTP Cookie File", "# 由 hhuvpn 导出，含登录态，请勿外传", ""]
        n = 0
        for c in self.jar:
            if domain_filter and domain_filter not in (c.domain or ""):
                continue
            expires = int(c.expires) if c.expires else 0
            lines.append("\t".join([
                c.domain or "", "TRUE" if (c.domain or "").startswith(".") else "FALSE",
                c.path or "/", "TRUE" if c.secure else "FALSE",
                str(expires), c.name, c.value or "",
            ]))
            n += 1
        from pathlib import Path
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return n


class _RawResponse:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def location(self) -> str:
        for k, v in self.headers.items():
            if k.lower() == "location":
                return v
        return ""


import http.client  # noqa: E402  (放在末尾避免与上面的循环导入错觉，实际只是风格选择)


def probe_http(url: str, timeout: float = 5.0, expect: int = 204) -> tuple[bool, int | None, str]:
    """轻量探针：不跟随重定向，用于判断「是否已认证上网」。"""
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"})
    # 同上：探针也必须直连，否则代理会"代答"204 造成假在线
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(64)
            return r.status == expect, r.status, ""
    except urllib.error.HTTPError as e:
        return e.code == expect, e.code, str(e.reason)
    except Exception as e:  # noqa: BLE001
        return False, None, repr(e)


__all__ = ["HttpSession", "Response", "HttpError", "DEFAULT_UA", "decode_body",
           "probe_http", "REDIRECT_CODES"]
