"""编排层：把「上校园网 → 登 WebVPN → 确认能进目标库」串成一个幂等动作。

agent 只需要调一个 ensure()：
  - 已在线就不动校园网；
  - 已有有效 WebVPN 会话（本地缓存 cookie 通过线上校验）就不重新登录；
  - 只对本次请求的目标库做校验；
  - 每一步都留结构化痕迹，失败给出错误码 + 可执行建议，不做静默降级。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import databases as db_catalog
from .campus import CampusClient
from .config import Config, Credentials, resolve_credentials
from .http import HttpError, HttpSession
from .state import State
from .verify import calibrate, calibrated_verify, verify_page
from .webvpn import WebVPNClient

def _rank(result: dict) -> int:
    """结果好坏排序：已认证 > 可达但未知 > 不可达。用于多通道择优。"""
    if result.get("authenticated") is True:
        return 2
    return 1 if result.get("reachable") else 0


CODE_HINTS = {
    "credentials_missing": "在 config.ini 填 [account] username/password，或设环境变量 HHU_USERNAME/HHU_PASSWORD",
    "auth_rejected": "检查学号/密码（校园网与 WebVPN 是同一账号，只需改一处）；"
                     "密码错累计会触发验证码，必要时先在浏览器登录一次",
    "captcha_required": "需要验证码：图形验证码可 --captcha 回填；滑块验证码请用 hhuvpn login --browser",
    "webvpn_unreachable": "确认能访问 https://webvpn.hhu.edu.cn（校外也可访问；如路由器/代理拦截请放行）",
    "portal_unreachable": "不在校园网内或门卫未劫持；连上 Hohai University 无线后再试",
    "campus_disabled": "config.ini [campus] enabled = false 时不会自动登录校园网",
    "db_not_authenticated": "WebVPN 已登录但该库仍要求认证：确认学校是否订购了该库，或换用 db calibrate 复核",
}


class Connector:
    def __init__(self, cfg: Config, state: State | None = None, verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose
        self.state = state or State(cfg.state_dir, logger=self._log if verbose else None)
        self.session = HttpSession(timeout=25, tls=cfg.tls_verify, retries=cfg.retries,
                                   logger=self._log if verbose else None)
        self.campus = CampusClient(cfg, session=self.session,
                                   logger=self._log if verbose else None)
        self.webvpn = WebVPNClient(cfg, session=self.session,
                                   logger=self._log if verbose else None)
        self._catalog, self.defaults = db_catalog.load_catalog(self.state.dir)
        self._session_loaded = False

    # ---------------------------------------------------------------- 基础设施
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def load_session(self) -> dict:
        data = self.state.load_session()
        n = self.session.load_cookies(data.get("cookies") or [])
        self._session_loaded = True
        return {"cookies_loaded": n, "saved_at_iso": data.get("saved_at_iso", ""),
                "meta": data.get("meta", {})}

    def save_session(self, meta: dict | None = None) -> Path:
        base_meta = {"webvpn_base": self.cfg.webvpn_base,
                     "saved_by": "hhuvpn",
                     "username": (meta or {}).get("username") or self.cfg.username}
        return self.state.save_session(self.session.cookies(), {**base_meta, **(meta or {})})

    def session_age_minutes(self) -> float | None:
        data = self.state.load_session()
        if not data.get("saved_at"):
            return None
        return round((time.time() - float(data["saved_at"])) / 60.0, 1)

    # ---------------------------------------------------------------- 各层状态
    def campus_status(self) -> dict:
        return self.campus.status()

    def webvpn_status(self, deep: bool = True, use_cache: bool = True) -> dict:
        if use_cache and not self._session_loaded:
            info = self.load_session()
            self._log(f"[state] 载入本地会话 cookie {info['cookies_loaded']} 条"
                      f"（{info.get('saved_at_iso') or '无记录'}）")
        st = self.webvpn.status(deep=deep)
        st["session_age_minutes"] = self.session_age_minutes()
        st["session_ttl_minutes"] = self.cfg.session_ttl_minutes
        return st

    def _fetch_direct(self, db):
        """校园网直连（不走 WebVPN）：校内大量数据库是按出口 IP 授权的。"""
        return self.session.get(db.url, timeout=35), ""

    def database_status(self, alias: str, save: bool = True, via: str = "webvpn") -> dict:
        """确认某个库是否已可用。

        正常逻辑（默认）：**校园网 → WebVPN → 数据库**，所以默认 via="webvpn"，
        即"通过学校 WebVPN 进数据库"这条正道。

        via 取值：
          - "webvpn"（默认）：只走 WebVPN——正常流程；
          - "auto"：先 WebVPN，不行再试校内 IP 直连（备用兜底）；
          - "direct"：只走校内直连——**备用通道**，不是正常流程。

        结果里的 channel 字段标明是哪条通道判定的（webvpn / direct）。
        """
        db = db_catalog.get(alias, self.state.dir)
        if db is None:
            return {"alias": alias, "error": "unknown_database",
                    "message": f"目录里没有「{alias}」，可用 hhuvpn db list 查看"}

        # 顺序即优先级：正常逻辑永远是 WebVPN 在前，校内直连只在 --via auto 时兜底
        if via == "direct":
            channels = ["direct"]
        elif via == "auto":
            channels = (["webvpn"] if self.cfg.webvpn_enabled else []) + ["direct"]
        else:                                   # "webvpn"（默认）
            channels = ["webvpn"]

        fp = self.state.get_fingerprint(db.alias)
        if fp.get("markers"):
            fp = {**fp, "verify": calibrated_verify(fp)}

        started = time.time()
        best: dict | None = None
        for channel in channels:
            try:
                if channel == "direct":
                    resp, vpn_url = self._fetch_direct(db)
                else:
                    target = db.host if db.url.rstrip("/") == db.host else db.url
                    resp, vpn_url = self.webvpn.fetch(target, timeout=35)
            except HttpError as exc:
                cand = {"alias": db.alias, "name": db.name, "target": db.url, "reachable": False,
                        "authenticated": False, "channel": channel,
                        "error": "db_unreachable", "message": str(exc)}
            else:
                resp.vpn_url = vpn_url
                cand = verify_page(db, resp, self.cfg, fingerprint=fp, defaults=self.defaults)
                cand["channel"] = channel
            cand["elapsed_ms"] = int((time.time() - started) * 1000)
            if cand.get("authenticated") is True:
                best = cand
                break
            # 记账：直连"未知"、WebVPN"已认证"这类情况，要保留更有信息量的那个
            if best is None or _rank(cand) > _rank(best):
                best = cand

        out = best or {"alias": db.alias, "reachable": False, "authenticated": False,
                       "error": "db_unreachable", "message": "没有可用通道"}
        if save:
            self.state.save_status({"last_db_check": out})
        return out

    # ---------------------------------------------------------------- 登录动作
    def ensure_campus(self, creds: Credentials, force: bool = False) -> dict:
        if self.campus.online():
            return {"name": "campus", "action": "already_online", "ok": True,
                    "message": "已能正常上网（204 探针通过）"}
        if not creds.complete:
            return {"name": "campus", "action": "skipped", "ok": False,
                    "error_code": "credentials_missing",
                    "message": "检测到未上网，但没有可用账号密码，无法自动认证"}
        res = self.campus.login(creds.username, creds.password, force=force)
        out = {"name": "campus", **res.to_dict()}
        self.state.log(f"campus login: {out['action']} {out.get('error_code','')}")
        return out

    def ensure_webvpn(self, creds: Credentials, deep: bool = True, force: bool = False,
                      by: str = "unknown") -> dict:
        """确保 WebVPN 会话可用（必要时登录）。

        by 是审计用标记：谁会开 VPN 是这个功能的核心契约（守护不该开），
        所以每次调用都记进日志，出问题能直接看出是谁。
        """
        if not self.cfg.webvpn_enabled:
            return {"name": "webvpn", "action": "skipped", "ok": False,
                    "error_code": "webvpn_disabled", "message": "config.ini [webvpn] enabled = false"}
        if not self._session_loaded:
            self.load_session()
        self.state.log(f"ensure_webvpn(by={by}) intent={self.state.webvpn_intent().get('intent')}")
        if deep and not force:
            ok, info = self.webvpn.is_authenticated()
            if ok:
                # 会话有效即视为"使用者要开着"，之后由守护负责保活（直到 vpn off 或关机）
                self.state.set_webvpn_intent(True, by="session_valid")
                return {"name": "webvpn", "action": "already_logged_in", "ok": True,
                        "message": "本地 WebVPN 会话仍然有效", "evidence": info}
        if not creds.complete:
            return {"name": "webvpn", "action": "skipped", "ok": False,
                    "error_code": "credentials_missing",
                    "message": "WebVPN 会话不可用，但没有可用账号密码，无法自动登录"}
        res = self.webvpn.login_with_credentials(creds.username, creds.password)
        if res.get("ok"):
            self.save_session({"username": creds.username})
            self.state.set_webvpn_intent(True, by="login")
            self.state.log("webvpn login ok")
        else:
            self.state.log(f"webvpn login failed: {res.get('error_code')} {res.get('message')}")
        return {"name": "webvpn", **res}

    # ---------------------------------------------------------------- 主流程
    def ensure(self, targets=None, allow_login: bool = True, verify_dbs: bool = True,
               force: bool = False, via: str = "webvpn") -> dict:
        """正常逻辑：校园网 → WebVPN → 数据库。

        via 只影响"最后一步怎么进库"以及失败处理：
          - "webvpn"（默认）：必须有 WebVPN，再通过 WebVPN 进库；WebVPN 拿不到就是失败；
          - "auto"：仍然先走 WebVPN，走不通时允许用校内直连兜底（结果里 channel=direct）；
          - "direct"：明确只用备用通道（校内直连），不要求 WebVPN。
        """
        started = time.time()
        targets = db_catalog.split_aliases(targets) or self.cfg.default_databases
        creds = resolve_credentials(self.cfg, allow_prompt=False)
        report = {"ok": False, "steps": [], "databases": [], "ready": [], "blocked": [],
                  "credentials": creds.to_dict(),
                  "targets": targets,
                  "config": str(self.cfg.path),
                  "state_dir": str(self.state.dir)}

        campus_step = self.ensure_campus(creds, force=force)
        report["steps"].append(campus_step)

        # 校园网与 WebVPN 是**同一个账号**（同一套学号/信息门户密码）。所以：
        # 校园网那边已经因为凭据被拒/要验证码，就不要拿同一份密码再去撞 WebVPN——
        # 两边都撞只会把失败次数翻倍，更容易把账号推进滑块验证码。
        creds_rejected = campus_step.get("error_code") in ("auth_rejected", "captcha_required")
        report["account_scope"] = "campus+webvpn（同一账号）"

        if via == "direct":
            webvpn_step = {"name": "webvpn", "action": "skipped", "ok": False,
                           "error_code": "skipped_by_request",
                           "message": "--via direct：按请求跳过 WebVPN，只用校内直连（备用通道）"}
        elif creds_rejected:
            webvpn_step = {
                "name": "webvpn", "action": "skipped", "ok": False,
                "error_code": campus_step.get("error_code"),
                "message": ("校园网认证已被拒（" + str(campus_step.get("error_code")) +
                            "）。校园网与 WebVPN 是同一账号，同一份密码再试 WebVPN 也不会通过，"
                            "因此本次跳过，避免把失败次数翻倍触发验证码。"),
                "shared_credentials": True}
        else:
            webvpn_step = self.ensure_webvpn(creds, force=force, by="ensure")
        report["steps"].append(webvpn_step)
        report["webvpn_ok"] = bool(webvpn_step.get("ok"))
        report["via"] = via

        # 正常逻辑：校园网 → WebVPN → 数据库。WebVPN 没登上 = 这条路走不通，
        # 直接判失败（不拿备用通道的成功去掩饰主流程的失败）。
        # 只有明确 --via auto 时才允许兜底；--via direct 是显式选的备用模式。
        if not webvpn_step.get("ok") and via == "webvpn":
            code = webvpn_step.get("error_code") or "webvpn_unavailable"
            report["ok"] = False
            report["error"] = {"code": code, "message": webvpn_step.get("message", ""),
                               "hint": CODE_HINTS.get(code, "")}
            if verify_dbs:
                fallback = [self.database_status(a, save=False, via="direct") for a in targets]
                report["fallback"] = [
                    {"alias": f.get("alias"), "channel": "direct",
                     "authenticated": f.get("authenticated"),
                     "note": f.get("note", "") or f.get("message", "")}
                    for f in fallback]
                report["fallback_ready"] = [f["alias"] for f in report["fallback"]
                                            if f.get("authenticated") is True]
                if report["fallback_ready"]:
                    report["note"] = ("主流程（WebVPN）未走通；备用通道（校内 IP 直连）"
                                      "目前可用的库：" + "、".join(report["fallback_ready"])
                                      + "。仅作备选，不改变本次判定为失败。")
            report["elapsed_ms"] = int((time.time() - started) * 1000)
            report["next_actions"] = self._next_actions(report)
            self.state.save_status({"last_ensure": report})
            return report

        if verify_dbs:
            for alias in targets:
                out = self.database_status(alias, via=via)
                report["databases"].append(out)
                if out.get("authenticated") is True:
                    report["ready"].append(out["alias"])
                elif out.get("reachable"):
                    report["blocked"].append({"alias": out.get("alias"),
                                              "reason": "not_authenticated",
                                              "detail": out.get("note", "")})
                else:
                    report["blocked"].append({"alias": out.get("alias"),
                                              "reason": out.get("error", "unreachable"),
                                              "detail": out.get("message", "") or out.get("note", "")})

        if verify_dbs:
            report["ok"] = bool(report["ready"])
            report["channels"] = {d.get("alias"): d.get("channel") for d in report["databases"]}
            if not report["ready"]:
                if not webvpn_step.get("ok") and via == "auto":
                    code = webvpn_step.get("error_code") or "webvpn_unavailable"
                    report["error"] = {"code": code, "message": webvpn_step.get("message", ""),
                                       "hint": CODE_HINTS.get(code, "")}
                elif via == "direct":
                    report["error"] = {"code": "db_not_authenticated",
                                       "message": "校内直连（备用通道）下目标库未确认到机构身份",
                                       "hint": CODE_HINTS["db_not_authenticated"]}
                else:
                    report["error"] = {"code": "db_not_authenticated",
                                       "message": "WebVPN 已登录，但目标库未确认到机构身份",
                                       "hint": CODE_HINTS["db_not_authenticated"]}
            elif via == "auto" and not webvpn_step.get("ok"):
                report["note"] = ("WebVPN 未走通（" + str(webvpn_step.get("error_code")) +
                                  "），以下库是靠备用通道（校内 IP 直连）确认的："
                                  + "、".join(report["ready"]))
        else:
            report["ok"] = bool(webvpn_step.get("ok")) or via == "direct"
            if not report["ok"]:
                code = webvpn_step.get("error_code") or "webvpn_unavailable"
                report["error"] = {"code": code, "message": webvpn_step.get("message", ""),
                                   "hint": CODE_HINTS.get(code, "")}
        report["elapsed_ms"] = int((time.time() - started) * 1000)
        report["next_actions"] = self._next_actions(report)
        self.state.save_status({"last_ensure": report})
        return report

    @staticmethod
    def _next_actions(report: dict) -> list[str]:
        acts = []
        err = report.get("error") or {}
        if err.get("hint"):
            acts.append(err["hint"])
        for step in report.get("steps", []):
            if step.get("error_code") in ("captcha_required", "auth_rejected"):
                acts.append(CODE_HINTS.get(step["error_code"], ""))
        if report.get("ready"):
            acts.append("已可用：用 hhuvpn url <库别名> 取 WebVPN 入口，或 hhuvpn fetch <别名> <路径> 走会话取页面")
        return [a for a in acts if a]

    # ---------------------------------------------------------------- WebVPN 开关
    def close_webvpn(self, reason: str = "", remote: bool = True) -> dict:
        """关掉 WebVPN：远端注销（尽力而为）+ 清本地会话 cookie。

        「按需开启」模型下，三条路径都会走到这里：
        使用者主动 vpn off、开机后首次守护运行、以及 logout。
        """
        if not self._session_loaded:
            self.load_session()
        had = bool(self.webvpn.ticket())
        logout_result: dict = {}
        if had and remote:
            try:
                logout_result = self.webvpn.logout()
            except Exception as exc:  # noqa: BLE001
                logout_result = {"ok": False, "message": repr(exc)}
        self.session.clear_cookies()
        self.state.clear_session()
        self.state.set_webvpn_intent(False, by=reason or "close")
        self._session_loaded = True
        self.state.log(f"webvpn closed: had_session={had} reason={reason}")
        return {"closed": True, "had_session": had, "remote_logout": logout_result,
                "reason": reason}

    # ---------------------------------------------------------------- 便捷动作
    def db_url(self, alias: str) -> dict:
        db = db_catalog.get(alias, self.state.dir)
        if db is None:
            return {"ok": False, "error": "unknown_database", "alias": alias}
        return {"ok": True, "alias": db.alias, "name": db.name, "target": db.url,
                "vpn_url": self.webvpn.to_vpn(db.url)}

    def fetch(self, target: str, path: str | None = None, max_bytes: int | None = None) -> dict:
        db = db_catalog.get(target, self.state.dir)
        if db is not None and path is None:
            target, path = db.url, None
        if not self._session_loaded:
            self.load_session()
        resp, vpn_url = self.webvpn.fetch(target, path=path, max_bytes=max_bytes)
        return {
            "ok": resp.status < 400, "status": resp.status, "target": target,
            "vpn_url": vpn_url, "final_url": resp.url, "bytes": len(resp.body),
            "content_type": resp.content_type,
        }

    def calibrate_db(self, alias: str) -> dict:
        db = db_catalog.get(alias, self.state.dir)
        if db is None:
            return {"ok": False, "error": "unknown_database", "alias": alias}
        resp, vpn_url = self.webvpn.fetch(db.host, path=None)
        fp = calibrate(db, resp, self.cfg)
        fp["vpn_url"] = vpn_url
        self.state.set_fingerprint(db.alias, fp)
        return {"ok": True, **fp}


__all__ = ["Connector", "CODE_HINTS"]
