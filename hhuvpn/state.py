"""本地状态：会话 cookie、校准指纹、运行日志。

落盘位置默认 <项目>/state/，可用 config.ini [agent] state_dir 改到别处。
里面含登录态 cookie，README 里明确写了「不要外传/不要提交」。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA = 1
SESSION_FILE = "session.json"
FINGERPRINT_FILE = "fingerprints.json"
STATUS_FILE = "status.json"
LOG_FILE = "hhuvpn.log"
INTENT_FILE = "webvpn_intent.json"


class State:
    def __init__(self, state_dir: Path, logger=None):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._log_sink = logger

    # ---------------------------------------------------------------- 路径
    @property
    def session_path(self) -> Path:
        return self.dir / SESSION_FILE

    @property
    def fingerprint_path(self) -> Path:
        return self.dir / FINGERPRINT_FILE

    @property
    def status_path(self) -> Path:
        return self.dir / STATUS_FILE

    @property
    def log_path(self) -> Path:
        return self.dir / LOG_FILE

    def _read(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 会话
    def load_session(self) -> dict:
        data = self._read(self.session_path)
        return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}

    def save_session(self, cookies: list, meta: dict | None = None) -> Path:
        existing = self.load_session()
        data = {"schema": SCHEMA, "saved_at": time.time(),
                "saved_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cookies": cookies,
                "meta": meta if meta is not None else existing.get("meta", {})}
        self._write(self.session_path, data)
        return self.session_path

    def clear_session(self) -> None:
        if self.session_path.exists():
            self.session_path.unlink()

    # ---------------------------------------------------------------- WebVPN 开启意图
    def set_webvpn_intent(self, on: bool, by: str = "") -> Path:
        """记录"使用者是否要求开着 WebVPN"。

        这是「按需开启 + 开着就保活」模型的状态位：
          - 只有使用者显式开启（vpn on / ensure / open / login webvpn）才会置 on；
          - vpn off、注销、以及开机后的首次守护运行会置 off；
          - 守护据此决定"要不要保活"：on 就续期，off 就完全不碰。
        """
        path = self.dir / INTENT_FILE
        self._write(path, {"intent": "on" if on else "off", "by": by,
                           "since": time.time(),
                           "since_iso": time.strftime("%Y-%m-%d %H:%M:%S")})
        return path

    def webvpn_intent(self) -> dict:
        data = self._read(self.dir / INTENT_FILE)
        return data if data.get("intent") in ("on", "off") else {"intent": "off",
                                                                 "by": "default"}

    @property
    def webvpn_wanted(self) -> bool:
        return self.webvpn_intent().get("intent") == "on"

    # ---------------------------------------------------------------- 指纹
    def fingerprints(self) -> dict:
        data = self._read(self.fingerprint_path)
        return data.get("databases", {}) if isinstance(data, dict) else {}

    def get_fingerprint(self, alias: str) -> dict:
        return self.fingerprints().get(alias, {})

    def set_fingerprint(self, alias: str, fp: dict) -> Path:
        data = self._read(self.fingerprint_path)
        data.setdefault("schema", SCHEMA)
        data.setdefault("databases", {})
        data["databases"][alias] = {**fp, "calibrated_at": time.time(),
                                    "calibrated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._write(self.fingerprint_path, data)
        return self.fingerprint_path

    # ---------------------------------------------------------------- 状态快照 / 日志
    def save_status(self, payload: dict) -> Path:
        self._write(self.status_path, {"schema": SCHEMA, "saved_at": time.time(), **payload})
        return self.status_path

    def load_status(self) -> dict:
        return self._read(self.status_path)

    def log(self, message: str) -> None:
        line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(message)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 512 * 1024:
                self.log_path.write_bytes(self.log_path.read_bytes()[-64 * 1024:])
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
        if self._log_sink:
            self._log_sink(line)


__all__ = ["State", "SCHEMA"]
