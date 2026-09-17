"""数据库访问判定与校准测试（离线，用合成响应）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hhuvpn.config import load_config  # noqa: E402
from hhuvpn.databases import get, load_catalog, normalize_alias, split_aliases  # noqa: E402
from hhuvpn.verify import calibrate, verify_page  # noqa: E402


class FakeResp:
    def __init__(self, text="", status=200, url="https://www.sciencedirect.com/", body=None):
        self.text = text
        self.status = status
        self.url = url
        self.body = body if body is not None else text.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    @property
    def content_type(self):
        return "text/html"


CFG = load_config()          # 用项目默认（可能不存在 config.ini，但默认值可用）


def test_catalog_loads_and_normalizes():
    dbs, defaults = load_catalog()
    assert len(dbs) > 20
    assert "sd" in dbs and "cnki" in dbs
    assert defaults.get("verify", {}).get("min_bytes")
    assert normalize_alias("ScienceDirect") == "sd"
    assert normalize_alias("知网") == "cnki"
    assert split_aliases("sd, cnki;wos") == ["sd", "cnki", "wos"]
    db = get("sciencedirect")
    assert db and db.host == "www.sciencedirect.com"


def test_custom_url_becomes_database():
    db = get("https://example.org/abc")
    assert db and db.host == "example.org" and db.entry == "/abc"


def test_authenticated_when_institution_marker_present():
    db = get("sd")
    html = ("<html><title>ScienceDirect</title><body>Welcome, 河海大学 读者"
            + "z" * 2000 + "</body></html>")
    out = verify_page(db, FakeResp(html), CFG)
    assert out["reachable"] is True
    assert out["authenticated"] is True
    assert any("河海大学" in m for m in out["markers_matched"])


def test_not_authenticated_when_bounced_to_login():
    db = get("sd")
    out = verify_page(db, FakeResp("<html><title>统一身份认证平台</title></html>", status=200,
                                   url="https://webvpn.hhu.edu.cn/https/xx/authserver/login?service=1"),
                      CFG)
    assert out["reachable"] is False
    assert out["authenticated"] is False


def test_unknown_when_no_marker_but_page_looks_fine():
    db = get("sd")
    out = verify_page(db, FakeResp("<html><title>ScienceDirect</title><body>"
                                   + ("x" * 5000) + "</body></html>"), CFG)
    assert out["reachable"] is True
    assert out["authenticated"] == "unknown"
    assert "calibrate" in out["note"]


def test_rate_limited_page_is_not_reported_as_not_authenticated():
    """实测：ScienceDirect 短时间多次请求会 429。

    限流既不是"未授权"也不是"页面改版"：必须单独说明，且不能判成 True/False，
    只能"无法确认"——否则会把订阅问题误报或把限流误报成登录失败。
    """
    db = get("sd")
    html = "<html><title>ScienceDirect</title><body>Too Many Requests" + "z" * 3000 + "</body></html>"
    out = verify_page(db, FakeResp(html, status=429), CFG)
    assert out["rate_limited"] is True
    assert out["authenticated"] == "unknown", "限流时拿到的不是真实页面，不能下结论"
    assert "限流" in out["note"] and "几分钟" in out["note"]
    assert "429" in out["note"]


def test_too_small_page_is_not_reachable():
    db = get("cnki")
    out = verify_page(db, FakeResp("<html><title>CNKI</title></html>"), CFG)
    assert out["reachable"] is False


def test_cnki_bare_机构用户_is_not_evidence():
    """实测回归：CNKI 首页写着「机构用户」，与是否认证无关。

    早先的宽松规则把 CNKI 判成"已登上"——典型假阳性。这条测试锁住它不会再发生。
    """
    db = get("cnki")
    html = ("<html><title>中国知网</title><body>" + "y" * 3000
            + "<a>机构用户</a><a>登录</a><a>注册</a></body></html>")
    out = verify_page(db, FakeResp(html, url="https://www.cnki.net/"), CFG)
    assert out["authenticated"] == "unknown", out["authenticated"]
    assert out["markers_matched"] == []


def test_institution_in_unrelated_context_is_not_evidence():
    """机构名出现在新闻/页脚里不算认证——必须落在"像认证信息"的上下文里。"""
    db = get("sd")
    html = ("<html><title>ScienceDirect</title><body>" + "z" * 3000
            + "<div>河海大学举办第 60 届运动会</div></body></html>")
    out = verify_page(db, FakeResp(html), CFG)
    assert out["authenticated"] == "unknown", out["authenticated"]


def test_real_orgname_marker_is_evidence_with_snippet():
    """ScienceDirect 实测标记：页面内嵌 JSON 里的 orgName。"""
    db = get("sd")
    html = ('<html><title>ScienceDirect</title><script>{"userName":"","orgName":"Hohai University",'
            '"webUserId":"1968895"}</script><body>' + "q" * 3000 + "</body></html>")
    out = verify_page(db, FakeResp(html), CFG)
    assert out["authenticated"] is True
    assert any("orgName" in e["snippet"] for e in out["evidence"])


def test_vpn_url_is_carried_through():
    """走 WebVPN 判定时必须把签名入口地址带回结果里（实测发现被漏掉过一次）。"""
    db = get("sd")
    html = "<html><title>ScienceDirect</title>" + "k" * 3000 + "</html>"
    resp = FakeResp(html)
    resp.vpn_url = "https://webvpn.hhu.edu.cn/https/abc123/"
    out = verify_page(db, resp, CFG)
    assert out["vpn_url"] == "https://webvpn.hhu.edu.cn/https/abc123/"

    resp2 = FakeResp(html)                    # 直连时没有 WebVPN 地址，应为空串而不是 None
    assert verify_page(db, resp2, CFG)["vpn_url"] == ""


def test_authentication_context_rule_accepts_welcome_line():
    db = get("cnki")
    html = ("<html><title>中国知网</title><body>" + "w" * 3000
            + "<span>欢迎您：河海大学</span></body></html>")
    out = verify_page(db, FakeResp(html, url="https://www.cnki.net/"), CFG)
    assert out["authenticated"] is True
    assert out["evidence"], "命中时必须留下可核对的证据片段"


def test_invalid_rule_is_surfaced_not_silently_skipped():
    """写错的正则必须暴露（rules_invalid），而不是静默降级成子串匹配。"""
    db = get("sd")
    db.verify = {"require_any": ["河海大学|(?i)hohai"]}     # (?i) 不在开头 → 非法
    html = "<html><title>ScienceDirect</title><body>" + "v" * 3000 + "</body></html>"
    out = verify_page(db, FakeResp(html), CFG)
    assert out["rules_invalid"], "非法正则应被记录"
    assert not out["markers_matched"]


def test_calibrate_learns_markers():
    db = get("cnki")
    html = ("<html><title>中国知网</title><body>机构用户：河海大学图书馆 欢迎您</body></html>")
    fp = calibrate(db, FakeResp(html, url="https://www.cnki.net/"), CFG)
    assert fp["title"] == "中国知网"
    assert any("河海大学" in m for m in fp["markers"])
    assert fp["evidence_snippets"]


def test_fingerprint_overrides_verify():
    db = get("sd")
    fp = {"markers": ["(?i)hohai university"], "verify": {"require_any": ["(?i)hohai university"]}}
    html = "<html><title>ScienceDirect</title><body>Hohai University</body>" + "y" * 3000 + "</html>"
    out = verify_page(db, FakeResp(html), CFG, fingerprint=fp)
    assert out["authenticated"] is True


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
