"""hhuvpn 命令行：给人看也给 agent 看。

约定（agent 依赖这条约定）：
  - 任何子命令都支持 --json，输出**唯一一个** JSON 对象；
  - 信封固定为 {schema, command, ok, generated_at, ...payload}；
  - 失败也返回 JSON（ok=false + error.code/message/hint），不会只往 stderr 吐中文；
  - 退出码：0 成功 / 1 失败 / 2 用法错误 / 3 需要人工介入（验证码）/ 4 缺凭据。

范围声明：本工具只负责「登上数据库」——校园网认证、WebVPN 登录、目标库可达与身份确认。
不提供检索、下载、综述等后续科研动作。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__, databases as db_catalog
from .campus import CampusClient
from .config import (CONFIG_TEMPLATE, PROJECT_ROOT, find_config, load_config,
                     resolve_credentials, save_config)
from .connector import CODE_HINTS, Connector
from .guard import Guard
from .secret import available as dpapi_available
from .secret import clear_secret, save_secret
from .state import State
from .webvpn import WebVPNClient, unwrap_vpn_url, vpn_url

SCHEMA = 1
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_HUMAN, EXIT_CRED = 0, 1, 2, 3, 4


# ---------------------------------------------------------------- 输出

class Emitter:
    def __init__(self, as_json: bool, pretty: bool = True):
        self.as_json = as_json
        self.pretty = pretty

    def _emit(self, payload: dict) -> None:
        if self.as_json:
            text = json.dumps(payload, ensure_ascii=False,
                              indent=2 if self.pretty else None)
            print(text, flush=True)
        else:
            self._human(payload)

    def done(self, command: str, payload: dict, ok: bool = True, **extra) -> dict:
        out = {"schema": SCHEMA, "command": command, "ok": bool(ok),
               "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "hhuvpn_version": __version__, **payload, **extra}
        self._emit(out)
        return out

    # ---- 人读版（只在交互时用；agent 一律 --json） ----
    def _human(self, p: dict) -> None:
        cmd = p.get("command", "")
        ok = p.get("ok")
        head = "√" if ok else "×"
        if cmd == "status":
            # 每行用自己的标记：校园网在线而 WebVPN 没登时，头部的 × 不该贴到校园网那行
            c_ok = bool(p["campus"].get("online"))
            print(f"{'√' if c_ok else '×'} 校园网: {'在线' if c_ok else '离线'}"
                  + (f"（WiFi: {p['campus'].get('wifi_ssid')}）" if p['campus'].get('wifi_ssid') else ""))
            wv = p.get("webvpn", {})
            w_ok = bool(wv.get("authenticated"))
            print(f"{'√' if w_ok else '×'} WebVPN: {'已登录' if w_ok else '未登录'}"
                  f"  {wv.get('base','')}")
            ch = {"direct": "校内直连", "webvpn": "WebVPN"}
            for d in p.get("databases", []):
                flag = {True: "已登上", False: "未登上", "unknown": "无法确认"}[d.get("authenticated")]
                mark = {True: "√", False: "×"}.get(d.get("authenticated"), "?")
                print(f"    {mark} {d.get('alias')}: {flag}"
                      f"（{ch.get(d.get('channel'), d.get('channel') or '?')}）"
                      f"  ({d.get('title') or d.get('http_status')})")
                if d.get("note"):
                    print(f"      · {d['note']}")
            return
        if cmd == "ensure":
            for s in p.get("steps", []):
                print(f"  [{s.get('name')}] {s.get('action')} {s.get('message','')}")
            ch = {"direct": "校内直连", "webvpn": "WebVPN"}
            for d in p.get("databases", []):
                flag = {True: "已登上", False: "未登上", "unknown": "无法确认"}[d.get("authenticated")]
                print(f"  [{d.get('alias')}] {flag}"
                      f"（{ch.get(d.get('channel'), d.get('channel') or '?')}）  {d.get('vpn_url','')}")
            if p.get("note"):
                print(f"  说明: {p['note']}")
            err = p.get("error") or {}
            if err:
                print(f"  ! {err.get('message')}")
                if err.get("hint"):
                    print(f"    提示: {err['hint']}")
            if p.get("ready"):
                print("  可用库: " + ", ".join(p["ready"]))
            return
        if cmd in ("url",):
            print(p.get("vpn_url") or p.get("target") or "")
            return
        if cmd == "db-list":
            for d in p.get("databases", []):
                print(f"  {d['alias']:<12} {d['name']:<32} {d['url']}   {'/'.join(d.get('tags', []))}")
            return
        if cmd == "fetch":
            print(f"  {p.get('status')} {p.get('final_url','')} ({p.get('bytes')} bytes) -> {p.get('out','(未保存)')}")
            return
        if cmd == "doctor":
            for k, v in p.get("checks", {}).items():
                print(f"  {'√' if v.get('ok') else '×'} {k}: {v.get('detail','')}")
            return
        if cmd == "open":
            mark = {True: "已登上", False: "未登上", "unknown": "无法确认"}
            if p.get("portal"):
                print(f"  WebVPN 门户: {p['portal']}"
                      + ("（已用浏览器打开）" if p.get("browser_opened") else ""))
                return
            ch = {"direct": "校内直连", "webvpn": "WebVPN"}
            for o in p.get("opened", []):
                print(f"  [{o.get('alias')}] {mark.get(o.get('authenticated'), '?')}"
                      f"（{ch.get(o.get('channel'), o.get('channel') or '?')}）"
                      + ("（已用浏览器打开）" if o.get("browser_opened") else ""))
                print(f"      {o.get('url') or o.get('vpn_url')}")
            for s in p.get("skipped", []):
                print(f"  [{s.get('alias')}] 未打开：{s.get('reason')}")
            err = p.get("error") or {}
            if err:
                print(f"  ! {err.get('message')}")
                if err.get("hint"):
                    print(f"    提示: {err['hint']}")
            if p.get("opened"):
                print(f"  说明: {p.get('note','')}")
            return
        if cmd in ("guard-tick", "guard", "guard-status", "guard-status"):
            if "circuit_open" in p:
                print(f"  断路标志: {'已触发（需人工处理）' if p['circuit_open'] else '正常'}"
                      f"  {p.get('flag','')}")
            last = p.get("last_tick") or p
            c = last.get("campus") or {}
            w = last.get("webvpn") or {}
            print(f"  校园网: {c.get('state','?')} {c.get('message','') or c.get('note','')}")
            print(f"  WebVPN: {w.get('state','?')} {w.get('message','') or w.get('note','')}")
            for d in last.get("databases", []) or []:
                print(f"    - {d.get('alias')}: {d.get('authenticated')}")
            if last.get("t"):
                print(f"  最近一轮: {last['t']}")
            return
        if cmd == "vpn-on":
            print(f"  WebVPN 已开启：{p.get('action','')} {p.get('message','')}")
            print("  守护会保活到 vpn off 或关机为止")
            return
        if cmd in ("vpn-status", "vpn-off"):
            if cmd == "vpn-off":
                had = p.get("had_session")
                print(f"  WebVPN 已关闭（{p.get('reason','')}）"
                      + ("，本地会话已清" if had else "（本来就没有会话）"))
                print("  已复位开启意图：守护不会自动重开，直到你再次 vpn on")
                return
            state_txt = {"on": "已开启", "off": "未开启", "expired": "已过期"}.get(p.get("state"), p.get("state"))
            intent_txt = {"on": "使用者已开启", "off": "使用者未开启"}.get(p.get("intent"), "?")
            print(f"  WebVPN: {state_txt}（{intent_txt}）")
            print(f"  保活    : {'开' if p.get('keepalive') else '关'}"
                  f"   开机后自动关闭: {'是' if p.get('close_on_boot') else '否'}")
            if p.get("session_age_minutes") is not None:
                print(f"  会话已存在: {p['session_age_minutes']} 分钟")
            if p.get("verified") is not None:
                print(f"  线上校验: {'有效' if p['verified'] else '无效'}")
            print(f"  说明    : {p.get('note','')}")
            return
        if cmd == "login":
            for r in p.get("results", []):
                print(f"  [{r.get('name')}] {r.get('action')} {'√' if r.get('ok') else '×'} "
                      f"{r.get('message','')}")
            return
        if cmd == "db-check":
            mark = {True: "已登上", False: "未登上", "unknown": "无法确认"}
            ch = {"direct": "校内直连", "webvpn": "WebVPN"}
            for d in p.get("databases", []):
                if d.get("error"):
                    print(f"  {d.get('alias'):<12} 出错  {d.get('error')}: {d.get('message','')}")
                    continue
                print(f"  {d.get('alias'):<12} {mark.get(d.get('authenticated'), '?')}"
                      f"  [{ch.get(d.get('channel'), d.get('channel') or '?')}]"
                      f"  {d.get('title') or ''}")
                if d.get("authenticated") != True and d.get("note"):  # noqa: E712
                    print(f"               · {d['note']}")
            return
        if cmd == "db-calibrate":
            for r in p.get("results", []):
                print(f"  {r.get('alias')}: 指纹已保存  markers={r.get('markers')}")
            return
        if cmd and cmd.startswith("config"):
            for k in ("path", "secret_file", "username", "encrypted", "removed", "project_root"):
                if k in p:
                    print(f"  {k}: {p[k]}")
            if "account" in p:
                print(f"  account: {p['account']}")
            return
        if cmd == "logout":
            print(f"  WebVPN 会话已清理：{p.get('result')}")
            return
        # 兜底：直接打印 JSON，别让人读不到信息
        print(json.dumps(p, ensure_ascii=False, indent=2))


def _fail(em: Emitter, command: str, code: str, message: str, hint: str = "",
          exit_code: int = EXIT_FAIL, **extra) -> int:
    """打印失败信封并返回退出码。

    必须返回 int（而不是 em.done 的 payload）：否则 main() 里 int(dict) 会抛异常，
    于是又打一份 internal_error 的 JSON——同一个命令输出两个 JSON 对象。
    """
    em.done(command, {"error": {"code": code, "message": message,
                                "hint": hint or CODE_HINTS.get(code, "")}, **extra},
            ok=False)
    return exit_code


# ---------------------------------------------------------------- 命令实现

def cmd_status(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    conn = Connector(cfg, State(Path(args.state_dir) if args.state_dir else cfg.state_dir),
                     verbose=args.verbose)
    targets = db_catalog.split_aliases(args.db)
    payload = {
        "campus": conn.campus_status(),
        "webvpn": conn.webvpn_status(deep=not args.cache_only),
        "config": str(cfg.path),
        "state_dir": str(conn.state.dir),
        "session_age_minutes": conn.session_age_minutes(),
    }
    if targets:
        payload["databases"] = [conn.database_status(a) for a in targets]
    ok = bool(payload["webvpn"].get("authenticated"))
    if targets:
        ok = ok and all(d.get("authenticated") is not False for d in payload["databases"])
    em.done("status", payload, ok=ok)
    return EXIT_OK if (ok or not args.strict) else EXIT_FAIL


def cmd_ensure(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    conn = Connector(cfg, State(Path(args.state_dir) if args.state_dir else cfg.state_dir),
                     verbose=args.verbose)
    report = conn.ensure(targets=db_catalog.split_aliases(args.db),
                         verify_dbs=not args.no_verify, force=args.force, via=args.via)
    em.done("ensure", report, ok=bool(report.get("ok")))
    if report.get("ok"):
        return EXIT_OK
    code = (report.get("error") or {}).get("code", "")
    steps = report.get("steps", [])
    if any(s.get("error_code") == "captcha_required" for s in steps):
        return EXIT_HUMAN
    if code == "credentials_missing" or any(s.get("error_code") == "credentials_missing" for s in steps):
        return EXIT_CRED
    return EXIT_FAIL


def cmd_login(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    conn = Connector(cfg, state, verbose=args.verbose)
    target = args.target or "all"
    results = []
    # 校园网与 WebVPN 是同一账号：只解析一次，两边复用——否则交互模式下会连问两遍密码
    creds = resolve_credentials(cfg, allow_prompt=not args.no_prompt and target != "webvpn")

    if target in ("all", "campus"):
        results.append(conn.ensure_campus(creds, force=True))

    if target in ("all", "webvpn"):
        if args.ticket:
            conn.session.load_cookies([{"name": "wengine_vpn_ticketwebvpn_hhu_edu_cn",
                                        "value": args.ticket, "domain": ".webvpn.hhu.edu.cn",
                                        "path": "/", "secure": True}])
            ok, info = conn.webvpn.is_authenticated()
            conn.save_session({"username": cfg.username, "source": "ticket-import"})
            results.append({"name": "webvpn", "action": "ticket_imported" if ok else "ticket_rejected",
                            "ok": ok, "message": "手工导入的 ticket 有效" if ok else "导入的 ticket 无效",
                            "evidence": info})
        else:
            if args.browser:
                url = conn.webvpn.base + "/"
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass
                print(f"已在浏览器打开 {url}\n"
                      f"在浏览器里完成登录（含滑块验证码）后：F12 → Application → Cookies → "
                      f"复制 wengine_vpn_ticketwebvpn_hhu_edu_cn 的值，"
                      f"执行 hhuvpn login --ticket <值> 导入。", file=sys.stderr)
                results.append({"name": "webvpn", "action": "browser_opened", "ok": False,
                                "message": "已打开浏览器，等待人工登录后用 --ticket 导入"})
            else:
                if not creds.complete:      # 复用上面解析好的同一份账号（校园网/WebVPN 同账号）
                    creds = resolve_credentials(cfg, allow_prompt=not args.no_prompt)
                if not creds.complete:
                    em.done("login", {"error": {"code": "credentials_missing",
                                                "message": "缺少账号或密码",
                                                "hint": CODE_HINTS["credentials_missing"]},
                                      "results": results}, ok=False)
                    return EXIT_CRED
                res = conn.webvpn.login_with_credentials(creds.username, creds.password,
                                                         captcha=args.captcha or "",
                                                         debug_dump_dir=args.debug_dump)
                if res.get("ok"):
                    conn.save_session({"username": creds.username})
                results.append({"name": "webvpn", **res})

    ok = all(r.get("ok") for r in results) or (
        # 校园网离线不算硬失败：校外也能用 WebVPN
        all(r.get("ok") for r in results if r.get("name") == "webvpn") and
        any(r.get("name") == "webvpn" for r in results))
    em.done("login", {"results": results, "state_dir": str(state.dir)}, ok=bool(ok))
    if ok:
        return EXIT_OK
    if any(r.get("error_code") == "captcha_required" for r in results):
        return EXIT_HUMAN
    if any(r.get("error_code") == "credentials_missing" for r in results):
        return EXIT_CRED
    return EXIT_FAIL


def cmd_logout(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    conn = Connector(cfg, state, verbose=args.verbose)
    conn.load_session()
    res = {"webvpn": conn.webvpn.logout()} if not args.local_only else {"webvpn": {"ok": True, "skipped": True}}
    conn.session.clear_cookies()
    state.clear_session()
    conn.campus.session.clear_cookies()
    em.done("logout", {"result": res}, ok=True)
    return EXIT_OK


def cmd_db(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    if args.action == "list":
        items = [d.to_dict() for d in db_catalog.list_all(state.dir)]
        if args.tag:
            items = [i for i in items if args.tag in " ".join(i.get("tags", []))]
        em.done("db-list", {"databases": items, "count": len(items),
                            "user_catalog": str(state.dir / db_catalog.USER_FILE)})
        return EXIT_OK

    conn = Connector(cfg, state, verbose=args.verbose)
    if args.action == "add":
        if not args.name and not args.url:
            return _fail(em, "db-add", "usage", "需要 alias 与 url", exit_code=EXIT_USAGE)
        from urllib.parse import urlsplit
        u = urlsplit(args.url if "://" in args.url else "https://" + args.url)
        path = db_catalog.add_user_db(state.dir, args.alias, args.name or args.alias,
                                      u.hostname or "", u.path or "/", u.scheme or "https")
        em.done("db-add", {"ok": True, "alias": db_catalog.normalize_alias(args.alias),
                           "written": str(path)})
        return EXIT_OK

    aliases = db_catalog.split_aliases(args.aliases) or cfg.default_databases
    conn.load_session()
    if args.action == "calibrate":
        out = [conn.calibrate_db(a) for a in aliases]
        em.done("db-calibrate", {"results": out}, ok=all(o.get("ok") for o in out))
        return EXIT_OK if all(o.get("ok") for o in out) else EXIT_FAIL

    # check
    results = [conn.database_status(a, save=False, via=getattr(args, "via", "webvpn"))
               for a in aliases]
    for r in results:
        if args.evidence:
            r["verify_rules"] = {**conn.defaults,
                                 **(db_catalog.get(r["alias"], state.dir).verify
                                    if db_catalog.get(r["alias"], state.dir) else {})}
            r["fingerprint"] = state.get_fingerprint(r.get("alias", ""))
    ready = [r["alias"] for r in results if r.get("authenticated") is True]
    unknown = [r["alias"] for r in results if r.get("authenticated") == "unknown"]
    em.done("db-check", {"databases": results, "ready": ready, "unknown": unknown},
            ok=bool(ready) or (not ready and not unknown and False))
    return EXIT_OK if ready else EXIT_FAIL


def cmd_url(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    if args.action == "unwrap":
        info = unwrap_vpn_url(cfg.webvpn_base, args.value)
        if not info:
            return _fail(em, "url-unwrap", "not_a_vpn_url", "不是可识别的 WebVPN 网址")
        em.done("url-unwrap", info)
        return EXIT_OK
    db = db_catalog.get(args.value, state.dir)
    target = db.url if db else (args.value if "://" in args.value else "https://" + args.value)
    vpn = vpn_url(cfg.webvpn_base, target)
    payload = {"target": target, "vpn_url": vpn, "alias": db.alias if db else None,
               "name": db.name if db else None}
    if args.open:
        try:
            opened = webbrowser.open(vpn)
            payload["browser_opened"] = bool(opened)
            payload["browser_note"] = "浏览器需要自己的 WebVPN 会话；未登录会跳统一身份认证"
        except Exception as exc:  # noqa: BLE001
            payload["browser_opened"] = False
            payload["browser_error"] = str(exc)
    em.done("url", payload)
    return EXIT_OK


def cmd_fetch(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    conn = Connector(cfg, State(Path(args.state_dir) if args.state_dir else cfg.state_dir),
                     verbose=args.verbose)
    conn.load_session()
    try:
        if getattr(args, "direct", False):
            # 校内 IP 直连：不经过 WebVPN（校内很多库本来就按出口 IP 授权）
            db = db_catalog.get(args.value, conn.state.dir)
            target = db.url if db else args.value
            if args.path:
                target = target.rstrip("/") + "/" + args.path.lstrip("/")
            resp, vpn = conn.session.get(target, timeout=40), ""
        elif args.value.startswith("http"):
            resp, vpn = conn.webvpn.fetch(args.value, timeout=40)
            target = args.value
        else:
            db = db_catalog.get(args.value, conn.state.dir)
            target = db.url if db else args.value
            resp, vpn = conn.webvpn.fetch(target, path=args.path, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return _fail(em, "fetch", "fetch_failed", str(exc))
    out_path = ""
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(resp.body)
        out_path = str(p)
    em.done("fetch", {"target": target, "vpn_url": vpn, "status": resp.status,
                      "final_url": resp.url, "bytes": len(resp.body),
                      "content_type": resp.content_type, "out": out_path,
                      "title": (resp.text.split("<title>", 1)[1].split("</title>", 1)[0].strip()
                                if "<title>" in resp.text else "")[:160]},
            ok=resp.status < 400)
    return EXIT_OK if resp.status < 400 else EXIT_FAIL


def cmd_session(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    conn = Connector(cfg, state, verbose=args.verbose)
    info = conn.load_session()
    payload = {"state_dir": str(state.dir), "session_file": str(state.session_path),
               "loaded": info, "cookies": conn.session.cookies(),
               "webvpn_authenticated": conn.webvpn.is_authenticated()[0],
               "age_minutes": conn.session_age_minutes()}
    if args.cookie_file:
        n = conn.session.export_netscape(args.cookie_file, domain_filter="webvpn")
        payload["cookie_file"] = args.cookie_file
        payload["cookie_count"] = n
    em.done("session", payload, ok=True)
    return EXIT_OK


def cmd_doctor(args, em: Emitter) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    conn = Connector(cfg, state, verbose=args.verbose)
    checks: dict[str, dict] = {}

    c = conn.campus.probe()
    checks["campus_online"] = {"ok": c["online"],
                               "detail": f"{c['probe_url']} → {c['status'] or c['error']}"}
    try:
        r = conn.webvpn.s.get(conn.webvpn.base + "/", allow_redirects=True, max_redirects=6)
        checks["webvpn_reachable"] = {"ok": True,
                                      "detail": f"HTTP {r.status} → {r.url[:90]}"}
        checks["webvpn_tls"] = {"ok": conn.session._tls_verified,
                                "detail": "严格校验" if conn.session._tls_verified
                                          else "已降级为不校验（自签证书？）"}
    except Exception as exc:  # noqa: BLE001
        checks["webvpn_reachable"] = {"ok": False, "detail": str(exc)[:160]}

    ok, info = conn.webvpn.is_authenticated()
    checks["webvpn_session"] = {"ok": ok, "detail": info.get("reason", "") + " " + info.get("final_url", "")[:80]}

    creds = resolve_credentials(cfg, allow_prompt=False)
    checks["credentials"] = {"ok": creds.complete,
                             "detail": f"来源={creds.source} 账号={creds.username or '(空)'} "
                                       f"密码={'已设置' if creds.password else '未设置'}"}
    checks["config_file"] = {"ok": cfg.path.exists() if cfg.path else False,
                             "detail": str(cfg.path)}
    checks["state_dir"] = {"ok": True, "detail": str(state.dir)}
    checks["dpapi"] = {"ok": dpapi_available(),
                       "detail": "可用（可用 config set-password 加密保存）" if dpapi_available()
                                 else "不可用（非 Windows，退回明文配置）"}
    alias_list = db_catalog.split_aliases(args.db) or cfg.default_databases
    for alias in alias_list[:5]:
        d = conn.database_status(alias, save=False)
        checks[f"db:{alias}"] = {"ok": d.get("authenticated") is True,
                                 "detail": f"{d.get('authenticated')} / {d.get('title') or d.get('note','')}"}
    em.done("doctor", {"checks": checks,
                       "all_ok": all(v["ok"] for k, v in checks.items()
                                     if k not in ("campus_online",))})
    return EXIT_OK


def cmd_config(args, em: Emitter) -> int:
    if args.action == "init":
        p = find_config(args.config)
        if p.exists() and not args.force:
            return _fail(em, "config-init", "exists", f"配置已存在：{p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        example = PROJECT_ROOT / "config.example.ini"
        p.write_text(example.read_text(encoding="utf-8") if example.exists() else CONFIG_TEMPLATE,
                     encoding="utf-8")
        em.done("config-init", {"path": str(p)})
        return EXIT_OK
    cfg = load_config(args.config)
    if args.action == "path":
        em.done("config-path", {"path": str(cfg.path), "project_root": str(PROJECT_ROOT),
                                "state_dir": str(cfg.state_dir)})
        return EXIT_OK
    if args.action == "show":
        creds = resolve_credentials(cfg, allow_prompt=False)
        em.done("config-show", {"path": str(cfg.path),
                                "account": {"username": creds.username, "source": creds.source,
                                            "has_password": bool(creds.password)},
                                "webvpn": cfg.webvpn_base, "campus_probe": cfg.campus_probe_url,
                                "default_databases": cfg.default_databases,
                                "state_dir": str(cfg.state_dir)})
        return EXIT_OK
    if args.action == "set-password":
        import getpass
        state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
        user = args.username or cfg.username
        if not user:
            print("学号/工号：", end="", flush=True)
            user = sys.stdin.readline().strip()
        pwd = args.password or getpass.getpass("信息门户密码（不回显）：")
        path = save_secret(state.dir, user, pwd)
        save_config(cfg, {"account": {"username": user}})
        em.done("config-set-password", {"secret_file": str(path), "username": user,
                                        "encrypted": dpapi_available()})
        return EXIT_OK
    if args.action == "clear-password":
        state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
        removed = clear_secret(state.dir)
        em.done("config-clear-password", {"removed": removed})
        return EXIT_OK
    return EXIT_USAGE


def cmd_open(args, em: Emitter) -> int:
    """给人用的一步到位：先确保登上数据库，再用默认浏览器打开。"""
    cfg = load_config(args.config)
    conn = Connector(cfg, State(Path(args.state_dir) if args.state_dir else cfg.state_dir),
                     verbose=args.verbose)
    if args.portal:
        url = cfg.webvpn_base + "/"
        payload = {"portal": url, "target": cfg.webvpn_base}
        if not args.no_browser:
            payload["browser_opened"] = bool(webbrowser.open(url))
        em.done("open", payload, ok=True)
        return EXIT_OK

    aliases = db_catalog.split_aliases(args.database) or cfg.default_databases
    # 正常逻辑：校园网 → WebVPN → 数据库；open 默认就走这条，开出的也是 WebVPN 入口地址
    report = conn.ensure(targets=aliases, verify_dbs=not args.no_verify, via=args.via)
    opened, skipped = [], []
    for entry in report.get("databases", []):
        # 通道决定开哪个地址：校内直连已认证 → 直接开真实网址（浏览器同样在校内 IP 上，
        # 一样能进）；只有走 WebVPN 时才需要那个带签名的入口地址。
        channel = entry.get("channel")
        url = entry.get("vpn_url") or entry.get("target")
        if not url:
            skipped.append({"alias": entry.get("alias"), "reason": "no_url"})
            continue
        if entry.get("authenticated") is False and not args.force:
            skipped.append({"alias": entry.get("alias"), "reason": entry.get("note") or "not_reachable"})
            continue
        item = {"alias": entry.get("alias"), "url": url, "channel": channel,
                "vpn_url": entry.get("vpn_url") or "",
                "authenticated": entry.get("authenticated")}
        if not args.no_browser:
            try:
                item["browser_opened"] = bool(webbrowser.open(url))
            except Exception as exc:  # noqa: BLE001
                item["browser_opened"] = False
                item["browser_error"] = str(exc)
        opened.append(item)
    confirmed_direct = [o["alias"] for o in opened
                        if o.get("channel") == "direct" and o.get("authenticated") is True]
    unconfirmed = [o["alias"] for o in opened if o.get("authenticated") != True]  # noqa: E712
    used_webvpn = [o["alias"] for o in opened if o.get("channel") == "webvpn"]
    note = ""
    if used_webvpn:
        note = ("通过学校 WebVPN 打开（正常流程）：浏览器里若仍要求登录，"
                "是浏览器自己没有 WebVPN 会话——在浏览器登录一次 https://webvpn.hhu.edu.cn/ 即可"
                "（脚本侧会话不受影响）。")
    elif confirmed_direct:
        note = (f"备用通道（校内 IP 直连）已用学校身份认证（{'、'.join(confirmed_direct)}）："
                "浏览器直接打开真实网址即可；正常流程仍是先登 WebVPN。")
    if unconfirmed:
        extra = (f"注意：{'、'.join(unconfirmed)} 没能确认到机构身份（可能是页面由 JS 渲染，"
                 "也可能确实需要 WebVPN）；打开后请自行核对页面上的机构名。")
        note = (note + " " + extra).strip()
    payload = {"databases": report.get("databases", []), "opened": opened, "skipped": skipped,
               "steps": report.get("steps", []), "error": report.get("error"), "note": note}
    em.done("open", payload, ok=bool(opened))
    if opened:
        return EXIT_OK
    code = (report.get("error") or {}).get("code", "")
    if code == "captcha_required":
        return EXIT_HUMAN
    if code == "credentials_missing":
        return EXIT_CRED
    return EXIT_FAIL


def cmd_guard(args, em: Emitter) -> int:
    """独立守护（不需要 agent）：校园网掉线自动重登 + WebVPN 会话过期自动重登。"""
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    guard = Guard(cfg, state, quiet=args.quiet or args.json)

    if args.clear_flag:
        cleared = guard.clear_circuit()
        em.done("guard", {"cleared": cleared, "flag": str(guard.auth_flag)}, ok=True)
        return EXIT_OK

    if args.status:
        snap = {}
        p = state.dir / "guard.json"
        if p.exists():
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                snap = {}
        em.done("guard-status", {"circuit_open": guard.circuit_open(),
                                 "flag": str(guard.auth_flag),
                                 "last_tick": snap}, ok=True)
        return EXIT_OK

    verify_dbs = db_catalog.split_aliases(args.db)
    keepalive = True if args.keepalive_webvpn else None
    close_on_boot = False if args.no_close_on_boot else None
    if args.once:
        out = guard.tick(verify_dbs=verify_dbs,
                         webvpn_check_minutes=args.webvpn_check_minutes,
                         db_check_minutes=args.db_check_minutes,
                         notify=not args.no_notify,
                         webvpn_keepalive=keepalive, close_on_boot=close_on_boot)
        ok = (out.get("campus") or {}).get("state") == "online" or not cfg.campus_enabled
        em.done("guard-tick", out, ok=ok)
        return EXIT_OK if ok else EXIT_FAIL

    if args.json:
        print(json.dumps({"schema": SCHEMA, "command": "guard", "ok": True,
                          "note": "以下为每轮一条 JSON（JSONL）",
                          "interval_minutes": args.interval}, ensure_ascii=False), flush=True)
    guard.run(interval_minutes=args.interval, max_minutes=args.max_minutes,
              verify_dbs=verify_dbs, webvpn_check_minutes=args.webvpn_check_minutes,
              db_check_minutes=args.db_check_minutes, notify=not args.no_notify,
              webvpn_keepalive=keepalive, close_on_boot=close_on_boot,
              on_tick=(lambda o: print(json.dumps(o, ensure_ascii=False), flush=True))
              if args.json else None)
    return EXIT_OK


def cmd_vpn(args, em: Emitter) -> int:
    """WebVPN 的显式开关：按需开启 / 主动关闭 / 查看状态。

    这是"使用者说明"那条路径——守护（计划任务）不会替你开 WebVPN。
    """
    cfg = load_config(args.config)
    state = State(Path(args.state_dir) if args.state_dir else cfg.state_dir)
    conn = Connector(cfg, state, verbose=args.verbose)

    if args.action == "status":
        conn.load_session()
        has = bool(conn.webvpn.ticket())
        intent = state.webvpn_intent()
        payload = {"state": "on" if has else "off", "intent": intent.get("intent"),
                   "intent_by": intent.get("by"),
                   "keepalive": cfg.webvpn_keepalive, "close_on_boot": cfg.webvpn_close_on_boot,
                   "has_ticket": has, "session_age_minutes": conn.session_age_minutes(),
                   "note": ("按需模式：守护不主动开启；开启后由守护保活到 vpn off 或关机"
                            if intent.get("intent") == "on" else
                            "按需模式：当前未开启，守护不会自动开；用 vpn on 显式开启")}
        if has and not args.cache_only:
            ok, info = conn.webvpn.is_authenticated()
            payload["verified"] = ok
            payload["evidence"] = info
            payload["state"] = "on" if ok else "expired"
            if ok and intent.get("intent") != "on":
                # 会话确实有效 = 实际上就是开着的：认领它，避免"状态 on / 意图 off"自相矛盾。
                # 想关就显式 vpn off，别让它悬在半空。
                state.set_webvpn_intent(True, by="status_verified")
                payload["intent"] = "on"
                payload["intent_by"] = "status_verified"
                payload["note"] = ("检测到本地 WebVPN 会话有效，已按「开启中」认领；"
                                   "守护会保活到 vpn off 或关机")
        em.done("vpn-status", payload, ok=True)
        return EXIT_OK

    if args.action == "off":
        out = conn.close_webvpn("使用者主动关闭", remote=not args.local_only)
        em.done("vpn-off", out, ok=True)
        return EXIT_OK

    # on
    creds = resolve_credentials(cfg, allow_prompt=not args.no_prompt)
    if not creds.complete:
        return _fail(em, "vpn-on", "credentials_missing", "缺少账号或密码",
                     exit_code=EXIT_CRED)
    res = conn.ensure_webvpn(creds, force=args.force)
    if res.get("ok"):
        em.done("vpn-on", {"state": "on", "action": res.get("action"),
                           "message": res.get("message", "")}, ok=True)
        return EXIT_OK
    code = res.get("error_code") or "webvpn_unavailable"
    em.done("vpn-on", {"error": {"code": code, "message": res.get("message", ""),
                                 "hint": CODE_HINTS.get(code, "")}, "steps": res.get("steps", [])},
            ok=False)
    return EXIT_HUMAN if code == "captcha_required" else EXIT_FAIL


def cmd_mcp(args, em: Emitter) -> int:
    """【可选附属】把 CLI 转发给 integrations/mcp_server.py。

    核心不依赖它：删掉 integrations/ 目录，其它命令照常工作。
    """
    import importlib.util
    cfg = load_config(args.config)
    entry = PROJECT_ROOT / "integrations" / "mcp_server.py"
    if not entry.exists():
        return _fail(em, "mcp", "integration_missing",
                     f"未找到 {entry}（MCP 是可选附属功能，可自行决定是否保留）")
    spec = importlib.util.spec_from_file_location("hhuvpn_mcp_server", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.serve_stdio(cfg, state_dir=Path(args.state_dir) if args.state_dir else None)


# ---------------------------------------------------------------- 参数

def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """让 --json/--config 等既能写在子命令前，也能写在子命令后。

    用 SUPPRESS 作为默认值：只有真的传了才会覆盖主解析器的取值，
    否则「hhuvpn --json ensure」会被子解析器的默认 False 悄悄抹掉。
    """
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出单个 JSON 对象（可放在子命令之后）")
    parser.add_argument("--compact", action="store_true", default=argparse.SUPPRESS,
                        help="JSON 不缩进")
    parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="打印过程日志到 stderr")
    parser.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="状态目录")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hhuvpn",
                                description="河海大学校园网 + WebVPN 自动登录与文献数据库接入连接器")
    p.add_argument("--version", action="version", version=f"hhuvpn {__version__}")
    p.add_argument("--config", help="配置文件路径（默认项目内 config.ini）")
    p.add_argument("--state-dir", help="状态目录（会话 cookie / 指纹 / 日志）")
    p.add_argument("--json", action="store_true", help="输出单个 JSON 对象（agent 请一律带上）")
    p.add_argument("--compact", action="store_true", help="JSON 不缩进")
    p.add_argument("-v", "--verbose", action="store_true", help="打印过程日志到 stderr")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="查看校园网 / WebVPN / 指定库的当前状态")
    s.add_argument("--db", help="逗号分隔的库别名，如 sd,cnki")
    s.add_argument("--strict", action="store_true", help="未登录也返回非零退出码")
    s.add_argument("--cache-only", action="store_true", help="只看本地 cookie，不联网校验")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("ensure", help="幂等：需要就登录，然后确认目标库可用（agent 主入口）")
    s.add_argument("--db", help="逗号分隔的库别名，默认取 config 的 [databases] default")
    s.add_argument("--no-verify", action="store_true", help="只保证 WebVPN 会话，不校验库")
    s.add_argument("--force", action="store_true", help="忽略本地会话，强制重新登录")
    s.add_argument("--via", choices=["webvpn", "auto", "direct"], default="webvpn",
                   help="进库通道：webvpn=通过学校 WebVPN（默认，正常逻辑）；"
                        "auto=WebVPN 走不通时允许校内直连兜底；direct=只用校内直连（备用通道）")
    s.set_defaults(func=cmd_ensure)

    s = sub.add_parser("login", help="显式登录校园网 / WebVPN")
    s.add_argument("target", nargs="?", choices=["all", "campus", "webvpn"], default="all")
    s.add_argument("--captcha", help="图形验证码（captchaSwitch=1 时）")
    s.add_argument("--ticket", help="手工导入浏览器里的 wengine_vpn_ticketwebvpn_hhu_edu_cn 值")
    s.add_argument("--browser", action="store_true", help="打开浏览器人工登录（滑块验证码场景）")
    s.add_argument("--debug-dump", help="把登录响应原文存到该目录，便于排查")
    s.add_argument("--no-prompt", action="store_true", help="不要交互式索要密码")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", help="注销 WebVPN 会话并清理本地 cookie")
    s.add_argument("--local-only", action="store_true", help="只清本地，不请求远端注销")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("db", help="数据库目录：列出 / 校验 / 校准 / 添加")
    s.add_argument("action", choices=["list", "check", "calibrate", "add"])
    s.add_argument("aliases", nargs="*", help="库别名（check/calibrate 用）")
    s.add_argument("--tag", help="list 时按标签过滤")
    s.add_argument("--url", help="add 时的网址")
    s.add_argument("--name", help="add 时的显示名")
    s.add_argument("--evidence", action="store_true", help="check 时附带判定规则与指纹")
    s.add_argument("--via", choices=["webvpn", "auto", "direct"], default="webvpn",
                   help="进库通道：webvpn=通过学校 WebVPN（默认，正常逻辑）；"
                        "auto=WebVPN 走不通时允许校内直连兜底；direct=只用校内直连（备用通道）")
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("url", help="把库别名/网址换算成 WebVPN 入口地址")
    s.add_argument("action", nargs="?", choices=["build", "unwrap"], default="build")
    s.add_argument("value", help="库别名或网址；unwrap 时为 WebVPN 网址")
    s.add_argument("--open", action="store_true", help="用默认浏览器打开")
    s.set_defaults(func=cmd_url)

    s = sub.add_parser("fetch", help="用 WebVPN 会话取页面（认证通道，不代表在做检索）")
    s.add_argument("value", help="库别名或完整网址")
    s.add_argument("--path", help="追加到库首页后面的路径")
    s.add_argument("--out", help="把响应体保存到文件")
    s.add_argument("--direct", action="store_true", help="校内 IP 直连，不走 WebVPN")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("session", help="查看/导出当前 WebVPN 会话")
    s.add_argument("--cookie-file", help="导出 Netscape 格式 cookie 文件（供 curl 用）")
    s.set_defaults(func=cmd_session)

    s = sub.add_parser("doctor", help="环境与链路体检")
    s.add_argument("--db", help="额外体检的库别名")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="配置管理")
    s.add_argument("action", choices=["init", "path", "show", "set-password", "clear-password"])
    s.add_argument("--force", action="store_true")
    s.add_argument("--username")
    s.add_argument("--password", help="不推荐：会出现在命令历史里，建议留空改交互输入")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("open", help="一步到位：先确保登上数据库，再用浏览器打开（人用）")
    s.add_argument("database", nargs="*", help="库别名，如 sd cnki；留空用配置里的默认库")
    s.add_argument("--portal", action="store_true", help="改为打开 WebVPN 门户首页")
    s.add_argument("--no-verify", action="store_true", help="不逐个校验库，只要会话就打开")
    s.add_argument("--no-browser", action="store_true", help="只做登录校验，不调浏览器")
    s.add_argument("--force", action="store_true", help="即使判定未认证也照样打开")
    s.add_argument("--via", choices=["webvpn", "auto", "direct"], default="webvpn",
                   help="进库通道，默认 webvpn（校内直连是备用通道）")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("guard", help="独立守护：校园网掉线重登 + WebVPN 会话保活（无需 agent）")
    s.add_argument("--once", action="store_true", help="只跑一轮（计划任务用这个）")
    s.add_argument("--interval", type=int, default=1, help="检测间隔（分钟，默认 1）")
    s.add_argument("--max-minutes", type=int, default=0, help="运行 N 分钟后自动退出（0=一直跑）")
    s.add_argument("--db", help="顺带定期校验的库别名，如 sd,cnki")
    s.add_argument("--webvpn-check-minutes", type=int, default=15,
                   help="WebVPN 会话多久检查一次（分钟，默认 15）")
    s.add_argument("--db-check-minutes", type=int, default=30,
                   help="指定了 --db 时，库检查间隔（分钟，默认 30）")
    s.add_argument("--quiet", action="store_true", help="不打印过程日志")
    s.add_argument("--no-notify", action="store_true", help="不弹 Windows 通知")
    s.add_argument("--clear-flag", action="store_true", help="清除断路标志（改密码后用）")
    s.add_argument("--status", action="store_true", help="查看守护状态与上一轮结果")
    s.add_argument("--keepalive-webvpn", action="store_true",
                   help="让守护也保活 WebVPN（默认不保活：WebVPN 按需开启）")
    s.add_argument("--no-close-on-boot", action="store_true",
                   help="开机后首次运行不关闭 WebVPN（默认会关闭，保证关机后保持关闭）")
    s.set_defaults(func=cmd_guard)

    s = sub.add_parser("vpn", help="WebVPN 显式开关：on=按需开启 / off=关闭并清会话 / status")
    s.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    s.add_argument("--no-prompt", action="store_true", help="不要交互式索要密码")
    s.add_argument("--force", action="store_true", help="忽略本地会话，强制重新登录")
    s.add_argument("--local-only", action="store_true", help="off 时只清本地，不请求远端注销")
    s.add_argument("--cache-only", action="store_true", help="status 时只看本地 cookie，不联网校验")
    s.set_defaults(func=cmd_vpn)

    s = sub.add_parser("mcp", help="【可选】以 MCP stdio 服务器运行，供支持 MCP 的 agent 调用")
    s.set_defaults(func=cmd_mcp)

    for _name, subparser in sub.choices.items():
        _add_common_flags(subparser)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        args.state_dir = args.state_dir
    em = Emitter(as_json=args.json, pretty=not args.compact)
    try:
        return int(args.func(args, em) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.json:
            em.done(getattr(args, "cmd", "unknown"),
                    {"error": {"code": "internal_error", "message": repr(exc), "hint": ""}},
                    ok=False)
        else:
            import traceback
            traceback.print_exc()
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
