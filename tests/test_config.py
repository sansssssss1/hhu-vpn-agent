"""配置与凭据解析测试（离线）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import TempDir  # noqa: E402
from hhuvpn.config import Config, load_config, resolve_credentials, save_config  # noqa: E402


def _cfg(tmp: Path, text: str = "") -> Config:
    """测试用配置。

    必须把 [agent] state_dir 指到临时目录：否则 resolve_credentials 会去读**开发者本机**
    的 DPAPI 凭据（state/credentials.dpapi），"缺凭据"这类断言就永远不成立了。
    """
    p = tmp / "config.ini"
    if text:
        p.write_text(f"[agent]\nstate_dir = {tmp / 'state'}\n" + text, encoding="utf-8")
    return load_config(p, create=not text)


def test_defaults_when_empty():
    with TempDir() as d:
        cfg = _cfg(d)
        assert cfg.webvpn_base == "https://webvpn.hhu.edu.cn"
        assert cfg.default_databases == ["sd", "cnki"]
        assert cfg.service_keyword == "校园网"
        assert cfg.tls_verify == "auto"
        assert cfg.state_dir.name == "state"


def test_reads_account_section_compatible_with_upstream():
    with TempDir() as d:
        cfg = _cfg(d, "[account]\nusername=2401010101\npassword=secret\nservice=移动\n")
        assert cfg.username == "2401010101"
        assert cfg.password == "secret"
        assert cfg.service_keyword == "移动"
        creds = resolve_credentials(cfg)
        assert creds.complete and creds.source == "config"


def test_env_overrides_config():
    with TempDir() as d:
        cfg = _cfg(d, "[account]\nusername=fromfile\npassword=fromfile\n")
        os.environ["HHU_USERNAME"] = "fromenv"
        os.environ["HHU_PASSWORD"] = "envpass"
        try:
            creds = resolve_credentials(cfg)
            assert creds.username == "fromenv" and creds.password == "envpass"
            assert creds.source == "env"
        finally:
            os.environ.pop("HHU_USERNAME", None)
            os.environ.pop("HHU_PASSWORD", None)


def test_missing_credentials_reported_not_raised():
    with TempDir() as d:
        cfg = _cfg(d, "[account]\nusername=\npassword=\n")
        creds = resolve_credentials(cfg, allow_prompt=False)
        assert not creds.complete
        assert creds.to_dict()["has_password"] is False


def test_save_config_roundtrip():
    with TempDir() as d:
        cfg = _cfg(d, "[account]\nusername=a\n")
        save_config(cfg, {"account": {"username": "b"}, "extra_section": {"x": "1"}})
        cfg2 = load_config(cfg.path)
        assert cfg2.username == "b"
        assert cfg2.get("extra_section", "x") == "1"


def test_extra_sections_do_not_break_upstream_shape():
    with TempDir() as d:
        cfg = _cfg(d, "[webvpn]\nbase_url=https://vpn.example.edu.cn\n"
                       "[databases]\ndefault = cnki , sd\n")
        assert cfg.webvpn_base == "https://vpn.example.edu.cn"
        assert cfg.default_databases == ["cnki", "sd"]


def test_config_create_then_reload():
    with TempDir() as d:
        p = d / "config.ini"
        cfg = load_config(p, create=True)
        assert p.exists()
        assert "[account]" in p.read_text(encoding="utf-8")
        assert cfg.webvpn_base.endswith("webvpn.hhu.edu.cn")


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
