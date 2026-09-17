"""临时脚本：用本机已保存的 GitHub 凭据创建仓库（token 只在进程内使用，绝不打印）。"""
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NAME = "hhu-vpn-agent"
DESC = "河海大学校园网自动登录 + 学校 WebVPN 文献数据库接入（ScienceDirect/知网等）；核心可独立运行，agent 联动为可选"
PRIVATE = False

# 从凭据管理器取（GCM），不经过命令行、不落盘
env = {**os.environ, "GCM_INTERACTIVE": "never", "GIT_TERMINAL_PROMPT": "0"}
p = subprocess.run(["git", "-c", "credential.interactive=false", "credential", "fill"],
                   input="protocol=https\nhost=github.com\n\n",
                   capture_output=True, text=True, timeout=90, env=env)
creds = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
token = creds.get("password", "")
if not token:
    sys.exit("取不到 GitHub 凭据，无法创建仓库")

ctx = ssl.create_default_context()
body = json.dumps({"name": NAME, "description": DESC, "private": PRIVATE,
                   "has_issues": True, "has_wiki": False, "auto_init": False}).encode()
req = urllib.request.Request(
    "https://api.github.com/user/repos", data=body, method="POST",
    headers={"Authorization": "token " + token,
             "Accept": "application/vnd.github+json",
             "User-Agent": "hhuvpn-repo-setup",
             "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        data = json.loads(r.read().decode("utf-8"))
    print("创建成功")
    print("  full_name :", data["full_name"])
    print("  private   :", data["private"])
    print("  html_url  :", data["html_url"])
    print("  clone_url :", data["clone_url"])
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")[:400]
    print("创建失败 HTTP", exc.code)
    print(" ", detail)
    if exc.code == 422:
        print("  （可能同名仓库已存在）")
except Exception as exc:  # noqa: BLE001
    print("创建失败:", repr(exc))
