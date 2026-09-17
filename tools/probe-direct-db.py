#!/usr/bin/env python3
"""探针：校园网直连（不走 WebVPN）时，数据库是否已经用学校身份认出了我们。

校内很多库是按出口 IP 授权的，能直连就不必绕 WebVPN。
只读探测，不改任何状态。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hhuvpn.http import HttpSession  # noqa: E402

TARGETS = [
    ("cnki", "https://www.cnki.net/"),
    ("cnki-kns", "https://kns.cnki.net/kns8s/"),
    ("sd", "https://www.sciencedirect.com/"),
    ("wos", "https://www.webofscience.com/wos/woscc/basic-search"),
    ("wanfang", "https://www.wanfangdata.com.cn/"),
    ("lib", "https://lib.hhu.edu.cn/"),
]
MARKERS = ["河海大学", "Hohai University", "hohai"]

s = HttpSession(timeout=30, retries=1)
for alias, url in TARGETS:
    try:
        r = s.get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"{alias:<10} 请求失败: {exc!r}"[:160])
        continue
    text = r.text
    title = re.sub(r"\s+", " ", (re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I) or [None, ""])[1]).strip()[:70]
    hits = [m for m in MARKERS if m in text]
    inst = ""
    for m in re.finditer(r"[^<>\n]{0,30}(?:河海大学|Hohai University)[^<>\n]{0,30}", text):
        inst = re.sub(r"\s+", " ", m.group(0)).strip()[:70]
        break
    print(f"{alias:<10} HTTP {r.status} {len(r.body):>7}B  标题={title!r}")
    print(f"{'':<10} 机构标记={hits or '无'}  证据={inst!r}")
    print(f"{'':<10} 按 IP 直连即已认证={bool(hits)}")
    print()
