"""WebVPN「按需开启 + 开着就保活 + 关机即关闭」的行为测试（全程离线）。

规则（用户明确要求）：
  R1 守护**不主动开启** WebVPN：使用者没开（intent=off）时，守护不登录、不续期；
  R2 一旦使用者开启（vpn on / ensure / open / login webvpn 成功），
     守护负责**保活**（会话过期自动重登），直到使用者 vpn off 或关机；
  R3 **关机边界**：本次开机后的第一次守护运行会关掉 WebVPN 并复位意图
     —— 即"关机后到下次开机前都是关的"；
  R4 vpn off / 注销 → 关闭会话并复位意图。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._tmp import TempDir  # noqa: E402
from hhuvpn.config import load_config  # noqa: E402
from hhuvpn.connector import Connector  # noqa: E402
from hhuvpn.guard import Guard  # noqa: E402
from hhuvpn.state import State  # noqa: E402


class FakeVPN:
    """替身：记录被调用的次数，绝不联网。"""

    def __init__(self, valid=False, login_ok=True, ticket="fake-ticket"):
        self.calls = {"is_authenticated": 0, "login": 0, "logout": 0}
        self._valid = valid
        self._login_ok = login_ok
        self._ticket = ticket

    def ticket(self):
        return self._ticket

    def is_authenticated(self, timeout=15):
        self.calls["is_authenticated"] += 1
        return (self._valid, {"reason": "portal_ok" if self._valid else "redirected_to_cas",
                              "final_url": "https://webvpn.hhu.edu.cn/"})

    def login_with_credentials(self, username, password, captcha="", debug_dump_dir=None,
                              timeout=25):
        self.calls["login"] += 1
        if self._login_ok:
            self._ticket = "fresh-ticket"
            self._valid = True
            return {"ok": True, "action": "logged_in", "message": "WebVPN 登录成功"}
        return {"ok": False, "action": "failed", "error_code": "auth_rejected",
                "message": "统一身份认证被拒"}

    def logout(self, timeout=15):
        self.calls["logout"] += 1
        self._ticket = ""
        self._valid = False
        return {"ok": True}


def _guard(tmp: Path, vpn: FakeVPN) -> Guard:
    """构造一个完全自包含的 Guard（两处都要小心）：

    1. 替换 Guard **自己**那个 Connector 里的替身——Guard 会新建 Connector；
    2. **必须自带假凭据**并把 state_dir 指向临时目录。否则 resolve_credentials 会去读
       本机的 DPAPI 凭据 / 环境变量：本机有真实凭据时测试"通过"，换台机器就失败
       （打包验证在干净副本里跑时正是这样暴露的——测试绝不能依赖本机隐私状态）。
    """
    (tmp / "config.ini").write_text(
        f"[account]\nusername=test-user\npassword=test-pass\n"
        f"[agent]\nstate_dir = {tmp / 'state'}\n", encoding="utf-8")
    cfg = load_config(tmp / "config.ini")
    state = State(tmp / "state")
    guard = Guard(cfg, state, quiet=True)
    guard.conn.campus.online = lambda timeout=5.0: True    # 校园网已在线，不触发校园网登录
    guard.conn.webvpn = vpn
    return guard


def test_guard_helper_is_hermetic():
    """回归：测试自带假凭据，不得依赖本机 DPAPI / 环境变量里的真实凭据。

    （打包验证时踩到：在开发机上因为存在真实 credentials.dpapi，
      R2 这类"需要凭据"的用例会假通过；换到干净副本立刻失败。）
    """
    from hhuvpn.config import resolve_credentials
    with TempDir() as tmp:
        vpn = FakeVPN(ticket="")
        guard = _guard(tmp, vpn)
        creds = resolve_credentials(guard.conn.cfg, allow_prompt=False)
        assert creds.source == "config", f"凭据来源应为临时配置，实际 {creds.source}"
        assert creds.username == "test-user"
        assert str(guard.conn.cfg.state_dir).startswith(str(tmp))


def test_intent_defaults_to_off():
    with TempDir() as tmp:
        state = State(tmp / "state")
        assert state.webvpn_intent()["intent"] == "off"
        assert state.webvpn_wanted is False


def test_R1_guard_does_not_open_webvpn_when_off():
    with TempDir() as tmp:
        vpn = FakeVPN(ticket="")
        guard = _guard(tmp, vpn)
        guard._boot_marker = lambda: "boot-1"
        out = guard.tick(close_on_boot=False)
        assert vpn.calls["login"] == 0, "使用者没开，守护绝不该去登录 WebVPN"
        assert out["webvpn"]["managed"] is False
        assert out["webvpn"]["intent"] == "off"


def test_R2_guard_keeps_alive_once_user_turned_it_on():
    with TempDir() as tmp:
        vpn = FakeVPN(valid=False, ticket="old-ticket")
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard._boot_marker = lambda: "boot-1"
        out = guard.tick(close_on_boot=False)
        assert vpn.calls["login"] == 1, "开着就该保活：会话失效要自动重登"
        assert out["webvpn"]["state"] == "relogged"
        assert guard.state.webvpn_wanted is True, "保活不该改变使用者的开启意图"


def test_R2b_guard_leaves_valid_session_alone():
    with TempDir() as tmp:
        vpn = FakeVPN(valid=True)
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard._boot_marker = lambda: "boot-1"
        out = guard.tick(close_on_boot=False)
        assert vpn.calls["login"] == 0
        assert out["webvpn"]["state"] == "ok"


def test_R5_close_during_inflight_tick_wins():
    """竞态回归（实测踩到过）：tick 开始后使用者执行了 vpn off，
    这轮保活不得把会话重新拉起来——"关闭"必须压得过在途的保活。
    """
    with TempDir() as tmp:
        vpn = FakeVPN(valid=False, ticket="")
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard._boot_marker = lambda: "boot-1"

        # 让 is_authenticated 在被调用的瞬间模拟"使用者此刻关闭了 WebVPN"
        def flip_and_report(timeout=15):
            guard.state.set_webvpn_intent(False, by="vpn off")
            return (False, {"reason": "redirected_to_cas"})

        vpn.is_authenticated = flip_and_report
        out = guard.tick(close_on_boot=False)

        assert vpn.calls["login"] == 0, "使用者已关闭，在途 tick 不该再登录"
        assert out["webvpn"]["state"] == "aborted"
        assert guard.state.webvpn_wanted is False


def test_R5b_rollback_if_closed_during_login():
    """极端情况：登录请求已经发出、期间使用者又关了 —— 登录成功后必须回滚成关闭。"""
    with TempDir() as tmp:
        vpn = FakeVPN(valid=False, ticket="")
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard._boot_marker = lambda: "boot-1"

        def login_then_user_closes(username, password, captcha="", debug_dump_dir=None,
                                  timeout=25):
            vpn.calls["login"] += 1
            vpn._ticket = "fresh-ticket"                         # 登录确实拿到了会话
            guard.state.set_webvpn_intent(False, by="vpn off")   # 但使用者在此期间关了
            return {"ok": True, "action": "logged_in", "message": "ok"}

        vpn.login_with_credentials = login_then_user_closes
        out = guard.tick(close_on_boot=False)

        assert out["webvpn"]["state"] == "rolled_back"
        assert guard.state.webvpn_wanted is False, "回滚后意图必须仍是关闭"
        assert vpn.calls["logout"] >= 1, "回滚要真的把会话注销掉"


def test_R3_first_run_after_reboot_closes_webvpn():
    with TempDir() as tmp:
        vpn = FakeVPN(valid=True)
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard.state.save_session([{"name": "wengine_vpn_ticketwebvpn_hhu_edu_cn",
                                   "value": "t", "domain": ".webvpn.hhu.edu.cn", "path": "/"}],
                                 {"username": "u"})
        guard._boot_marker = lambda: "boot-NEW"
        (guard.state.dir / "boot_marker.txt").write_text("boot-OLD", encoding="utf-8")

        out = guard.tick(close_on_boot=True)

        assert vpn.calls["logout"] == 1, "开机后应把上一次开机留下的 WebVPN 关掉"
        assert guard.state.webvpn_wanted is False, "关机边界要复位开启意图"
        assert not guard.state.session_path.exists(), "本地会话应被清掉"
        assert out["actions"][0]["action"] == "close_webvpn_on_boot"


def test_R3b_first_ever_run_only_records_boot_marker():
    """第一次启用守护时不该掐掉使用者此刻正开着的会话。"""
    with TempDir() as tmp:
        vpn = FakeVPN(valid=True)
        guard = _guard(tmp, vpn)
        guard.state.set_webvpn_intent(True, by="test")
        guard._boot_marker = lambda: "boot-1"
        out = guard.tick(close_on_boot=True)          # 没有旧 marker
        assert vpn.calls["logout"] == 0
        assert guard.state.webvpn_wanted is True
        assert out["boot"]["first_since_boot"] is False
        assert (guard.state.dir / "boot_marker.txt").read_text(encoding="utf-8") == "boot-1"
        # 同一开机内的后续运行也不关
        out2 = guard.tick(close_on_boot=True)
        assert vpn.calls["logout"] == 0 and out2["boot"]["first_since_boot"] is False


def test_R4_connector_close_resets_intent_and_session():
    with TempDir() as tmp:
        cfg = load_config(tmp / "config.ini", create=True)
        state = State(tmp / "state")
        conn = Connector(cfg, state)
        conn.webvpn = FakeVPN(valid=True)
        state.set_webvpn_intent(True, by="test")
        state.save_session([{"name": "wengine_vpn_ticketwebvpn_hhu_edu_cn", "value": "t",
                             "domain": ".webvpn.hhu.edu.cn", "path": "/"}], {"username": "u"})

        out = conn.close_webvpn("使用者主动关闭")

        assert out["closed"] is True and out["had_session"] is True
        assert out["remote_logout"]["ok"] is True
        assert state.webvpn_wanted is False
        assert not state.session_path.exists()
        assert conn.webvpn.ticket() == ""


def test_intent_file_survives_and_is_readable():
    with TempDir() as tmp:
        state = State(tmp / "state")
        state.set_webvpn_intent(True, by="vpn-on")
        raw = json.loads((state.dir / "webvpn_intent.json").read_text(encoding="utf-8"))
        assert raw["intent"] == "on" and raw["by"] == "vpn-on"
        assert State(tmp / "state").webvpn_wanted is True


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
