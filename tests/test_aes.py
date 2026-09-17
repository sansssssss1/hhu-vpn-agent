"""AES / WebVPN 签名的已知答案测试。

运行：python tests/test_aes.py   （或 pytest）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hhuvpn import aes  # noqa: E402


def test_fips197_block():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = aes.AES128(key).encrypt_block(pt)
    assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()


def test_nist_cfb128():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    ct = aes.cfb128_encrypt(key, iv, pt)
    assert ct.hex() == "3b3fd92eb72dad20333449f8e83cfb4a", ct.hex()
    assert aes.cfb128_decrypt(key, iv, ct) == pt


def test_nist_cbc():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    ct = aes.cbc_encrypt(key, iv, pt, pad=False)
    assert ct.hex() == "7649abac8119b246cee98e9b12e9197d", ct.hex()


def test_webvpn_host_signature_live_vector():
    """真实抓取自 https://webvpn.hhu.edu.cn/ 的 CAS 跳转地址。"""
    from hhuvpn.webvpn import sign_host
    assert sign_host("authserver.hhu.edu.cn") == (
        "77726476706e69737468656265737421f1e2559434357a467b1ac7a490406d301894467e2b"
    )


def test_cas_password_shape():
    """密文长度 = ceil((64 + len(pwd)) / 16) * 16，且长度符合服务端预期。"""
    import base64
    out = aes.encrypt_cas_password("MyPassw0rd!", "676kS0VeaLq30Rlg", prefix="A" * 64)
    raw = base64.b64decode(out)
    assert len(raw) == 80, len(raw)
    # 固定 IV 与固定前缀时结果稳定（便于回归）
    assert aes.encrypt_cas_password("x", "676kS0VeaLq30Rlg", iv="1234567890123456", prefix="B" * 64) == \
        aes.encrypt_cas_password("x", "676kS0VeaLq30Rlg", iv="1234567890123456", prefix="B" * 64)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
