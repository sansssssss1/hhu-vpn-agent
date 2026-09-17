"""常驻守护：让「校园网自动登录 + WebVPN 会话可用」脱离 agent 独立运行。

这是给人用的独立功能，和 agent 无关：装一次计划任务（standalone/安装守护任务.cmd），
之后开机自动生效——校园网掉线自动重登，WebVPN 会话过期自动重登，
需要时还可以定时确认目标文献数据库是否仍然可访问。

设计上刻意复用上游 hhu-autologin 的两条经验：
  1. 先判在线再动手（已在线时零副作用地退出，不碰门户）；
  2. 密码被服务器拒绝就立刻断路（写 auth_failed.flag 停止重试），
     否则每分钟撞接口会触发验证码，把人锁在门外。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, resolve_credentials
from .connector import Connector
from .state import State

AUTH_FLAG = "auth_failed.flag"
GUARD_SNAPSHOT = "guard.json"

_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode($env:TOAST_TITLE)) | Out-Null
$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode($env:TOAST_MSG)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def toast(title: str, message: str) -> None:
    """Windows 原生通知；失败静默（通知只是锦上添花）。"""
    if sys.platform != "win32":
        return
    try:
        import os
        env = {**os.environ, "TOAST_TITLE": title[:64], "TOAST_MSG": message[:180]}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST],
                       capture_output=True, timeout=15, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass


class Guard:
    def __init__(self, cfg: Config, state: State | None = None, quiet: bool = False):
        self.cfg = cfg
        self.quiet = quiet
        self.state = state or State(cfg.state_dir, logger=None if quiet else self._echo)
        self.conn = Connector(cfg, self.state, verbose=False)
        self._last_webvpn_check = 0.0
        self._last_db_check = 0.0

    # ---------------------------------------------------------------- 基础设施
    def _echo(self, msg: str) -> None:
        if not self.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    @property
    def auth_flag(self) -> Path:
        return self.state.dir / AUTH_FLAG

    def circuit_open(self) -> bool:
        return self.auth_flag.exists()

    def trip_circuit(self, reason: str) -> None:
        try:
            self.auth_flag.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def clear_circuit(self) -> bool:
        if self.auth_flag.exists():
            self.auth_flag.unlink()
            return True
        return False

    def _snapshot(self, payload: dict) -> None:
        try:
            (self.state.dir / GUARD_SNAPSHOT).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 关机/开机边界
    def _boot_marker(self) -> str:
        """本次开机的标识（近似开机时刻，分钟级）。取不到返回空串。"""
        try:
            if sys.platform == "win32":
                import ctypes
                ms = ctypes.windll.kernel32.GetTickCount64()
                return str(int((time.time() - ms / 1000.0) // 60))
            with open("/proc/uptime", "r", encoding="ascii") as fh:
                up = float(fh.read().split()[0])
            return str(int((time.time() - up) // 60))
        except Exception:  # noqa: BLE001
            return ""

    @property
    def _boot_file(self) -> Path:
        return self.state.dir / "boot_marker.txt"

    def close_webvpn(self, reason: str, notify: bool = False) -> dict:
        """关掉 WebVPN（委托 Connector，保证与 vpn off / logout 同一条代码路径）。"""
        out = self.conn.close_webvpn(reason)
        self._echo(f"WebVPN 已关闭（{reason}）")
        if notify and out.get("had_session"):
            toast("WebVPN 已关闭", reason)
        return out

    # ---------------------------------------------------------------- 一轮
    def tick(self, verify_dbs=(), webvpn_check_minutes: int = 15,
             db_check_minutes: int = 0, notify: bool = True,
             webvpn_keepalive: bool | None = None,
             close_on_boot: bool | None = None) -> dict:
        """跑一轮。

        行为（按用户要求）：
          - 校园网：**常驻自动登录**，掉线即重登；
          - WebVPN：**按需开启 + 开着就保活**——使用者没开就完全不碰；
            一旦开过（state/webvpn_intent.json = on），守护负责续期，
            直到使用者 vpn off 或关机；
          - 开机边界：本次开机后的第一次运行会**关掉 WebVPN 并复位开启意图**，
            保证"关机后到下次开机前都是关的"。
        webvpn_keepalive / close_on_boot 传 None 时取配置默认值。
        """
        started = time.time()
        if webvpn_keepalive is None:
            webvpn_keepalive = self.cfg.webvpn_keepalive
        if close_on_boot is None:
            close_on_boot = self.cfg.webvpn_close_on_boot
        out: dict = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "actions": []}

        # 0) 开机边界：本次开机后的第一次运行先关掉 WebVPN
        if close_on_boot:
            marker = self._boot_marker()
            try:
                prev = self._boot_file.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                prev = ""
            # 只在"确实换了开机标识"时才关：
            # 第一次启用守护（没有旧标识）只记录，不掐掉使用者此刻正开着的会话。
            first_since_boot = bool(marker) and bool(prev) and marker != prev
            out["boot"] = {"marker": marker, "prev": prev, "first_since_boot": first_since_boot}
            if first_since_boot:
                out["actions"].append({"action": "close_webvpn_on_boot",
                                       **self.close_webvpn("开机后保持关闭，等使用者需要时再开启",
                                                           notify=notify)})
            try:
                self._boot_file.write_text(marker or "unknown", encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

        # 1) 校园网
        if self.conn.campus.online():
            out["campus"] = {"state": "online"}
        elif not self.cfg.campus_enabled:
            out["campus"] = {"state": "offline", "note": "campus.enabled=false，不自动登录"}
        elif self.circuit_open():
            out["campus"] = {"state": "offline", "note": "已断路（凭据被拒），等待人工处理"}
        else:
            creds = resolve_credentials(self.cfg, allow_prompt=False)
            if not creds.complete:
                out["campus"] = {"state": "offline", "note": "缺少账号密码，无法自动登录"}
            else:
                self._echo("校园网离线，尝试自动登录…")
                res = self.conn.campus.login(creds.username, creds.password)
                out["campus"] = {"state": "online" if res.ok else "offline",
                                 "action": res.action, "message": res.message}
                if res.ok:
                    self._echo("校园网已恢复")
                    if notify:
                        toast("校园网已自动登录", "检测到掉线并已自动恢复")
                    self.clear_circuit()
                elif res.error_code == "auth_rejected":
                    self.trip_circuit(res.message)
                    self._echo(f"凭据被拒，已断路停止重试：{res.message}")
                    if notify:
                        toast("校园网自动登录失败", res.message[:180])
                elif notify:
                    toast("校园网自动登录失败", (res.message or res.error_code)[:180])

        # 2) WebVPN：按需开启 + **开着就保活**
        #    - 使用者没开（intent=off）：守护完全不碰，不建立也不续期；
        #    - 使用者开过（intent=on）：守护负责续期，直到 vpn off 或关机。
        intent = self.state.webvpn_intent()
        out["webvpn_intent"] = intent
        wanted = intent.get("intent") == "on"
        if not wanted:
            self.conn.load_session()
            has_ticket = bool(self.conn.webvpn.ticket())
            out["webvpn"] = {
                "state": "on" if has_ticket else "off",
                "managed": False, "keepalive": False, "intent": "off",
                "note": ("按需模式：使用者尚未开启，守护不主动开、也不保活"
                         + ("（本地还留有会话，可用 hhuvpn vpn status 认领或 vpn off 关掉）"
                            if has_ticket else
                            "；用 hhuvpn vpn on（或 ensure / open）开启后，守护会保活到 vpn off 或关机"))}
            self._last_webvpn_check = time.time()
            session_active = has_ticket
        elif not webvpn_keepalive:
            self.conn.load_session()
            out["webvpn"] = {"state": "on" if self.conn.webvpn.ticket() else "off",
                             "managed": False, "keepalive": False, "intent": "on",
                             "note": "已开启，但配置里关掉了保活（webvpn.keepalive=false）"}
            session_active = bool(self.conn.webvpn.ticket())
        else:
            out["webvpn_mode"] = "keepalive"
            session_active = self._tick_webvpn_keepalive(out, webvpn_check_minutes, verify_dbs)
            self.state.log(f"guard keepalive tick: session_active={session_active}")

        # 3) 目标库抽查（默认关闭；开了就是「定期确认还登得上」）
        targets = list(verify_dbs or [])
        if targets and not session_active:
            # WebVPN 没开就别拿签名地址去撞：那只会拿回统一身份认证页，徒增噪音
            out["databases"] = [{"alias": a, "skipped": True,
                                 "reason": "WebVPN 当前未开启（按需模式），跳过库检查"}
                                for a in targets]
        elif targets and (time.time() - self._last_db_check) >= max(1, db_check_minutes) * 60:
            self._last_db_check = time.time()
            checks = []
            for alias in targets:
                r = self.conn.database_status(alias, save=False)
                checks.append({"alias": alias, "authenticated": r.get("authenticated"),
                               "reachable": r.get("reachable"), "note": r.get("note", "")})
            out["databases"] = checks
            self._echo("库检查：" + ", ".join(
                f"{c['alias']}={c['authenticated']}" for c in checks))
        elif targets:
            out["databases"] = [{"alias": a, "skipped": True, "reason": "未到检查间隔"}
                                for a in targets]

        out["elapsed_ms"] = int((time.time() - started) * 1000)
        self._snapshot(out)
        return out

    # ---------------------------------------------------------------- WebVPN 保活（可选）
    def _tick_webvpn_keepalive(self, out: dict, webvpn_check_minutes: int, verify_dbs) -> bool:
        """旧的"守护也保活 WebVPN"行为，仅在显式开启时使用。返回会话是否可用。"""
        due = (time.time() - self._last_webvpn_check) >= max(1, webvpn_check_minutes) * 60
        if self.cfg.webvpn_enabled and (due or verify_dbs):
            self._last_webvpn_check = time.time()
            # 记下本轮的"开启意图版本"：登录前要再核对一次。
            # 实测踩过：使用者执行 vpn off 的同一秒，计划任务里已启动的 tick 会拿旧意图
            # 把会话重新拉起来——"关闭"必须压得过在途的保活。
            epoch = self.state.webvpn_intent().get("since")
            self.conn.load_session()
            ok, info = self.conn.webvpn.is_authenticated()
            if ok:
                out["webvpn"] = {"state": "ok", "reason": info.get("reason"), "keepalive": True}
                return True
            self.state.log(f"guard: webvpn 会话失效（{info.get('reason')}），按开启意图重登")
            if self.circuit_open():
                out["webvpn"] = {"state": "expired", "note": "已断路，不重新登录",
                                 "keepalive": True}
                return False
            else:
                creds = resolve_credentials(self.cfg, allow_prompt=False)
                if not creds.complete:
                    out["webvpn"] = {"state": "expired", "note": "缺少账号密码",
                                     "keepalive": True}
                    return False
                else:
                    current = self.state.webvpn_intent()
                    if current.get("intent") != "on" or current.get("since") != epoch:
                        out["webvpn"] = {"state": "aborted", "keepalive": True,
                                         "note": "使用者在此期间关闭了 WebVPN，放弃保活重登"}
                        self._echo("检测到使用者已关闭 WebVPN，放弃本次保活重登")
                        return False
                    self._echo("WebVPN 会话失效，重新登录…")
                    res = self.conn.webvpn.login_with_credentials(creds.username, creds.password)
                    if res.get("ok"):
                        self.conn.save_session({"username": creds.username})
                        # 注意：保活**不改写**使用者的开启意图（意图是使用者的，不是守护的）。
                        # 登录期间如果使用者关了，立刻回滚——不能让它"复活"。
                        if self.state.webvpn_intent().get("intent") != "on":
                            self.close_webvpn("保活重登期间使用者关闭了 WebVPN，已回滚")
                            out["webvpn"] = {"state": "rolled_back", "keepalive": True,
                                             "note": "重登期间被关闭，已回滚为关闭状态"}
                            return False
                        out["webvpn"] = {"state": "relogged", "action": res.get("action"),
                                         "keepalive": True}
                        self._echo("WebVPN 已重新登录")
                        return True
                    else:
                        out["webvpn"] = {"state": "expired", "message": res.get("message", ""),
                                         "error_code": res.get("error_code", "")}
                        if res.get("error_code") == "auth_rejected":
                            self.trip_circuit(str(res.get("message")))
                        self._echo(f"WebVPN 重登失败：{res.get('message') or res.get('error_code')}")
                        return False
        out["webvpn"] = {"state": "skipped", "keepalive": True}
        return bool(self.conn.webvpn.ticket())

    # ---------------------------------------------------------------- 循环
    def run(self, interval_minutes: int = 1, once: bool = False, max_minutes: int = 0,
            verify_dbs=(), webvpn_check_minutes: int = 15, db_check_minutes: int = 0,
            notify: bool = True, on_tick=None) -> int:
        deadline = time.time() + max_minutes * 60 if max_minutes else 0
        fail_streak = 0
        while True:
            try:
                out = self.tick(verify_dbs=verify_dbs, webvpn_check_minutes=webvpn_check_minutes,
                                db_check_minutes=db_check_minutes, notify=notify)
            except KeyboardInterrupt:
                self._echo("收到中断，退出守护")
                return 0
            except Exception as exc:  # noqa: BLE001
                self._echo(f"本轮异常（已忽略，继续守护）：{exc!r}")
                out = {"error": repr(exc)}
            if on_tick:
                try:
                    on_tick(out)
                except Exception:  # noqa: BLE001
                    pass
            campus_ok = (out.get("campus") or {}).get("state") == "online"
            fail_streak = 0 if campus_ok else fail_streak + 1
            if once:
                return 0 if campus_ok or not self.cfg.campus_enabled else 1
            if deadline and time.time() >= deadline:
                self._echo(f"达到运行时长上限（{max_minutes} 分钟），退出守护")
                return 0
            try:
                time.sleep(max(10, interval_minutes * 60))
            except KeyboardInterrupt:
                self._echo("收到中断，退出守护")
                return 0


__all__ = ["Guard", "toast", "AUTH_FLAG", "GUARD_SNAPSHOT"]
