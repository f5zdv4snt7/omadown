"""AES-CBC decryption for HLS ``#EXT-X-KEY:METHOD=AES-128`` streams.

Prefers the ``cryptography`` package when it is installed, and otherwise falls
back to a self-contained AES implementation so that ``omadown`` has no hard
third-party dependency beyond yt-dlp and ffmpeg.
"""

from __future__ import annotations

from typing import Iterable

try:  # pragma: no cover - exercised only when the optional dep is present
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAVE_CRYPTOGRAPHY = True
except Exception:  # noqa: BLE001
    _HAVE_CRYPTOGRAPHY = False


class DecryptionError(RuntimeError):
    pass


# --------------------------------------------------------------------- pure AES

def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value = (value ^ 0x1B) & 0xFF
    return value & 0xFF


def _gmul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xFF


def _build_sboxes() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        transformed = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = transformed ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inverse = [0] * 256
    for index, value in enumerate(sbox):
        inverse[value] = index
    return sbox, inverse


SBOX, INV_SBOX = _build_sboxes()

_MUL = {
    9: lambda x: _gmul(x, 9),
    11: lambda x: _gmul(x, 11),
    13: lambda x: _gmul(x, 13),
    14: lambda x: _gmul(x, 14),
}


def _expand_key(key: bytes) -> tuple[list[list[int]], int]:
    if len(key) not in (16, 24, 32):
        raise DecryptionError(f"unsupported AES key length: {len(key)} bytes")
    nk = len(key) // 4
    rounds = nk + 6
    words: list[list[int]] = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    round_keys = [
        [byte for r in range(4) for byte in words[rnd * 4 + r]]
        for rnd in range(rounds + 1)
    ]
    return round_keys, rounds


def _add_round_key(state: list[int], key: list[int]) -> None:
    for i in range(16):
        state[i] ^= key[i]


def _inv_shift_rows(state: list[int]) -> None:
    for row in range(1, 4):
        block = [state[col * 4 + row] for col in range(4)]
        block = block[-row:] + block[:-row]
        for col in range(4):
            state[col * 4 + row] = block[col]


def _inv_sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = INV_SBOX[state[i]]


def _inv_mix_columns(state: list[int]) -> None:
    for col in range(4):
        i = col * 4
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _MUL[14](a0) ^ _MUL[11](a1) ^ _MUL[13](a2) ^ _MUL[9](a3)
        state[i + 1] = _MUL[9](a0) ^ _MUL[14](a1) ^ _MUL[11](a2) ^ _MUL[13](a3)
        state[i + 2] = _MUL[13](a0) ^ _MUL[9](a1) ^ _MUL[14](a2) ^ _MUL[11](a3)
        state[i + 3] = _MUL[11](a0) ^ _MUL[13](a1) ^ _MUL[9](a2) ^ _MUL[14](a3)


def _decrypt_block(block: bytes, round_keys: list[list[int]], rounds: int) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[rounds])
    for rnd in range(rounds - 1, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, round_keys[0])
    return bytes(state)


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _aes_cbc_decrypt_pure(data: bytes, key: bytes, iv: bytes) -> bytes:
    round_keys, rounds = _expand_key(key)
    if len(iv) != 16:
        iv = iv.rjust(16, b"\x00")[:16]
    out = bytearray()
    previous = iv
    for offset in range(0, len(data) - 15, 16):
        block = data[offset:offset + 16]
        out += _xor(_decrypt_block(block, round_keys, rounds), previous)
        previous = block
    return bytes(out)


# ------------------------------------------------------------------ public API

def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if not data:
        return b""
    if _HAVE_CRYPTOGRAPHY:
        try:
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            return decryptor.update(data) + decryptor.finalize()
        except Exception as exc:  # noqa: BLE001
            raise DecryptionError(str(exc)) from exc
    return _aes_cbc_decrypt_pure(data, key, iv)


def strip_pkcs7(data: bytes) -> bytes:
    """Remove PKCS#7 padding; tolerant of streams that were never padded."""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_segment(data: bytes, key: bytes, iv: bytes, *, strip_pad: bool = True) -> bytes:
    plain = aes_cbc_decrypt(data, key, iv)
    return strip_pkcs7(plain) if strip_pad else plain


def iv_from_sequence(sequence: int) -> bytes:
    """HLS default IV: the media sequence number as a 128-bit big-endian integer."""
    return sequence.to_bytes(16, "big")


def parse_hex_iv(value: str | None) -> bytes | None:
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return raw.rjust(16, b"\x00")[:16]
