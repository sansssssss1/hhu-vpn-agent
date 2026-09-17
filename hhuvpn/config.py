"""配置与凭据解析。

配置文件与上游 hhu-autologin 完全兼容：同一份 config.ini 既给 `upstream/hhu_login.py`
当守护配置，也给 `hhuvpn` 当连接器配置。优先顺序：

    --config 参数  >  $HHUVPN_CONFIG  >  项目根目录 config.ini  >  ~/.hhu-vpn/config.ini

凭据解析顺序（先命中者胜出）：
    1. 环境变量 HHU_USERNAME / HHU_PASSWORD（CI、临时测试最安全，不落盘）
    2. config.ini [account]
    3. DPAPI 加密凭据文件（Windows，仅当前用户可解，见 secret.py）
    4. 交互式输入（仅当 stdin 是终端）
"""

from __future__ import annotations

import base64
import configparser
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATHS = (
    PROJECT_ROOT / "config.ini",
    Path.home() / ".hhu-vpn" / "config.ini",
)

CONFIG_TEMPLATE = """# hhu-vpn-agent 配置（与上游 hhu-autologin 的 config.ini 兼容，同一个文件两用）
# 敏感信息：本文件含明文密码时请勿外传/提交到 git。

[account]
; 学号 / 工号（信息门户账号，校园网与 WebVPN 通用）
username =
; 信息门户密码（校园网认证 与 WebVPN 统一身份认证 同一个密码）
password =
; 校园网服务关键词：校园网 / 移动 / 电信 / 联通（自动匹配登录页选项，三校区通用）
service = 校园网

[campus]
; 是否允许 hhuvpn 自动登录校园网 ePortal
enabled = true
; 在线判定用的 204 探针（校园网会劫持 http 请求，未认证时拿不到 204）
probe_url = http://connect.rom.miui.com/generate_204
; 认证门户
eportal_host = http://eportal.hhu.edu.cn
; 仅当连接指定 WiFi 时才自动登录（留空 = 不检查，接网线请留空）
wifi_ssid = Hohai University

[webvpn]
; 是否允许 hhuvpn 自动登录学校 WebVPN
enabled = true
base_url = https://webvpn.hhu.edu.cn
; TLS 校验：auto = 先严格校验，失败后降级并告警；true = 必须校验；false = 不校验
tls_verify = auto
; 会话缓存时长（分钟）。到期后即使本地有 cookie 也会重新走一次线上校验
session_ttl_minutes = 120
; WebVPN 采用「按需开启 + 开着就保活」：
;   - 守护不会主动登录 WebVPN（真正的开启只有 vpn on / ensure / open / login webvpn）；
;   - 一旦开启，keepalive = true（默认）时由守护负责续期，
;     直到使用者 vpn off 或关机为止——"不主动开，但开着就别掉"。
keepalive = true
; 本次开机后的第一次守护运行，是否先把 WebVPN 关掉（远端注销 + 清本地会话 + 复位开启意图）。
; true（默认）= 关机后到下次开机前保持关闭，直到使用者明确要用。
close_on_boot = true
; 说明：以上开关只影响"要不要保活"，都不会让守护去主动开启 WebVPN。

[databases]
; ensure 未指定 --db 时的默认目标（逗号分隔的别名，见 hhuvpn db list）
default = sd,cnki
; 机构名（用于判断"是否已用学校身份登上数据库"），多个用 | 分隔
institution = 河海大学|Hohai University

[agent]
; 状态目录（会话 cookie / 指纹 / 日志）。留空 = 项目下 state/
state_dir =
; JSON 输出缩进（agent 读起来无所谓，人看着舒服）
json_pretty = true
; 每次调用最多重试几次网络请求
retries = 1

[guard]
; 上游 hhu_login.py 的守护间隔（分钟）
interval_minutes = 1
auto_exit_after_login = false
auto_exit_minutes = 0

[notify]
enabled = true
on_success = true
on_failure = true
failure_cooldown_minutes = 30

[advanced]
debug = true
"""


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


class Config:
    """轻量配置对象。所有取值都有兜底默认，绝不因为缺项崩掉。"""

    def __init__(self, path: Path | None, parser: configparser.ConfigParser):
        self.path = path
        self._p = parser

    # ---- 通用取值 ----
    def get(self, section: str, option: str, default: str = "") -> str:
        try:
            return self._p.get(section, option, fallback=default).strip()
        except Exception:  # noqa: BLE001
            return default

    def get_bool(self, section: str, option: str, default: bool) -> bool:
        try:
            return self._p.getboolean(section, option, fallback=default)
        except Exception:  # noqa: BLE001
            return default

    def get_int(self, section: str, option: str, default: int) -> int:
        try:
            return self._p.getint(section, option, fallback=default)
        except Exception:  # noqa: BLE001
            return default

    # ---- 语义化快捷方式 ----
    @property
    def username(self) -> str:
        return self.get("account", "username")

    @property
    def password(self) -> str:
        return self.get("account", "password")

    @property
    def service_keyword(self) -> str:
        return self.get("account", "service", "校园网") or "校园网"

    @property
    def campus_enabled(self) -> bool:
        return self.get_bool("campus", "enabled", True)

    @property
    def campus_probe_url(self) -> str:
        return self.get("campus", "probe_url", "http://connect.rom.miui.com/generate_204")

    @property
    def eportal_host(self) -> str:
        return self.get("campus", "eportal_host", "http://eportal.hhu.edu.cn").rstrip("/")

    @property
    def wifi_ssid(self) -> str:
        return self.get("campus", "wifi_ssid", self.get("guard", "wifi_ssid", "Hohai University"))

    @property
    def webvpn_enabled(self) -> bool:
        return self.get_bool("webvpn", "enabled", True)

    @property
    def webvpn_base(self) -> str:
        return self.get("webvpn", "base_url", "https://webvpn.hhu.edu.cn").rstrip("/")

    @property
    def tls_verify(self) -> str:
        return (self.get("webvpn", "tls_verify", "auto") or "auto").lower()

    @property
    def session_ttl_minutes(self) -> int:
        return self.get_int("webvpn", "session_ttl_minutes", 120)

    @property
    def webvpn_keepalive(self) -> bool:
        """使用者开启 WebVPN 后，守护是否负责保活（会话过期自动续）。

        默认 true：一旦开了就一直续期，直到使用者 vpn off 或关机——
        即"不主动开，但开着就别掉"。
        """
        return self.get_bool("webvpn", "keepalive", True)

    @property
    def webvpn_close_on_boot(self) -> bool:
        """本次开机后的第一次守护运行是否关掉 WebVPN。默认 true —— 关机后保持关闭。"""
        return self.get_bool("webvpn", "close_on_boot", True)

    @property
    def default_databases(self) -> list[str]:
        raw = self.get("databases", "default", "sd,cnki")
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    @property
    def institution(self) -> str:
        return self.get("databases", "institution", "河海大学|Hohai University")

    @property
    def state_dir(self) -> Path:
        raw = self.get("agent", "state_dir")
        return Path(raw).expanduser() if raw else PROJECT_ROOT / "state"

    @property
    def json_pretty(self) -> bool:
        return self.get_bool("agent", "json_pretty", True)

    @property
    def retries(self) -> int:
        return max(0, self.get_int("agent", "retries", 1))


def find_config(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("HHUVPN_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return p
    return DEFAULT_CONFIG_PATHS[0]


def load_config(explicit: str | os.PathLike | None = None, create: bool = False) -> Config:
    path = find_config(explicit)
    parser = configparser.ConfigParser()
    parser.optionxform = str  # 保留大小写（对上游无影响，对本模块更友好）
    if path.exists():
        parser.read(path, encoding="utf-8")
    elif create:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 若项目根已有示例文件，直接用它做模板
        example = PROJECT_ROOT / "config.example.ini"
        path.write_text(example.read_text(encoding="utf-8") if example.exists() else CONFIG_TEMPLATE,
                        encoding="utf-8")
        parser.read(path, encoding="utf-8")
    return Config(path, parser)


def save_config(cfg: Config, updates: dict[str, dict[str, str]]) -> Path:
    """把若干 section/option 写回配置文件（保留其它内容）。"""
    path = cfg.path or DEFAULT_CONFIG_PATHS[0]
    parser = configparser.ConfigParser()
    parser.optionxform = str
    if path.exists():
        parser.read(path, encoding="utf-8")
    for section, opts in updates.items():
        if not parser.has_section(section):
            parser.add_section(section)
        for k, v in opts.items():
            parser.set(section, k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        parser.write(fh)
    return path


# ---------------------------------------------------------------- 凭据

class Credentials:
    def __init__(self, username: str, password: str, source: str):
        self.username = username
        self.password = password
        self.source = source          # env | config | dpapi | prompt

    @property
    def complete(self) -> bool:
        return bool(self.username and self.password)

    def to_dict(self) -> dict:
        # scope 明说一件事：校园网 ePortal 与 WebVPN 统一身份认证用的是**同一套账号**，
        # 所以配置里只需要一份，改动也只需要改一处。
        return {"username": self.username, "source": self.source,
                "has_password": bool(self.password), "scope": "campus+webvpn"}


def resolve_credentials(cfg: Config, allow_prompt: bool = False) -> Credentials:
    """按优先级解析账号密码；找不到就返回空凭据（由调用方决定是否报错）。"""
    user = os.environ.get("HHU_USERNAME") or os.environ.get("HHUVPN_USERNAME") or ""
    pwd = os.environ.get("HHU_PASSWORD") or os.environ.get("HHUVPN_PASSWORD") or ""
    if user or pwd:
        return Credentials(user or cfg.username, pwd or cfg.password, "env")

    # DPAPI 加密凭据（比明文 config 安全；存在则优先）
    try:
        from .secret import load_secret
        blob = load_secret(cfg.state_dir)
        if blob:
            u, p = blob
            return Credentials(u or cfg.username, p, "dpapi")
    except Exception:  # noqa: BLE001
        pass

    if cfg.username and cfg.password:
        return Credentials(cfg.username, cfg.password, "config")

    if allow_prompt and sys.stdin is not None and sys.stdin.isatty():
        if not cfg.username:
            print("请输入学号/工号：", end="", flush=True)
            user = sys.stdin.readline().strip()
        if not pwd and user:
            import getpass
            pwd = getpass.getpass("请输入信息门户密码（不回显）：")
        return Credentials(user, pwd, "prompt")

    return Credentials(cfg.username, "", "missing")


__all__ = ["Config", "ConfigError", "Credentials", "load_config", "save_config",
           "find_config", "resolve_credentials", "PROJECT_ROOT", "CONFIG_TEMPLATE",
           "DEFAULT_CONFIG_PATHS"]
