"""CLI 契约与 MCP 协议测试（离线；CLI 部分不发起网络请求）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hhuvpn.cli import main  # noqa: E402


_ISOLATED = Path(ROOT) / "tests" / ".tmp" / "cli-case"


def _run_cli_isolated(argv):
    """用临时配置与状态目录跑 CLI —— **所有** CLI 测试都走这里。

    为什么必须隔离（真机上被这条坑过一次）：
    未隔离时 CLI 测试会读到本机的真实 config.ini / DPAPI 凭据 / 环境变量，
    于是 ensure 会**真的去登录 WebVPN**——测试跑一遍就把使用者刚关掉的 VPN
    重新打开。这既让断言随本机状态漂移，也违背「按需开启」的契约。
    隔离后：读不到真实凭据，会话与状态都不落进真实 state 目录。
    """
    _ISOLATED.mkdir(parents=True, exist_ok=True)
    for key in ("HHU_USERNAME", "HHU_PASSWORD", "HHUVPN_USERNAME", "HHUVPN_PASSWORD",
                "HHUVPN_CONFIG"):
        os.environ.pop(key, None)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    args = [*argv, "--config", str(_ISOLATED / "config.ini"),
            "--state-dir", str(_ISOLATED / "state")]
    p = subprocess.run([sys.executable, os.path.join(ROOT, "hhuvpn.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT,
                       timeout=120)
    return p.returncode, p.stdout, p.stderr


# 所有 CLI 测试统一走隔离版本（见上面 _run_cli_isolated 的说明）
_run_cli_capture = _run_cli_isolated


def test_cli_json_envelope():
    code, out, err = _run_cli_capture(["--json", "config", "path"])
    assert code == 0, err
    payload = json.loads(out)
    assert payload["schema"] == 1
    assert payload["command"] == "config-path"
    assert payload["ok"] is True
    assert payload["path"].endswith("config.ini")


def test_cli_db_list():
    code, out, _ = _run_cli_capture(["--json", "db", "list"])
    payload = json.loads(out)
    assert payload["count"] > 20
    aliases = [d["alias"] for d in payload["databases"]]
    assert "sd" in aliases and "cnki" in aliases


def test_cli_json_flag_works_after_subcommand():
    """回归：README/SKILL 里写的是「子命令 ... --json」，两种位置都必须能用。"""
    code, out, err = _run_cli_capture(["db", "list", "--json"])
    assert code == 0, err
    assert json.loads(out)["count"] > 20
    code, out, err = _run_cli_capture(["url", "cnki", "--json"])
    assert json.loads(out)["alias"] == "cnki"


def test_cli_ensure_json_contract():
    """ensure 的 JSON 契约必须稳定（不断言网络结果：校内直连/WebVPN 成败因环境而异）。

    契约要点：
      - 永远是一个可解析的 JSON 对象；ok 为假时必须有 error.code 且退出码非 0；
      - databases[*] 必须带 channel（direct / webvpn），说明判定走的哪条通道；
      - 有库确认可用（含校内 IP 直连）时 ok 为真——不需要账号也算成功。
    """
    code, out, err = _run_cli_isolated(["ensure", "--db", "cnki", "--json"])
    payload = json.loads(out)
    assert payload["command"] == "ensure"
    assert isinstance(payload["ok"], bool)
    if not payload["ok"]:
        assert code != 0 and payload["error"]["code"]
    for entry in payload.get("databases", []):
        assert entry.get("channel") in ("direct", "webvpn"), entry.get("channel")
    assert [s.get("name") for s in payload["steps"]] == ["campus", "webvpn"]


def test_cli_url_build_and_unwrap():
    code, out, _ = _run_cli_capture(["--json", "url", "sd"])
    payload = json.loads(out)
    assert payload["vpn_url"].startswith("https://webvpn.hhu.edu.cn/https/")
    code, out2, _ = _run_cli_capture(["--json", "url", "unwrap", payload["vpn_url"]])
    assert json.loads(out2)["target"] == "https://www.sciencedirect.com/"


def test_cli_unknown_command_is_usage_error():
    code, out, err = _run_cli_capture(["--json", "nope"])
    assert code == 2


def test_cli_bad_db_returns_json_error_not_traceback():
    code, out, err = _run_cli_capture(["--json", "ensure", "--db", "no-such-db-xyz"])
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no-such-db-xyz" in json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- MCP

def _mcp_exchange(messages):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "hhuvpn.py"), "mcp"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, cwd=ROOT)
    stdin = "\n".join(json.dumps(m) for m in messages) + "\n"
    try:
        out, err = proc.communicate(stdin, timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return [json.loads(line) for line in out.splitlines() if line.strip()], err


def test_mcp_initialize_and_list_tools():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    responses, err = _mcp_exchange(msgs)
    by_id = {r.get("id"): r for r in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "hhuvpn"
    tools = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert "hhu_ensure" in tools and "hhu_db_url" in tools


def test_mcp_tool_call_returns_content():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "hhu_db_url", "arguments": {"database": "cnki"}}},
    ]
    responses, _ = _mcp_exchange(msgs)
    by_id = {r.get("id"): r for r in responses}
    text = by_id[2]["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["alias"] == "cnki"
    assert "/https/" in payload["vpn_url"]


def test_mcp_unknown_tool_is_error_content():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
    ]
    responses, _ = _mcp_exchange(msgs)
    assert responses[0]["result"]["isError"] is True


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
