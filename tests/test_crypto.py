import unittest

from omadown import crypto


class AesTests(unittest.TestCase):
    """NIST SP 800-38A F.2.1 CBC-AES128.Decrypt known-answer test."""

    KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    CIPHERTEXT = bytes.fromhex(
        "7649abac8119b246cee98e9b12e9197d"
        "5086cb9b507219ee95db113a917678b2"
    )
    PLAINTEXT = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
    )

    def test_cbc_decrypt_matches_nist_vector(self):
        self.assertEqual(crypto.aes_cbc_decrypt(self.CIPHERTEXT, self.KEY, self.IV),
                         self.PLAINTEXT)

    def test_pure_python_matches_fast_path(self):
        pure = crypto._aes_cbc_decrypt_pure(self.CIPHERTEXT, self.KEY, self.IV)
        self.assertEqual(pure, self.PLAINTEXT)

    def test_iv_from_sequence(self):
        self.assertEqual(crypto.iv_from_sequence(0x0102),
                         bytes.fromhex("00000000000000000000000000000102"))

    def test_parse_hex_iv(self):
        self.assertEqual(crypto.parse_hex_iv("0x0f0e0d0c0b0a09080706050403020100"),
                         bytes.fromhex("0f0e0d0c0b0a09080706050403020100"))
        self.assertIsNone(crypto.parse_hex_iv("not-hex"))

    def test_strip_pkcs7(self):
        self.assertEqual(crypto.strip_pkcs7(b"hello\x03\x03\x03"), b"hello")
        self.assertEqual(crypto.strip_pkcs7(b"hello"), b"hello")


if __name__ == "__main__":
    unittest.main()
