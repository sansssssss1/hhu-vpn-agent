"""锁定「所有请求强制直连」——上游 v1.4.1 的阻断级修复，本项目同步移植。

背景（docs/field-notes.md §5）：开了 Clash 等系统代理时，urllib 默认跟随代理，
发往校园门户的内网请求全被拒（ConnectionRefused 10061），表现为
"不在校园网"误报 + 守护整条链路瘫痪；掉线探测还可能被代理"代答"造成假在线。

实现手法与上游一致：build_opener 时显式传一个空映射的 ProxyHandler，
让 build_opener 跳过默认的 ProxyHandler；空映射不生成任何 *_open 钩子，请求即直连。
"""
import ast
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hhuvpn.http import HttpSession, probe_http  # noqa: E402


class _ProxyEnv:
    """临时设置系统代理环境变量，制造"最容易被代理带偏"的场景。"""

    def __enter__(self):
        self._old = {k: os.environ.get(k) for k in ("http_proxy", "https_proxy", "all_proxy")}
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ["https_proxy"] = "http://127.0.0.1:9"
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _proxy_handlers(opener):
    return [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]


def test_default_opener_would_follow_system_proxy():
    """先证明这个测试有鉴别力：默认 opener 确实会挂上系统代理。"""
    with _ProxyEnv():
        assert urllib.request.getproxies().get("http"), "环境变量没生效，测试无效"
        default = urllib.request.build_opener()
        handlers = _proxy_handlers(default)
        assert handlers, "默认 opener 竟然没有 ProxyHandler —— 前提不成立"
        assert any(h.proxies for h in handlers), "默认 opener 的代理映射为空，测试失去意义"


def test_plugin_opener_never_carries_system_proxy():
    with _ProxyEnv():
        opener = HttpSession()._opener(True)
        bad = [h for h in _proxy_handlers(opener) if h.proxies]
        assert not bad, f"会话 opener 跟随了系统代理: {bad[0].proxies if bad else ''}"

        opener_noverify = HttpSession(tls="false")._opener(False)
        bad2 = [h for h in _proxy_handlers(opener_noverify) if h.proxies]
        assert not bad2, "不校验证书的 opener 同样必须直连"


def test_probe_opener_never_carries_system_proxy():
    """掉线探针若走代理，可能被"代答"成 204 → 假在线，必须直连。"""
    import inspect
    src = inspect.getsource(probe_http)
    assert "ProxyHandler({})" in src, "probe_http 的 opener 没有禁用系统代理"


def test_python38_syntax_compatibility():
    """上游 v1.4.2 用 CI 矩阵承诺 Python 3.8+；这里用 ast 的 feature_version 做语法级核对。

    注意：只验证**语法**可被 3.8 解析，不等于在 3.8 上跑过全部测试（本机是 3.14）。
    """
    targets = sorted((ROOT / "hhuvpn").glob("*.py")) + [ROOT / "hhuvpn.py",
                                                        ROOT / "integrations" / "mcp_server.py"]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(path), feature_version=(3, 8))
        except SyntaxError as exc:
            raise AssertionError(f"{path.name} 用了 Python 3.8 不支持的语法: {exc}") from exc


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
