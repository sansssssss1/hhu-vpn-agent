"""证明「核心可独立运行」——不是口号，是测试。

两层验证：
  1. 静态：核心包 hhuvpn/*.py 在模块级不得 import integrations/；
  2. 动态：把 integrations/ 临时改名藏起来，CLI 仍然照常工作（跑完立刻改回来）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run(args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, str(ROOT / "hhuvpn.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", env=env,
                       cwd=str(ROOT), timeout=120)
    return p.returncode, p.stdout, p.stderr


def test_core_modules_do_not_import_integrations_at_module_level():
    offenders = []
    for path in sorted((ROOT / "hhuvpn").glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")) and "integrations" in s:
                offenders.append(f"{path.name}: {s}")
    assert not offenders, "核心模块在模块级依赖了可选组件: " + "; ".join(offenders)


def test_cli_still_works_without_integrations_dir():
    integrations = ROOT / "integrations"
    hidden = ROOT / "integrations__hidden_for_test"
    if hidden.exists():
        shutil.rmtree(hidden, ignore_errors=True)
    if not integrations.exists():
        raise AssertionError("integrations/ 不存在，无法验证可选性（应当存在但可删除）")
    integrations.rename(hidden)
    try:
        code, out, err = _run(["db", "list", "--json"])
        assert code == 0, err
        assert json.loads(out)["count"] > 20

        code, out, err = _run(["url", "cnki", "--json"])
        assert json.loads(out)["alias"] == "cnki"

        # 只有 mcp 这个可选入口应当给出友好提示而不是崩掉
        code, out, err = _run(["mcp", "--json"])
        payload = json.loads(out) if out.strip().startswith("{") else {}
        assert payload.get("error", {}).get("code") == "integration_missing"
    finally:
        hidden.rename(integrations)


def test_standalone_launchers_exist():
    """独立使用的人用入口必须存在（不依赖任何 agent 组件）。"""
    for name in ("open-databases.cmd", "install-guard.cmd", "uninstall-guard.cmd",
                 "install-guard.ps1", "uninstall-guard.ps1", "README.md"):
        assert (ROOT / "standalone" / name).exists(), f"缺少独立入口文件: {name}"


def test_guard_module_is_core_not_integration():
    from hhuvpn.guard import Guard  # noqa: F401  —— 守护属于核心能力
    assert (ROOT / "hhuvpn" / "guard.py").exists()


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
