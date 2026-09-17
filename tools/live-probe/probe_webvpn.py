import ssl, sys, json, urllib.request, urllib.parse, http.cookiejar, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")]

url = sys.argv[1] if len(sys.argv) > 1 else "https://webvpn.hhu.edu.cn/"
try:
    with op.open(url, timeout=15) as r:
        body = r.read(); print("STATUS", r.status, r.geturl())
        for k, v in r.headers.items():
            if k.lower() in ("server", "content-type", "set-cookie", "location"):
                print("HDR", k, ":", v[:400])
        txt = body.decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, e.headers.get("Location")); txt = e.read().decode("utf-8","replace")
except Exception as e:
    print("ERR", repr(e)); raise SystemExit(0)

print("LEN", len(txt))
print("TITLE:", (re.search(r"<title[^>]*>(.*?)</title>", txt, re.S) or [None,""])[1].strip())
print("=== forms ===")
for f in re.findall(r"<form[\s\S]*?</form>", txt, re.I)[:3]:
    print(re.sub(r"\s+", " ", f)[:1500])
print("=== inputs ===")
for m in re.findall(r"<input[^>]*>", txt, re.I)[:30]:
    print("  ", re.sub(r"\s+"," ",m)[:200])
print("=== salt / encrypt hints ===")
for pat in [r"pwdDefaultEncryptSalt\s*=\s*['\"]([^'\"]+)", r"pwdEncryptSalt[^,;]{0,80}", r"encrypt[^,;{]{0,60}", r"loginUrl[^,;]{0,80}"]:
    for m in re.findall(pat, txt, re.I)[:5]:
        print("  HIT:", str(m)[:120])
print("=== scripts ===")
for m in sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', txt, re.I))):
    print("  ", m[:200])
print("=== cookies ===")
for c in cj:
    print("  ", c.name, "=", (c.value or "")[:80], "| domain", c.domain, "| path", c.path)
