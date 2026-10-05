"""Independent calculations for the tests.

Nothing here imports acctpool_signer, eth_account or eth_keys: BIP32, secp256k1, the address encoding,
the transaction decoding and the signer recovery are written again, from the standards, with hashlib/hmac,
Keccak from pycryptodome and the rlp package. A fault in the signer's libraries cannot hide behind itself.
"""

import hashlib
import hmac
from typing import Any

import rlp
from Crypto.Hash import keccak

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
HARD = 0x80000000
Point = tuple[int, int] | None


def ec_add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        slope = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (slope * slope - a[0] - b[0]) % P
    return x, (slope * (a[0] - x) - a[1]) % P


def ec_mul(k: int, point: Point) -> Point:
    result: Point = None
    while k:
        if k & 1:
            result = ec_add(result, point)
        point = ec_add(point, point)
        k >>= 1
    return result


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def checksum_address(raw: bytes) -> str:
    assert len(raw) == 20
    text = raw.hex()
    digest = keccak256(text.encode()).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(text))


def address_from_point(point: Point) -> str:
    assert point is not None
    return checksum_address(keccak256(point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big"))[12:])


def address_from_key(key: bytes) -> str:
    return address_from_point(ec_mul(int.from_bytes(key, "big"), G))


def bip39_seed(words: str, passphrase: str = "") -> bytes:
    return hashlib.pbkdf2_hmac("sha512", words.encode(), b"mnemonic" + passphrase.encode(), 2048)


def bip32_derive(seed: bytes, path: list[int]) -> bytes:
    """BIP32 private derivation. path has the raw child numbers (hardened = number + 2^31)."""
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for child in path:
        if child >= HARD:
            data = b"\x00" + key + child.to_bytes(4, "big")
        else:
            point = ec_mul(int.from_bytes(key, "big"), G)
            assert point is not None
            data = bytes([2 + (point[1] & 1)]) + point[0].to_bytes(32, "big") + child.to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        left = int.from_bytes(digest[:32], "big")
        assert left < N
        key = ((left + int.from_bytes(key, "big")) % N).to_bytes(32, "big")
        chain = digest[32:]
    return key


def deposit_path(account: int, index: int, coin: int = 60) -> list[int]:
    return [44 + HARD, coin + HARD, account + HARD, 0 + HARD, index + HARD]


FEE_PATH = deposit_path(9000, 0)


def recover_address(digest: bytes, r: int, s: int, y_parity: int) -> str:
    """ECDSA public key recovery (SEC 1, 4.1.6), then the address."""
    assert 0 < r < N and 0 < s < N and y_parity in (0, 1)
    y = pow((pow(r, 3, P) + 7) % P, (P + 1) // 4, P)
    assert (y * y - pow(r, 3, P) - 7) % P == 0
    if y & 1 != y_parity:
        y = P - y
    z = int.from_bytes(digest, "big")
    r_inv = pow(r, -1, N)
    point = ec_add(ec_mul(s * r_inv % N, (r, y)), ec_mul(-z * r_inv % N, G))
    return address_from_point(point)


def _int(data: bytes) -> int:
    assert not data.startswith(b"\x00"), "RLP integers have no leading zero"
    return int.from_bytes(data, "big")


def decode_tx(raw_hex: str) -> dict[str, Any]:
    """Decode a signed EIP-1559 transaction and recover its signer."""
    assert raw_hex.startswith("0x")
    raw = bytes.fromhex(raw_hex[2:])
    assert raw[0] == 2, "not a type 2 transaction"
    items = rlp.decode(raw[1:])
    assert len(items) == 12
    chain_id, nonce, priority, max_fee, gas, to, value, data, access_list, y_parity, r, s = items
    assert len(to) == 20
    signing_hash = keccak256(b"\x02" + rlp.encode(items[:9]))
    assert _int(s) <= N // 2, "high-s signature"
    return {
        "type": 2,
        "chain_id": _int(chain_id),
        "nonce": _int(nonce),
        "max_priority_fee_per_gas": _int(priority),
        "max_fee_per_gas": _int(max_fee),
        "gas": _int(gas),
        "to": checksum_address(to),
        "value": _int(value),
        "data": bytes(data),
        "access_list": list(access_list),
        "signer": recover_address(signing_hash, _int(r), _int(s), _int(y_parity)),
        "hash": "0x" + keccak256(raw).hex(),
    }


def decode_transfer(data: bytes) -> tuple[str, int]:
    """transfer(address,uint256) call data to (recipient, amount)."""
    assert len(data) == 68
    assert data[:4] == keccak256(b"transfer(address,uint256)")[:4]
    assert data[4:16] == bytes(12)
    return checksum_address(data[16:36]), int.from_bytes(data[36:], "big")
