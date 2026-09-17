"""纯标准库实现的 AES-128（仅加密方向 + CBC / CFB128）。

为什么不用 pycryptodome / cryptography：
  1) 沿用上游 hhu-autologin「纯 Python 标准库、零依赖」的取舍，用户拿到就能跑；
  2) 这里只需要两个用途，代码量可控，且用 NIST/FIPS 官方测试向量锁死正确性：
     - CAS 统一身份认证的密码字段：AES-128-CBC + PKCS7 + Base64（见 webvpn.py）
     - 网瑞达 WebVPN 的网址签名：AES-128-CFB（见 sign_host）
如果本机恰好装了 cryptography，调用方可以选择走它（见 verify.py 的可选加速），
但本模块永远是可用的兜底实现。

测试向量来源：FIPS-197、NIST SP 800-38A。
"""

from __future__ import annotations

import base64

# ---------------------------------------------------------------- 基础表

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法，用于 MixColumns。"""
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r


class AES128:
    """AES-128 分组加密器（仅加密方向）。"""

    block_size = 16
    key_size = 16

    def __init__(self, key: bytes):
        if len(key) != 16:
            raise ValueError(f"AES-128 需要 16 字节密钥，收到 {len(key)} 字节")
        self._round_keys = self._expand_key(key)

    @staticmethod
    def _expand_key(key: bytes):
        # 44 个 32bit 轮密钥字
        w = [int.from_bytes(key[i * 4:i * 4 + 4], "big") for i in range(4)]
        for i in range(4, 44):
            t = w[i - 1]
            if i % 4 == 0:
                t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF          # RotWord
                t = ((_SBOX[(t >> 24) & 0xFF] << 24) |            # SubWord
                     (_SBOX[(t >> 16) & 0xFF] << 16) |
                     (_SBOX[(t >> 8) & 0xFF] << 8) |
                     _SBOX[t & 0xFF])
                t ^= _RCON[i // 4] << 24                          # Rcon
            w.append(w[i - 4] ^ t)
        return w

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("分组必须是 16 字节")
        s = list(block)                       # state[row + 4*col]
        rk = self._round_keys

        def add_round_key(rnd: int) -> None:
            for c in range(4):
                word = rk[rnd * 4 + c]
                for r in range(4):
                    s[r + 4 * c] ^= (word >> (24 - 8 * r)) & 0xFF

        add_round_key(0)
        for rnd in range(1, 11):
            # SubBytes + ShiftRows（列内位移，等价于行位移的转置写法）
            t = [0] * 16
            for r in range(4):
                for c in range(4):
                    t[r + 4 * c] = _SBOX[s[r + 4 * ((c + r) % 4)]]
            s = t
            if rnd != 10:
                # MixColumns
                for c in range(4):
                    a = s[4 * c:4 * c + 4]
                    s[4 * c + 0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
                    s[4 * c + 1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
                    s[4 * c + 2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
                    s[4 * c + 3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
            add_round_key(rnd)
        return bytes(s)


# ---------------------------------------------------------------- 模式

def cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes, pad: bool = True) -> bytes:
    """AES-128-CBC + PKCS7（pad=False 时要求长度已是 16 的倍数）。"""
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    data = plaintext
    if pad:
        n = 16 - (len(data) % 16)
        data = data + bytes([n]) * n
    elif len(data) % 16:
        raise ValueError("pad=False 时明文长度必须是 16 的倍数")
    aes = AES128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = aes.encrypt_block(block)
        out += prev
    return bytes(out)


def cfb128_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CFB128（支持末段不足 16 字节，与 CryptoJS/OpenSSL 行为一致）。"""
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    aes = AES128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(plaintext), 16):
        chunk = plaintext[i:i + 16]
        ks = aes.encrypt_block(prev)
        ct = bytes(a ^ b for a, b in zip(chunk, ks))
        out += ct
        prev = ct + ks[len(ct):] if len(ct) < 16 else ct
    return bytes(out)


def cfb128_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """CFB128 解密（WebVPN 地址反解用）。"""
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    aes = AES128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), 16):
        chunk = ciphertext[i:i + 16]
        ks = aes.encrypt_block(prev)
        out += bytes(a ^ b for a, b in zip(chunk, ks))
        prev = chunk + ks[len(chunk):] if len(chunk) < 16 else chunk
    return bytes(out)


# ---------------------------------------------------------------- CAS 密码字段

# 与页面 JS 保持一致的随机字符表（$aes_chars），用于填充前缀与 IV
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

# 经典 wisedu 前端固定 IV；服务端只取密文尾部（密码所在分组）即可解出，
# 因此 IV 的具体取值不影响后端识别，这里仍然给出一个确定值以免行为漂移。
DEFAULT_IV = "1234567890123456"


def random_string(n: int, randbytes=None) -> str:
    if randbytes is None:
        import os
        randbytes = os.urandom
    raw = randbytes(n)
    return "".join(AES_CHARS[b % len(AES_CHARS)] for b in raw)


def encrypt_cas_password(password: str, salt: str, iv: str | None = None,
                         prefix: str | None = None) -> str:
    """复刻登录页 encryptPassword(password, pwdEncryptSalt)。

    JS 原文：
        getAesString(randomString(64) + password, key=salt, iv=randomString(16))
        → AES-CBC/PKCS7 → Base64

    明文尾部才是真正的密码，前 64 字节随机前缀的作用是让服务端即使不掌握 IV
    （IV 随机且不随请求上传）也能从末段分组还原出密码。
    """
    if len(salt) != 16:
        # 服务端下发的 salt 固定 16 字节；短了补齐、长了截断，避免直接崩
        salt = (salt + "0" * 16)[:16]
    iv = iv or DEFAULT_IV
    prefix = random_string(64) if prefix is None else prefix
    data = (prefix + password).encode("utf-8")
    return base64.b64encode(cbc_encrypt(salt.encode("utf-8"), iv.encode("utf-8"), data)).decode("ascii")


__all__ = [
    "AES128", "cbc_encrypt", "cfb128_encrypt", "cfb128_decrypt",
    "encrypt_cas_password", "random_string", "AES_CHARS", "DEFAULT_IV",
]
