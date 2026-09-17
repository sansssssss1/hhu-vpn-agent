import ssl, sys, urllib.request, http.cookiejar, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
op.addheaders=[("User-Agent","Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")]
url, out = sys.argv[1], sys.argv[2]
try:
    with op.open(url, timeout=20) as r:
        data = r.read()
        print("STATUS", r.status, "BYTES", len(data), "CT", r.headers.get("Content-Type"))
except Exception as e:
    print("ERR", repr(e)); raise SystemExit(1)
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "wb").write(data)
print("SAVED", out)
