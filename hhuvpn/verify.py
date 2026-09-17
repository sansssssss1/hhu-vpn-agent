"""「是否已经用学校身份登上目标数据库」的判定与校准。

这件事本质上是启发式，所以本模块的原则是：宁可说不知道，也不假装成功。

判定分三层：
  1. 通道层：HTTP 拿到 200 且不是被弹回统一身份认证/登录页 → reachable
  2. 内容层：命中机构名（默认 河海大学|Hohai University）或库自身的 require_any 规则
  3. 指纹层：用户跑过一次 db calibrate 之后，用当时学到的标记做判定

命中不了任何标记时返回 authenticated="unknown"，并在 evidence 里给出标题/片段，
让人或 agent 自己看一眼就能确认——不猜、不糊弄。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

LOGIN_HINTS = ("authserver/login", "/cas/login", "login.jsp?", "/login?", "signin",
               "idp/saml", "wayf")
LOGIN_TEXT_HINTS = ("统一身份认证平台", "请输入学号", "请先登录", "用户登录", "Sign in to your account")


# 机构名必须出现在"像认证信息"的上下文里才算数。
# 教训（2026-09-16 实测）：CNKI 首页上写着"机构用户"四个字，与是否认证无关；
# 早先那条宽松规则直接把 CNKI 判成"已登上"——典型的假阳性。
# 所以：① 只用裸机构名匹配 → 可能被新闻/页脚命中；② 必须带上下文。
# 模板里的 {inst} 会被 config 里的机构名（形如 河海大学|Hohai University）替换。
INSTITUTION_PATTERNS = (
    r"(?:机构用户|所属机构|欢迎|您好|登录用户|当前IP|institution|orgName|entitled)[^<>]{0,40}(?:{inst})",
    r"(?:{inst})[^<>]{0,20}(?:图书馆|读者|用户|机构)",
)


def _snippet(text: str, match: re.Match, pad: int = 60) -> str:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:200]


def _match_any(patterns, text: str, evidence: list | None = None,
               invalid: list | None = None) -> list[str]:
    """返回命中的规则原文；命中的上下文片段写进 evidence，非法正则记进 invalid。

    非法正则**不再**静默退化成子串匹配——那样既可能误报也可能漏报，
    而且没人会发现规则写错了。写错就要显式暴露出来。
    """
    hits = []
    for p in patterns or []:
        try:
            m = re.search(p, text, re.I)
        except re.error as exc:
            if invalid is not None:
                invalid.append({"pattern": str(p), "error": str(exc)})
            continue
        if m:
            hits.append(p)
            if evidence is not None:
                evidence.append({"pattern": str(p), "snippet": _snippet(text, m)})
    return hits


def verify_page(db, resp, cfg, fingerprint: dict | None = None, defaults: dict | None = None) -> dict:
    """对一个已取得的响应做判定。"""
    if defaults is None:
        from .databases import load_catalog
        _dbs, _catalog_defaults = load_catalog()
        defaults = (_catalog_defaults or {}).get("verify", {})
    fp = fingerprint or {}
    text = resp.text or ""
    head = text[:200000]
    final = resp.url or ""
    status = resp.status

    rules = {**defaults, **(db.verify or {}), **(fp.get("verify") or {})}
    expect = rules.get("expect_status") or [200]
    min_bytes = int(rules.get("min_bytes") or 0)

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:160]

    reached_portal = any(h in final.lower() for h in LOGIN_HINTS)
    login_text = [h for h in LOGIN_TEXT_HINTS if h in head]
    # 限流要单独识别：429/Too Many Requests 既不是"未授权"也不是"页面改版"，
    # 报成"未命中机构标记"会让人以为是订阅问题。实测 ScienceDirect 短时间内多次请求就会 429。
    rate_limited = (status == 429 or "Too Many Requests" in head[:3000]
                    or "rate limit" in head[:3000].lower())

    evidence: list[dict] = []
    invalid: list[dict] = []
    inst = cfg.institution or "河海大学|Hohai University"
    # 用 replace 而不是 format：正则里的 {0,40} 会被 str.format 当成占位符
    inst_rules = [str(t).replace("{inst}", inst) for t in
                  (rules.get("institution_patterns") or INSTITUTION_PATTERNS)]

    markers: list[str] = []
    markers += [f"title:{t}" for t in fp.get("title_markers", []) if t and t.lower() in title.lower()]
    markers += _match_any(fp.get("markers"), head, evidence, invalid)
    markers += _match_any((db.verify or {}).get("require_any"), head, evidence, invalid)
    markers += _match_any(inst_rules, head, evidence, invalid)

    forbidden = _match_any(rules.get("forbid_any"), head)

    reachable = bool(status in expect and len(resp.body or b"") >= min_bytes
                     and not reached_portal and not login_text)
    if rate_limited:
        authenticated = "unknown"      # 限流时拿到的不是真实页面，不能下结论
    elif markers and reachable and not forbidden:
        authenticated = True
    elif not reachable:
        authenticated = False
    else:
        authenticated = "unknown"

    note = ""
    if rate_limited:
        note = (f"被目标站限流（HTTP {status}）：短时间内请求过多，等几分钟再试即可。"
                "这不是未授权，也不是 WebVPN 的问题——不要把结论写成「登不上」，"
                "间隔开请求后再验证")
    elif reached_portal:
        note = "被重定向回登录页：WebVPN 会话可能已失效"
    elif login_text and not markers:
        note = "页面出现登录/认证字样，未看到机构身份标记"
    elif not markers:
        note = ("未命中任何机构标记，因此判为「无法确认」而不是已认证：可能该库首页的内容由 JS 渲染、"
                "静态 HTML 里没有机构名，也可能确实需要 WebVPN 会话。"
                "可先看 evidence 里的原文片段人工核对，或执行 db calibrate 学习该库的真实指纹")
        # 有些库（如 CNKI）已知"脚本根本看不到机构名"，把实测原因直接说清楚，省得人去猜
        if rules.get("verify_note"):
            note = note + " 注：" + str(rules["verify_note"])
    elif forbidden:
        note = f"命中排除规则 {forbidden}，保守判为未认证"

    return {
        "alias": db.alias, "name": db.name, "target": db.url,
        # 走 WebVPN 时带上那条签名入口地址（caller 会给 resp 挂 vpn_url）
        "vpn_url": getattr(resp, "vpn_url", "") or "",
        "reachable": reachable,
        "authenticated": authenticated,
        "http_status": status,
        "final_url": final[:300],
        "title": title,
        "bytes": len(resp.body or b""),
        "markers_matched": markers[:8],
        "markers_forbidden": forbidden[:4],
        "rate_limited": rate_limited,
        "evidence": evidence[:5],
        "rules_invalid": invalid[:5],
        "institution": cfg.institution,
        "note": note,
    }


def calibrate(db, resp, cfg) -> dict:
    """从一次「人确认过已登录」的页面里学习指纹。"""
    text = resp.text or ""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]

    inst_hits = [p for p in cfg.institution.split("|") if p and p in text]
    extra = set()
    for m2 in re.finditer(r"[^<>\n]{0,40}(?:河海大学|Hohai University)[^<>\n]{0,40}", text):
        seg = re.sub(r"\s+", " ", m2.group(0)).strip()
        if 4 <= len(seg) <= 80:
            extra.add(seg)
        if len(extra) >= 5:
            break

    host = urlsplit(db.url).hostname or db.host
    markers = list(inst_hits)
    if not markers and "hohai" in text.lower():
        markers = ["(?i)hohai"]
    return {
        "alias": db.alias, "name": db.name, "host": host, "target": db.url,
        "title": title,
        "title_markers": [title] if title else [],
        "markers": markers,
        "evidence_snippets": sorted(extra)[:5],
        "http_status": resp.status,
        "bytes": len(resp.body or b""),
    }


def calibrated_verify(fingerprint: dict) -> dict:
    """把校准结果转成 verify 规则（不覆盖用户显式写的 require_any）。"""
    markers = list(fingerprint.get("markers") or [])
    markers += [re.escape(t) for t in (fingerprint.get("title_markers") or []) if t]
    return {"require_any": markers} if markers else {}


__all__ = ["verify_page", "calibrate", "calibrated_verify", "LOGIN_HINTS", "LOGIN_TEXT_HINTS"]
