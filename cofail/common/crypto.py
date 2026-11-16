"""密码学原语（仅依赖 Python 标准库）。

- MODP-2048（RFC 3526 14 组）DH 密钥协商
- HKDF-SHA256 派生成对密钥
- Encrypt-then-MAC 密封信封（HMAC-SHA256 流加密 + HMAC 认证）
- 素数域 p = 2^61-1 上的伪随机生成器（PRG）与运算
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# RFC 3526, 2048-bit MODP Group (group 14)
MODP_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
MODP_G = 2

# Mersenne prime 2^61 - 1，秘密分享所用域
FIELD_P = (1 << 61) - 1


# ---------- 有限域 ----------
def fadd(a: int, b: int) -> int:
    r = a + b
    return r if r < FIELD_P else r - FIELD_P


def fsub(a: int, b: int) -> int:
    r = a - b
    return r if r >= 0 else r + FIELD_P


def fmul(a: int, b: int) -> int:
    return (a * b) % FIELD_P


def frand() -> int:
    return secrets.randbelow(FIELD_P)


# ---------- DH ----------
def dh_keypair() -> tuple[int, int]:
    """返回 (私钥, 公钥)。"""
    priv = secrets.randbits(320) or 1
    pub = pow(MODP_G, priv, MODP_P)
    return priv, pub


def dh_shared(priv: int, peer_pub: int) -> bytes:
    if not (2 <= peer_pub <= MODP_P - 2):
        raise ValueError("非法 DH 公钥")
    z = pow(peer_pub, priv, MODP_P)
    return z.to_bytes(256, "big")


def hkdf(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256（手写实现，兼容旧版 Python）。"""
    salt = b"cofail-hkdf-v1"
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def pair_keys(shared: bytes, round_id: str, a: str, b: str) -> tuple[bytes, bytes]:
    """由 DH 共享秘密派生某一轮某一对参与端的加密密钥 / MAC 密钥。"""
    lo, hi = sorted((a, b))
    info = f"{round_id}:{lo}:{hi}".encode()
    enc = hkdf(shared, info + b":enc")
    mac = hkdf(shared, info + b":mac")
    return enc, mac


# ---------- 密封信封（加密 + 认证，协调端只能转发） ----------
def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])


def seal(enc_key: bytes, mac_key: bytes, aad: bytes, plaintext: bytes) -> dict:
    nonce = secrets.token_bytes(16)
    stream = _keystream(enc_key, nonce, len(plaintext))
    ct = bytes(x ^ y for x, y in zip(plaintext, stream))
    payload = nonce + ct
    tag = hmac.new(mac_key, aad + payload, hashlib.sha256).digest()
    return {"payload": payload.hex(), "tag": tag.hex()}


def open_envelope(enc_key: bytes, mac_key: bytes, aad: bytes, env: dict) -> bytes:
    payload = bytes.fromhex(env["payload"])
    tag = bytes.fromhex(env["tag"])
    expect = hmac.new(mac_key, aad + payload, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise ValueError("信封认证失败")
    nonce, ct = payload[:16], payload[16:]
    stream = _keystream(enc_key, nonce, len(ct))
    return bytes(x ^ y for x, y in zip(ct, stream))


# ---------- 域上 PRG（成对掩码 / 随机分享） ----------
def prg_field(key: bytes, domain: bytes, n: int) -> list[int]:
    """确定性生成 n 个域元素，用于成对掩码，同密钥双方生成相同序列。"""
    out: list[int] = []
    counter = 0
    while len(out) < n:
        h = hmac.new(key, domain + counter.to_bytes(4, "big"),
                     hashlib.sha256).digest()
        out.append(int.from_bytes(h, "big") % FIELD_P)
        counter += 1
    return out


def mask_vector(key: bytes, round_id: str, step: str,
                cells: list[str], lo: str, hi: str) -> list[int]:
    """成对掩码 m_{lo,hi}[cell]；约定 lo 加、hi 减。"""
    domain = f"{round_id}:{step}:{lo}<{hi}:".encode()
    vals = prg_field(key, domain, len(cells))
    return dict(zip(cells, vals))


# ---------- 哈希（样本对齐校验、简报追溯） ----------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
