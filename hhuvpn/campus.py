"""校园网 ePortal 认证（河海大学，锐捷 ePortal）。

这一层是上游 `upstream/hhu_login.py` 的库化改写：协议细节（门卫重定向链、
`InterFace.do?method=getServices/login`、参数二次 URL 编码、gbk 页面）全部沿用上游，
改的是三件事：

  1. 先判在线再谈登录 —— 已在线时门户会把链路 302 到 `http://123.123.123.123`
     这种不可达占位地址（实测，见 docs/field-notes.md），照着跟就会卡 20 秒；
  2. 返回值是结构化 dict，供 CLI/agent 直接消费，不再只打印中文；
  3. 失败分级：账号错、验证码、接口异常、链路不可达各自有错误码。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

from .http import HttpError, HttpSession, probe_http

# 与上游保持一致
SEEDS = (
    "http://1.2.3.4/",
    "http://10.96.0.155/eportal/redirectortosuccess.jsp",
    "http://eportal.hhu.edu.cn/eportal/redirectortosuccess.jsp",
    "http://eportal.hhu.edu.cn/",
)
INDEX_URL_RE = re.compile(r"https?://[^\s'<>]*index\.jsp\?[^\s'<>]+")
SERVICE_RE = re.compile(r"selectService\('([^']*)'\s*,\s*'([^']*)'\s*,\s*'(\d+)'\)")
NAT_RE = re.compile(r'name="net_access_type"[^>]*value="([^"]*)"')

# 只在这几个主机之间跟重定向；跳出校园域说明"门户已经放行/要去外网了"
CAMPUS_HOST_SUFFIXES = ("hhu.edu.cn",)
CAMPUS_HOST_LITERALS = ("1.2.3.4", "10.96.0.155", "10.96.0.156")


def ece(s) -> str:
    """等价于页面 JS 的 encodeURIComponent 执行两次（doauthen 对所有参数如此）。"""
    safe = "-_.!~*'()"
    return urllib.parse.quote(urllib.parse.quote(str(s), safe=safe), safe=safe)


def _is_campus_host(host: str) -> bool:
    host = (host or "").lower()
    if host in CAMPUS_HOST_LITERALS:
        return True
    if any(host == s or host.endswith("." + s) for s in CAMPUS_HOST_SUFFIXES):
        return True
    # 校园内网私网地址
    if re.match(r"^(10|127)\.", host) or host.startswith("192.168.") or host.startswith("172."):
        return True
    return False


@dataclass
class CampusResult:
    action: str                     # already_online | logged_in | skipped | failed
    online: bool = False
    ok: bool = False
    error_code: str = ""
    message: str = ""
    service: str = ""
    services: list = field(default_factory=list)
    steps: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action, "ok": self.ok, "online": self.online,
            "service": self.service, "services": self.services,
            "message": self.message, "error_code": self.error_code,
            "steps": self.steps,
        }


class CampusClient:
    def __init__(self, cfg, session: HttpSession | None = None, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda *a, **k: None)
        self.s = session or HttpSession(timeout=12, tls="auto", retries=cfg.retries, logger=logger)

    # ---------------------------------------------------------------- 状态
    def online(self, timeout: float = 5.0) -> bool:
        ok, _status, _err = probe_http(self.cfg.campus_probe_url, timeout=timeout, expect=204)
        return ok

    def probe(self, timeout: float = 5.0) -> dict:
        ok, status, err = probe_http(self.cfg.campus_probe_url, timeout=timeout, expect=204)
        return {"online": ok, "probe_url": self.cfg.campus_probe_url,
                "status": status, "error": err}

    def wifi_ssid(self) -> str | None:
        if sys.platform != "win32":
            return None
        try:
            raw = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                 capture_output=True, timeout=8).stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", raw, re.M)
        if not m:
            m = re.search(r"^\s*\u65e0\u7ebf\u7f51\u7edc\u540d\u79f0|^\s*SSID", raw, re.M)
        return m.group(1).strip() if m else None

    def ssid_ok(self) -> tuple[bool, str | None]:
        want = [x.strip() for x in (self.cfg.wifi_ssid or "").replace("，", ",").split(",") if x.strip()]
        ssid = self.wifi_ssid()
        if not want or ssid is None:
            return True, ssid          # 没配 / 有线连接 → 放行（与上游一致）
        return any(w.lower() in ssid.lower() for w in want), ssid

    # ---------------------------------------------------------------- 门户链路
    def find_login_page(self, max_hops: int = 6) -> tuple[str | None, str, str]:
        """返回 (带 queryString 的登录页 URL, 页面 HTML, 备注)。"""
        for seed in SEEDS:
            url, note = seed, ""
            for _hop in range(max_hops):
                try:
                    r = self.s.get(url, allow_redirects=False, timeout=6)
                except HttpError as exc:
                    note = f"unreachable:{exc}"
                    break
                loc = r.location
                if r.status in (301, 302, 303, 307, 308) and loc:
                    nxt = urllib.parse.urljoin(url, loc)
                    host = urllib.parse.urlsplit(nxt).hostname or ""
                    if not _is_campus_host(host):
                        # 门户把已认证用户送往外部地址（如 http://123.123.123.123）：说明已放行
                        return None, "", f"left_campus:{nxt}"
                    url = nxt
                    continue
                if "index.jsp?" in url:
                    return url, r.text, note
                m = INDEX_URL_RE.search(r.text)
                if m:
                    u2 = m.group(0)
                    try:
                        r2 = self.s.get(u2, timeout=6)
                        if r2.status == 200:
                            return u2, r2.text, note
                    except HttpError:
                        pass
                    break
                break
            self.log(f"[campus] seed {seed} 未取得登录页 {note}")
        return None, "", "portal_chain_unreachable"

    def parse_services(self, html: str) -> list[tuple[str, str, str]]:
        return [(v, d, i) for v, d, i in SERVICE_RE.findall(html)]

    def fetch_services(self, query_string: str = "") -> list[tuple[str, str, str]]:
        url = self.cfg.eportal_host + "/eportal/InterFace.do?method=getServices"
        if query_string:
            url += "&queryString=" + urllib.parse.quote(query_string, safe="")
        try:
            r = self.s.post(url, data="", timeout=8)
        except HttpError as exc:
            self.log(f"[campus] getServices 失败 {exc}")
            return []
        if r.status != 200:
            return []
        try:
            return self.parse_services(r.json().get("serviceContent", ""))
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def pick_service(services, keyword: str) -> str | None:
        kw = (keyword or "").strip().lower()
        for value, display, _idx in services:
            if kw and (kw in value.lower() or kw in display.lower()):
                return value
        return None

    # ---------------------------------------------------------------- 登录
    def login(self, username: str, password: str, service_keyword: str | None = None,
              force: bool = False) -> CampusResult:
        res = CampusResult(action="failed")
        if not self.cfg.campus_enabled and not force:
            res.action, res.message, res.error_code = "skipped", "campus.enabled = false", "campus_disabled"
            return res

        if self.online():
            res.action, res.ok, res.online = "already_online", True, True
            res.message = "校园网已在线，无需认证"
            res.steps.append({"step": "probe", "status": "online"})
            return res
        res.steps.append({"step": "probe", "status": "offline"})

        ok_ssid, ssid = self.ssid_ok()
        if not ok_ssid:
            res.error_code, res.message = "wifi_gate", f"当前 WiFi「{ssid}」不在白名单，按配置不自动登录"
            res.steps.append({"step": "ssid_gate", "status": "blocked", "ssid": ssid})
            return res
        res.steps.append({"step": "ssid_gate", "status": "pass", "ssid": ssid})

        url, html, note = self.find_login_page()
        if not url:
            res.error_code = "portal_unreachable"
            res.message = ("认证门户不可达（不在校园网？或已有会话被放行）。"
                           f"详情：{note}")
            res.steps.append({"step": "find_login_page", "status": "failed", "note": note})
            return res
        qs = url.split("?", 1)[1] if "?" in url else ""
        res.steps.append({"step": "find_login_page", "status": "ok", "url": url})

        services = self.parse_services(html) or self.fetch_services(qs)
        res.services = [{"value": v, "display": d} for v, d, _i in services]
        service = self.pick_service(services, service_keyword or self.cfg.service_keyword)
        if service is None:
            m = NAT_RE.search(html)
            service = m.group(1) if m else (services[0][0] if services else "校园外网服务(out-campus NET)")
        res.service = service
        res.steps.append({"step": "pick_service", "status": "ok", "service": service})

        body = ("userId=" + ece(username)
                + "&password=" + ece(password)
                + "&service=" + ece(service)
                + "&queryString=" + ece(qs)
                + "&operatorPwd=&operatorUserId=&validcode=&passwordEncrypt=" + ece("false"))
        try:
            r = self.s.post(self.cfg.eportal_host + "/eportal/InterFace.do?method=login",
                            data=body, timeout=12)
        except HttpError as exc:
            res.error_code, res.message = "portal_unreachable", f"登录接口异常：{exc}"
            res.steps.append({"step": "login", "status": "failed", "error": str(exc)})
            return res

        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            j = {}
        result = str(j.get("result", "")).lower()
        message = str(j.get("message", ""))
        res.steps.append({"step": "login", "status": result or f"http_{r.status}",
                          "message": message[:200]})

        if r.status == 200 and result == "success":
            time.sleep(3)
            if self.online():
                res.action, res.ok, res.online = "logged_in", True, True
                res.message = "校园网认证成功，网络已恢复"
                return res
            res.error_code = "online_not_restored"
            res.message = "认证接口返回成功但网络仍不通（可能需要重新连接 WiFi）"
            return res

        if r.status == 200 and result == "fail":
            vcode = str(j.get("validCodeUrl") or "")
            if vcode or "验证码" in message:
                res.error_code = "captcha_required"
                res.message = "认证被拒：已触发验证码，请先在浏览器登录一次再重试"
            else:
                res.error_code = "auth_rejected"
                res.message = f"账号或密码被服务器拒绝：{message or r.text[:120]}"
            return res

        res.error_code = "portal_error"
        res.message = f"登录请求异常 HTTP {r.status}: {r.text[:160]}"
        return res

    def status(self, timeout: float = 5.0) -> dict:
        p = self.probe(timeout)
        ssid_ok, ssid = self.ssid_ok()
        return {"online": p["online"], "probe_url": p["probe_url"], "probe_status": p["status"],
                "wifi_ssid": ssid, "ssid_allowed": ssid_ok,
                "eportal": self.cfg.eportal_host, "enabled": self.cfg.campus_enabled}


__all__ = ["CampusClient", "CampusResult", "ece"]
