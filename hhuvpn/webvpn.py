"""河海大学 WebVPN（网瑞达 wEngine）客户端。

一条链路串起来（实测见 docs/field-notes.md）：

    GET  https://webvpn.hhu.edu.cn/
      → 302 到 /https/<sig>/authserver/login?service=https%3A%2F%2Fwebvpn.hhu.edu.cn%2Flogin%3Fcas_login%3Dtrue
      → 统一身份认证（学号 + 密码，AES 加密密码字段）
      → 302 回 https://webvpn.hhu.edu.cn/login?cas_login=true&ticket=ST-xxx
      → 下发 wengine_vpn_ticketwebvpn_hhu_edu_cn，后续所有资源走：
         https://webvpn.hhu.edu.cn/https/<sig(host)>/<path>?<query>

WebVPN 的网址签名（<= 本模块最值钱的一段）：
    sig(host) = hex("wrdvpnisthebest!") + hex(AES-128-CFB(key=iv="wrdvpnisthebest!", host))
用真实抓到的 CAS 跳转地址反推验证通过，见 tests/test_aes.py。
"""

from __future__ import annotations

import binascii
import html as _html
import json
import re
import time
import urllib.parse

from .aes import cfb128_decrypt, cfb128_encrypt, encrypt_cas_password
from .http import HttpError, HttpSession

SIGN_KEY = b"wrdvpnisthebest!"
KEY_HEX = SIGN_KEY.hex()

TICKET_COOKIE = "wengine_vpn_ticketwebvpn_hhu_edu_cn"

RE_SALT = re.compile(r'id="pwdEncryptSalt"[^>]*value="([^"]*)"')
RE_EXECUTION = re.compile(r'name="execution"[^>]*value="([^"]*)"')
RE_LT = re.compile(r'name="lt"[^>]*value="([^"]*)"')
RE_CAPTCHA_SWITCH = re.compile(r'captchaSwitch\s*=\s*"([^"]*)"')
RE_SERVICE_VAR = re.compile(r"var service\s*=\s*(\[[^\]]*\])")
RE_FORM_ACTION = re.compile(r'<form[^>]+id="loginFromId"[^>]+action="([^"]+)"')
RE_ERR_TIP = re.compile(r'id="showErrorTip"[^>]*>(.*?)</span>', re.S)
RE_VPN_LINK = re.compile(r'(?:href|src|action)="(/(?:https?|wss?)/[0-9a-fA-F]{32,}/[^"]*)"')

# 页面上的错误提示有时是 i18n key，这里给出常见映射（够用即可，未知就原样回传）
KNOWN_ERRORS = {
    "usernameError": "账号不存在或格式不正确",
    "passwordError": "密码错误",
    "captchaError": "验证码错误",
    "userLocked": "账号已被锁定",
    "accountDisabled": "账号已禁用",
    "loginError": "用户名或密码错误",
}


def sign_host(host: str) -> str:
    """WebVPN 主机名签名（确定性：固定 IV = key）。"""
    ct = cfb128_encrypt(SIGN_KEY, SIGN_KEY, host.encode("utf-8"))
    return KEY_HEX + ct.hex()


def vpn_url(base: str, target: str) -> str:
    """把真实网址映射成 WebVPN 网址。"""
    p = urllib.parse.urlsplit(target)
    scheme = "https" if p.scheme in ("", "https") else p.scheme
    host = p.hostname or ""
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = p.path or "/"
    query = f"?{p.query}" if p.query else ""
    fragment = f"#{p.fragment}" if p.fragment else ""
    return f"{base.rstrip('/')}/{scheme}/{sign_host(host)}{port}{path}{query}{fragment}"


def unwrap_vpn_url(base: str, url: str) -> dict | None:
    """WebVPN 网址反解回真实网址（用于把学校资源列表变成可读清单）。"""
    p = urllib.parse.urlsplit(url)
    m = re.match(r"^/(https?)/([0-9a-fA-F]{32,})(.*)$", p.path)
    if not m:
        return None
    scheme, sig, rest = m.group(1), m.group(2), m.group(3)
    try:
        raw = binascii.unhexlify(sig)
    except binascii.Error:
        return None
    host = cfb128_decrypt(SIGN_KEY, SIGN_KEY, raw[16:]).decode("utf-8", "replace")
    if not host:
        return None
    target = f"{scheme}://{host}{rest}"
    if p.query:
        target += "?" + p.query
    return {"target": target, "host": host, "scheme": scheme, "path": rest or "/",
            "vpn_url": url}


def _strip_tags(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


class WebVPNClient:
    def __init__(self, cfg, session: HttpSession | None = None, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda *a, **k: None)
        self.s = session or HttpSession(timeout=20, tls=cfg.tls_verify, retries=cfg.retries,
                                        logger=logger)
        self.base = cfg.webvpn_base

    # ---------------------------------------------------------------- 映射
    def to_vpn(self, target: str) -> str:
        return vpn_url(self.base, target)

    def from_vpn(self, url: str) -> dict | None:
        return unwrap_vpn_url(self.base, url)

    # ---------------------------------------------------------------- 状态
    def ticket(self) -> str:
        return self.s.cookie_value(TICKET_COOKIE)

    def is_authenticated(self, timeout: float = 15) -> tuple[bool, dict]:
        """访问门户首页：被弹到 /authserver/ 就是没登录，否则视为已登录。"""
        try:
            r = self.s.get(self.base + "/", allow_redirects=True, max_redirects=6, timeout=timeout)
        except HttpError as exc:
            return False, {"error": str(exc), "reason": "unreachable"}
        final = r.url or ""
        info = {"final_url": final, "status": r.status, "ticket": self.ticket()[:32]}
        if "/authserver/" in final:
            return False, {**info, "reason": "redirected_to_cas"}
        if "webvpn" not in urllib.parse.urlsplit(final).hostname and "webvpn" not in final:
            return False, {**info, "reason": "left_webvpn"}
        return True, {**info, "reason": "portal_ok"}

    def status(self, deep: bool = True) -> dict:
        out = {"base": self.base, "enabled": self.cfg.webvpn_enabled,
               "has_ticket": bool(self.ticket()),
               "ticket_preview": self.ticket()[:24],
               "cookie_count": len(self.s.cookies())}
        if deep:
            ok, info = self.is_authenticated()
            out.update({"authenticated": ok, "evidence": info})
        return out

    # ---------------------------------------------------------------- 登录
    def _read_login_form(self, html: str, login_url: str) -> dict:
        salt = (RE_SALT.search(html) or [None, ""])[1]
        execution = (RE_EXECUTION.search(html) or [None, "e1s1"])[1]
        lt = (RE_LT.search(html) or [None, ""])[1]
        switch = (RE_CAPTCHA_SWITCH.search(html) or [None, ""])[1]
        action = (RE_FORM_ACTION.search(html) or [None, ""])[1]
        service = ""
        m = RE_SERVICE_VAR.search(html)
        if m:
            try:
                arr = json.loads(m.group(1))
                service = arr[0] if arr else ""
            except Exception:  # noqa: BLE001
                service = ""
        if not service:
            service = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query).get("service", [""])[0]
        return {"salt": salt, "execution": execution, "lt": lt,
                "captcha_switch": switch, "action": action, "service": service,
                "login_url": login_url}

    def _prefix_of(self, url: str) -> str:
        """从 .../https/<sig>/authserver/login 取出 .../https/<sig> 前缀。"""
        i = url.find("/authserver/")
        if i < 0:
            return self.base.rstrip("/")
        return urllib.parse.urljoin(url, url[:i])

    def _extract_error(self, html: str) -> str:
        for rx in (RE_ERR_TIP, re.compile(r'id="msg"[^>]*>(.*?)<', re.S),
                   re.compile(r'"message"\s*:\s*"([^"]{2,200})"'),
                   re.compile(r'alert\([\x27\x22]([^\x27\x22]{2,200})[\x27\x22]\)')):
            m = rx.search(html)
            if m:
                text = _strip_tags(m.group(1))
                if text:
                    return KNOWN_ERRORS.get(text, text)
        for key, zh in KNOWN_ERRORS.items():
            if key in html:
                return zh
        for kw in ("用户名或密码错误", "密码错误", "账号或密码", "验证码错误", "用户名不存在", "已锁定"):
            if kw in html:
                return kw
        return ""

    def login_with_credentials(self, username: str, password: str, captcha: str = "",
                               debug_dump_dir=None, timeout: float = 25) -> dict:
        """走完 CAS 并拿到 ticket。返回结构化结果，不抛异常（除非网络层彻底炸）。"""
        started = time.time()
        result = {"ok": False, "action": "failed", "error_code": "", "message": "",
                  "username": username, "steps": [], "authenticated": False}

        def step(name, phase, **extra):
            # 参数名用 phase：调用点还会传 status=HTTP 状态码，不能重名
            result["steps"].append({"step": name, "status": phase,
                                    "t": round(time.time() - started, 2), **extra})

        try:
            r = self.s.get(self.base + "/", allow_redirects=True, max_redirects=8, timeout=timeout)
        except HttpError as exc:
            result.update(error_code="webvpn_unreachable",
                          message=f"WebVPN 不可达：{exc}")
            step("open_portal", "failed", error=str(exc))
            return result
        step("open_portal", "ok", status=r.status, url=r.url)

        if "/authserver/" not in (r.url or ""):
            ok, info = self.is_authenticated()
            result.update(ok=ok, authenticated=ok,
                          action="already_logged_in" if ok else "failed",
                          error_code="" if ok else "unknown_state",
                          message="WebVPN 会话已有效，无需重新登录" if ok else "WebVPN 状态异常",
                          evidence=info)
            step("detect", "already_authenticated" if ok else "unexpected_page")
            return result

        form = self._read_login_form(r.text, r.url)
        step("read_login_form", "ok" if form["salt"] else "missing_salt",
             captcha_switch=form["captcha_switch"], lt=bool(form["lt"]))
        if not form["salt"]:
            result.update(error_code="login_page_unexpected",
                          message="登录页结构变化：未找到 pwdEncryptSalt，请更新插件或改用 --browser")
            return result

        prefix = self._prefix_of(r.url)
        need_captcha = False
        if form["captcha_switch"] != "0":
            try:
                cr = self.s.get(f"{prefix}/authserver/checkNeedCaptcha.htl",
                                headers={"Referer": r.url}, timeout=timeout)
                if "username" not in cr.url:  # 没被改写就直接拼参数
                    cr = self.s.get(f"{prefix}/authserver/checkNeedCaptcha.htl?username="
                                    + urllib.parse.quote(username),
                                    headers={"Referer": r.url}, timeout=timeout)
                need_captcha = bool(cr.json().get("isNeed"))
                step("check_need_captcha", "need" if need_captcha else "not_need", raw=cr.text[:120])
            except Exception as exc:  # noqa: BLE001
                step("check_need_captcha", "unknown", error=str(exc)[:120])

        if need_captcha and not captcha:
            detail = {"captcha_switch": form["captcha_switch"]}
            if form["captcha_switch"] == "1":
                # 图形验证码：把图片取回来存盘，人可以看着图片把验证码回填
                try:
                    img = self.s.get(f"{prefix}/authserver/getCaptcha.htl?{int(time.time()*1000)}",
                                     headers={"Referer": r.url}, timeout=timeout)
                    if debug_dump_dir:
                        from pathlib import Path
                        p = Path(debug_dump_dir) / "captcha.png"
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(img.body)
                        detail["captcha_image"] = str(p)
                except Exception as exc:  # noqa: BLE001
                    detail["captcha_image_error"] = str(exc)[:120]
                msg = "本次登录需要图形验证码：请看 captcha_image 并把验证码用 --captcha 传回"
            else:
                msg = ("本次登录需要滑块验证码（captchaSwitch=2），脚本无法自动完成。"
                       "请改用 hhuvpn login --browser 在浏览器里过一次，或稍后重试")
            result.update(error_code="captcha_required", message=msg, captcha=detail)
            step("captcha", "required", **detail)
            return result

        post_url = prefix + "/authserver/login"
        if form["service"]:
            post_url += "?service=" + urllib.parse.quote(form["service"], safe="")
        payload = {
            "username": username,
            "password": encrypt_cas_password(password, form["salt"]),
            "captcha": captcha,
            "_eventId": "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "execution": form["execution"],
            "lt": form["lt"],
            "rmShown": "1",
        }
        try:
            pr = self.s.post(post_url, data=payload, allow_redirects=False,
                             headers={"Referer": r.url, "Origin": self.base,
                                      "Upgrade-Insecure-Requests": "1"},
                             timeout=timeout)
        except HttpError as exc:
            result.update(error_code="login_request_failed", message=f"登录请求异常：{exc}")
            step("post_login", "failed", error=str(exc))
            return result
        step("post_login", str(pr.status), location=(pr.location or "")[:160])

        if debug_dump_dir:
            from pathlib import Path
            d = Path(debug_dump_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / "login_response.html").write_text(pr.text, encoding="utf-8")
            (d / "login_response_meta.json").write_text(
                json.dumps({"status": pr.status, "headers": {k: str(v) for k, v in pr.headers.items()},
                            "url": pr.url, "chain": pr.chain}, ensure_ascii=False, indent=2),
                encoding="utf-8")

        # 成功：302 回 webvpn（带 ticket）
        if pr.status in (301, 302, 303, 307, 308) and pr.location:
            try:
                follow = self.s.get(urllib.parse.urljoin(post_url, pr.location),
                                    allow_redirects=True, max_redirects=8, timeout=timeout)
                step("follow_ticket", str(follow.status), url=follow.url[:160])
            except HttpError as exc:
                step("follow_ticket", "failed", error=str(exc)[:160])
            ok, info = self.is_authenticated()
            result.update(ok=ok, authenticated=ok,
                          action="logged_in" if ok else "failed",
                          error_code="" if ok else "ticket_not_accepted",
                          message="WebVPN 登录成功" if ok else "拿到 ticket 但门户仍要求认证",
                          evidence=info)
            return result

        # 失败：401 / 重新渲染登录页
        err = self._extract_error(pr.text)
        is_login_page = 'id="pwdEncryptSalt"' in pr.text or "loginFromId" in pr.text
        if pr.status == 401 or is_login_page:
            result.update(error_code="auth_rejected",
                          message=f"统一身份认证被拒（HTTP {pr.status}）"
                                  + (f"：{err}" if err else "：请检查学号/密码"))
        else:
            result.update(error_code="login_unexpected",
                          message=f"登录返回异常 HTTP {pr.status}：{_strip_tags(pr.text)[:150]}")
        step("evaluate", result["error_code"], error_text=err)
        return result

    # ---------------------------------------------------------------- 资源
    def fetch(self, target: str, path: str | None = None, method: str = "GET",
              headers: dict | None = None, allow_redirects: bool = True,
              timeout: float = 30, max_bytes: int | None = None) -> tuple[object, str]:
        """通过 WebVPN 会话访问目标站点。target 可以是域名或完整 URL。"""
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        else:
            url = f"https://{target}"
        if path:
            url = url.rstrip("/") + "/" + path.lstrip("/")
        vpn = self.to_vpn(url)
        hdrs = {"Referer": self.base + "/", **(headers or {})}
        resp = self.s.request(method, vpn, headers=hdrs, allow_redirects=allow_redirects,
                              timeout=timeout)
        if max_bytes:
            resp.body = resp.body[:max_bytes]
        return resp, vpn

    def portal_resources(self, timeout: float = 25) -> list[dict]:
        """解析 WebVPN 门户里的资源列表（学校配好的数据库入口）。"""
        try:
            r = self.s.get(self.base + "/", timeout=timeout, allow_redirects=True)
        except HttpError:
            return []
        seen, out = set(), []
        for m in RE_VPN_LINK.finditer(r.text):
            full = urllib.parse.urljoin(self.base, m.group(1))
            if full in seen:
                continue
            seen.add(full)
            info = self.from_vpn(full)
            if info:
                out.append({**info, "vpn_url": full})
        return out

    def logout(self, timeout: float = 15) -> dict:
        for path in ("/logout", "/login/logout", "/wengine-vpn/logout"):
            try:
                r = self.s.get(self.base + path, timeout=timeout, allow_redirects=True)
                if r.status < 400:
                    return {"ok": True, "path": path, "status": r.status}
            except HttpError:
                continue
        return {"ok": False, "message": "未找到可用的注销入口（会话 cookie 已清）"}


__all__ = ["WebVPNClient", "sign_host", "vpn_url", "unwrap_vpn_url", "TICKET_COOKIE", "SIGN_KEY"]
