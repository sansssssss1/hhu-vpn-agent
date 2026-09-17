"""校园网与 WebVPN 是同一账号——把这个事实带来的行为锁进测试。

三条约定：
  1. 凭据只有一份（config / env / DPAPI 都作用于两个服务），报告里带 scope；
  2. 校园网认证已被拒时，同一份密码不再去撞 WebVPN
     （两边都撞 = 失败次数翻倍，更容易把账号推进滑块验证码）；
  3. 校园网只是"没上网/链路不通"这类非凭据问题时，不该阻止 WebVPN 登录。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._tmp import TempDir  # noqa: E402
from hhuvpn.campus import CampusResult  # noqa: E402
from hhuvpn.config import Credentials, load_config  # noqa: E402
from hhuvpn.connector import Connector  # noqa: E402
from hhuvpn.state import State  # noqa: E402


class _EnvCreds:
    """临时给一份账号（ensure 内部会重新解析凭据，走 env 最省事）。"""

    def __enter__(self):
        self._old = {k: os.environ.get(k) for k in ("HHU_USERNAME", "HHU_PASSWORD")}
        os.environ["HHU_USERNAME"] = "2401010101"
        os.environ["HHU_PASSWORD"] = "definitely-not-the-real-password"
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _connector(tmp: Path, campus_result: CampusResult, online: bool = False):
    cfg = load_config(tmp / "config.ini", create=True)
    conn = Connector(cfg, State(tmp / "state"))
    conn.campus.online = lambda timeout=5.0: online           # 不碰网络
    conn.campus.login = lambda *a, **k: campus_result
    calls = {"webvpn": 0}

    def fake_ensure_webvpn(creds, deep=True, force=False, by=None):
        calls["webvpn"] += 1
        return {"name": "webvpn", "action": "logged_in", "ok": True, "message": "fake",
                "by": by}

    conn.ensure_webvpn = fake_ensure_webvpn
    return conn, calls


def test_credentials_scope_is_campus_plus_webvpn():
    c = Credentials("2401010101", "pw", "config")
    d = c.to_dict()
    assert d["scope"] == "campus+webvpn"
    assert "password" not in d, "凭据对象绝不能被序列化出密码"


def test_rejected_credentials_skip_second_login():
    with TempDir() as tmp:
        rejected = CampusResult(action="failed", ok=False, error_code="auth_rejected",
                                message="该账号非常用账号或用户名密码有误")
        conn, calls = _connector(tmp, rejected)
        with _EnvCreds():
            report = conn.ensure(targets=[], verify_dbs=False)
        assert calls["webvpn"] == 0, "凭据被拒后不该再拿同一份密码去撞 WebVPN"
        webvpn_step = [s for s in report["steps"] if s["name"] == "webvpn"][0]
        assert webvpn_step["error_code"] == "auth_rejected"
        assert webvpn_step.get("shared_credentials") is True
        assert "同一账号" in webvpn_step["message"]
        assert report["account_scope"].startswith("campus+webvpn")


def test_captcha_on_campus_also_skips_second_login():
    with TempDir() as tmp:
        captcha = CampusResult(action="failed", ok=False, error_code="captcha_required",
                               message="认证被拒：已触发验证码")
        conn, calls = _connector(tmp, captcha)
        with _EnvCreds():
            conn.ensure(targets=[], verify_dbs=False)
        assert calls["webvpn"] == 0


def test_non_credential_campus_failure_still_tries_webvpn():
    """不在校园网（门户不可达）与密码无关：WebVPN 照样该试——校外就是这么用的。"""
    with TempDir() as tmp:
        portal_down = CampusResult(action="failed", ok=False, error_code="portal_unreachable",
                                   message="认证门户不可达")
        conn, calls = _connector(tmp, portal_down)
        with _EnvCreds():
            report = conn.ensure(targets=[], verify_dbs=False)
        assert calls["webvpn"] == 1
        assert report["webvpn_ok"] is True


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
