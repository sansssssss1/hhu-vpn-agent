import ssl, sys, re, json, base64, urllib.request, urllib.parse, http.cookiejar, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sympad

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

def aes_cbc_b64(plain: bytes, key: bytes, iv: bytes) -> str:
    p = sympad.PKCS7(128).padder(); data = p.update(plain) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()

def enc_password(pwd: str, salt: str) -> str:
    pre = "".join(CHARS[(i * 7 + 3) % len(CHARS)] for i in range(64))   # deterministic stand-in
    iv = "1234567890123456"
    return aes_cbc_b64((pre + pwd).encode(), salt.encode(), iv.encode())

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
op.addheaders = [("User-Agent", UA)]

def get(url, data=None):
    req = urllib.request.Request(url, data=data.encode() if isinstance(data, str) else data,
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"} if data else {"User-Agent": UA})
    try:
        with op.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), url
    except Exception as e:
        return None, {}, repr(e).encode(), url

s, h, b, final = get("https://webvpn.hhu.edu.cn/")
html = b.decode("utf-8", "replace")
print("STEP1 GET webvpn:", s, final[:160])
if s != 200 or "authserver" not in final:
    print("unexpected:", html[:500]); raise SystemExit()

prefix = final.split("/authserver")[0]
salt = (re.search(r'id="pwdEncryptSalt"[^>]*value="([^"]*)"', html) or [None, ""])[1]
execution = (re.search(r'name="execution"[^>]*value="([^"]*)"', html) or [None, ""])[1]
lt = (re.search(r'name="lt"[^>]*value="([^"]*)"', html) or [None, ""])[1]
capswitch = (re.search(r'captchaSwitch = "([^"]*)"', html) or [None, ""])[1]
print("salt:", salt, "| execution:", execution, "| lt:", repr(lt), "| captchaSwitch:", capswitch, "| prefix:", prefix[:60] + "...")
print("cookies:", [(c.name, c.value[:24]) for c in cj])

s2, h2, b2, _ = get(prefix + "/authserver/checkNeedCaptcha.htl?username=0000000000")
print("STEP2 checkNeedCaptcha:", s2, b2.decode("utf-8", "replace")[:200])

form = urllib.parse.urlencode({
    "username": "0000000000",
    "password": enc_password("NotARealPassword1", salt),
    "_eventId": "submit",
    "cllt": "userNameLogin",
    "dllt": "generalLogin",
    "execution": execution,
    "lt": lt,
    "captcha": "",
    "rmShown": "1",
})
s3, h3, b3, fin3 = get(prefix + "/authserver/login", data=form)
body = b3.decode("utf-8", "replace")
print("STEP3 POST login:", s3)
print("  Location:", h3.get("Location"))
print("  finalURL:", fin3[:160])
errs = re.findall(r'(?:id="showErrorTip"[^>]*>|id="msg"[^>]*>|class="[^"]*error[^"]*"[^>]*>)([^<]{0,120})', body)
print("  inline errors:", [e.strip() for e in errs if e.strip()][:5])
for kw in ["用户名或密码错误", "密码错误", "账号", "验证码", "锁定", "错误", "captcha", "authError", "userNotFound"]:
    if kw in body:
        i = body.find(kw); print(f"  KW {kw}: ...{re.sub(chr(10),' ',body[max(0,i-90):i+90])}...")
print("  body len:", len(body), "| title:", (re.search(r"<title[^>]*>(.*?)</title>", body, re.S) or [None,''])[1].strip()[:60])
print("  cookies now:", [(c.name, c.value[:24]) for c in cj])
