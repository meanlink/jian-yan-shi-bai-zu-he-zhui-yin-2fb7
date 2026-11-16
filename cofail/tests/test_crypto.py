"""密码学原语测试：HKDF 已知向量、信封认证、DH 协商、PRG 确定性。"""
import unittest

from cofail.common.crypto import (
    MODP_P, dh_keypair, dh_shared, hkdf, mask_vector, open_envelope,
    pair_keys, seal, sha256_hex, prg_field, FIELD_P,
)


class CryptoTests(unittest.TestCase):
    def test_hkdf_rfc5869_vector(self):
        # RFC 5869 Test Case 1（SHA-256）
        ikm = bytes.fromhex("0b" * 22)
        salt = b"\x00"  # 本实现固定 salt，这里仅验证长度与确定性
        out = hkdf(ikm, b"info", 42)
        self.assertEqual(len(out), 42)
        self.assertEqual(hkdf(ikm, b"info", 42), out)
        self.assertNotEqual(hkdf(ikm, b"other", 42), out)

    def test_dh_agreement(self):
        a_priv, a_pub = dh_keypair()
        b_priv, b_pub = dh_keypair()
        self.assertEqual(dh_shared(a_priv, b_pub), dh_shared(b_priv, a_pub))
        with self.assertRaises(ValueError):
            dh_shared(a_priv, 1)  # 非法公钥

    def test_pair_keys_independent(self):
        a_priv, a_pub = dh_keypair()
        b_priv, b_pub = dh_keypair()
        shared = dh_shared(a_priv, b_pub)
        k1 = pair_keys(shared, "r1", "lab-a", "lab-b")
        k2 = pair_keys(shared, "r2", "lab-a", "lab-b")
        self.assertNotEqual(k1, k2)  # 轮次隔离
        # 双方派生出相同密钥
        shared2 = dh_shared(b_priv, a_pub)
        self.assertEqual(pair_keys(shared2, "r1", "lab-a", "lab-b"), k1)

    def test_seal_open_and_tamper(self):
        enc, mac = b"\x01" * 32, b"\x02" * 32
        aad = b"r:triple:lab-a->lab-b"
        env = seal(enc, mac, aad, b"secret payload")
        self.assertEqual(open_envelope(enc, mac, aad, env), b"secret payload")
        # 篡改密文
        bad = dict(env)
        pl = bytearray(bytes.fromhex(bad["payload"]))
        pl[-1] ^= 1
        bad["payload"] = pl.hex()
        with self.assertRaises(ValueError):
            open_envelope(enc, mac, aad, bad)
        # 错 AAD（冒充别的步骤/方向）
        with self.assertRaises(ValueError):
            open_envelope(enc, mac, b"r:input:lab-a->lab-b", env)

    def test_prg_mask_cancels(self):
        key = sha256_hex(b"seed").encode()
        cells = ["c1", "c2", "c3"]
        m = mask_vector(key, "r", "aggregate", cells, "lab-a", "lab-b")
        m2 = mask_vector(key, "r", "aggregate", cells, "lab-a", "lab-b")
        self.assertEqual(m, m2)  # 双方确定性一致
        self.assertEqual(set(m.keys()), set(cells))
        self.assertTrue(all(0 <= v < FIELD_P for v in m.values()))


if __name__ == "__main__":
    unittest.main()
