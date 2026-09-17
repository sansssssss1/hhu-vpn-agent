"""WebVPN 网址签名、反解与登录页解析测试（全部离线，不碰网络）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hhuvpn.webvpn import (KEY_HEX, WebVPNClient, sign_host, unwrap_vpn_url, vpn_url)  # noqa: E402

BASE = "https://webvpn.hhu.edu.cn"
LIVE_CAS_SIG = "77726476706e69737468656265737421f1e2559434357a467b1ac7a490406d301894467e2b"


def test_sign_host_shape():
    sig = sign_host("www.cnki.net")
    assert sig.startswith(KEY_HEX)
    assert len(sig) == 32 + 2 * len("www.cnki.net")
    assert sig == sign_host("www.cnki.net")          # 确定性


def test_sign_live_vector():
    assert sign_host("authserver.hhu.edu.cn") == LIVE_CAS_SIG


def test_vpn_url_roundtrip():
    target = "https://www.sciencedirect.com/science/article/pii/S0000000000000000?dgcid=author"
    v = vpn_url(BASE, target)
    assert v.startswith(BASE + "/https/")
    info = unwrap_vpn_url(BASE, v)
    assert info["target"] == target
    assert info["host"] == "www.sciencedirect.com"


def test_vpn_url_keeps_port_and_scheme():
    v = vpn_url(BASE, "http://lib.hhu.edu.cn:8080/a/b?x=1")
    assert "/http/" in v and ":8080" in v
    assert unwrap_vpn_url(BASE, v)["target"] == "http://lib.hhu.edu.cn:8080/a/b?x=1"


def test_unwrap_rejects_non_vpn():
    assert unwrap_vpn_url(BASE, "https://www.cnki.net/") is None


def test_parse_login_form():
    cfg = type("C", (), {"webvpn_base": BASE})()
    client = WebVPNClient.__new__(WebVPNClient)
    client.cfg = cfg
    client.base = BASE
    html = (
        '<input type="hidden" id="pwdEncryptSalt" value="676kS0VeaLq30Rlg" />'
        '<input type="hidden" name="execution" value="e1s1" />'
        '<input type="hidden" name="lt" value="" />'
        'var captchaSwitch = "2";'
        'var service = ["https://webvpn.hhu.edu.cn/login?cas_login=true"];'
        '<form class="loginFromClass" method="post" id="loginFromId" '
        'action="/https/abc/authserver/login">'
    )
    form = WebVPNClient._read_login_form(client, html,
                                         "https://webvpn.hhu.edu.cn/https/abc/authserver/login"
                                         "?service=https%3A%2F%2Fwebvpn.hhu.edu.cn%2Flogin")
    assert form["salt"] == "676kS0VeaLq30Rlg"
    assert form["execution"] == "e1s1"
    assert form["captcha_switch"] == "2"
    assert form["service"] == "https://webvpn.hhu.edu.cn/login?cas_login=true"
    assert client._prefix_of("https://webvpn.hhu.edu.cn/https/abc/authserver/login") == \
        "https://webvpn.hhu.edu.cn/https/abc"


def test_extract_error_variants():
    client = WebVPNClient.__new__(WebVPNClient)
    assert WebVPNClient._extract_error(client, '<span id="showErrorTip">用户名或密码错误</span>') == "用户名或密码错误"
    assert WebVPNClient._extract_error(client, '{"message":"captchaError"}') == "验证码错误"
    assert WebVPNClient._extract_error(client, "alert('账号已被锁定')") == "账号已被锁定"
    assert WebVPNClient._extract_error(client, "<html>nothing</html>") == ""


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
