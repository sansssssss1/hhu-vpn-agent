#!/usr/bin/env python3
"""同步/对比上游 sqxart/hhu-autologin：只做只读检查，便于决定要不要升级基座。

用法：
    python tools/upstream-sync.py info            # 看上游最新版本/提交
    python tools/upstream-sync.py fetch <dir>     # 下载最新 tarball 解到 <dir>
    python tools/upstream-sync.py diff <dir>      # 与本地 upstream/ 逐文件对比
"""
from __future__ import annotations

import hashlib
import io
import json
import ssl
import sys
import tarfile
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "sqxart/hhu-autologin"
API = f"https://api.github.com/repos/{REPO}"
TARBALL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{{ref}}"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "hhuvpn-upstream-sync"}
ROOT = Path(__file__).resolve().parent.parent


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _json(url: str):
    return json.loads(_get(url).decode("utf-8", "replace"))


def info() -> int:
    latest = _json(f"{API}/releases/latest") if True else {}
    try:
        latest = _json(f"{API}/releases/latest")
        print("最新 release :", latest.get("tag_name"), "|", latest.get("name"),
              "|", latest.get("published_at"))
    except Exception as exc:  # noqa: BLE001
        print("最新 release : 查询失败", exc)
    try:
        tags = _json(f"{API}/tags?per_page=10")
        print("最近 tags    :", ", ".join(t["name"] for t in tags) or "(无)")
    except Exception as exc:  # noqa: BLE001
        print("最近 tags    : 查询失败", exc)
    commits = _json(f"{API}/commits?per_page=10")
    print("最近提交：")
    for c in commits:
        print("   ", c["sha"][:8], c["commit"]["committer"]["date"],
              (c["commit"]["message"].splitlines() or [""])[0][:90])
    local = ROOT / "upstream" / "CHANGELOG.md"
    if local.exists():
        head = local.read_text(encoding="utf-8", errors="replace").splitlines()[:6]
        print("\n本地 upstream/CHANGELOG.md 头部：")
        for line in head:
            print("   ", line)
    return 0


def fetch(dest: str, ref: str = "main") -> int:
    data = _get(TARBALL.format(ref=ref))
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(out, filter="data")
    sub = next(p for p in out.iterdir() if p.is_dir())
    print("已解到:", sub)
    print("文件数 :", sum(1 for _ in sub.rglob("*") if _.is_file()))
    return 0


def _hashes(base: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            out[str(p.relative_to(base)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def diff(other: str) -> int:
    a = _hashes(ROOT / "upstream")
    b = _hashes(Path(other))
    # 找到解压出来的那一层
    if not (Path(other) / "hhu_login.py").exists():
        cand = [p for p in Path(other).iterdir() if p.is_dir() and (p / "hhu_login.py").exists()]
        if cand:
            b = _hashes(cand[0])
    only_local = sorted(set(a) - set(b))
    only_new = sorted(set(b) - set(a))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    print(f"本地独有 {len(only_local)} | 上游新增 {len(only_new)} | 内容不同 {len(changed)}")
    for label, items in (("本地独有", only_local), ("上游新增", only_new), ("内容不同", changed)):
        if items:
            print(f"[{label}]")
            for i in items:
                print("   ", i)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "info":
        sys.exit(info())
    if cmd == "fetch":
        sys.exit(fetch(sys.argv[2] if len(sys.argv) > 2 else "upstream_new"))
    if cmd == "diff":
        sys.exit(diff(sys.argv[2]))
    print(__doc__)
