"""跑全部离线测试：python tests/run_all.py

说明：这些测试都不登录、不改系统状态；需要真实账号的端到端验证见 docs/verification.md。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_aes.py", "test_webvpn.py", "test_config.py", "test_verify.py",
          "test_http_direct.py", "test_shared_account.py", "test_webvpn_on_demand.py",
          "test_cli_mcp.py", "test_independence.py"]


def main() -> int:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    failed = []
    for suite in SUITES:
        path = os.path.join(HERE, suite)
        print(f"\n===== {suite} =====", flush=True)
        p = subprocess.run([sys.executable, path], env=env, cwd=os.path.dirname(HERE))
        if p.returncode != 0:
            failed.append(suite)
    print("\n" + "=" * 40)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("ALL SUITES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
