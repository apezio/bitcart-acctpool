"""Proof for SPEC 7.6: which ECDSA backend signs, and that it gives the same bytes as the pure-Python backend.

ECDSA with RFC 6979 has no random part: for one key and one message hash there is one correct signature.
Two correct implementations give the same 65 bytes (r, s, recovery id).

This file has no test tool in it. It runs in the signer image:  python - < backend_proof.py
and the unit tests import it (test_backend.py).
"""

import os
import sys

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def active_backend() -> str:
    from eth_keys import keys

    return type(keys.backend).__name__


def _keys() -> list[bytes]:
    edge = [1, 2, 3, N - 1, N - 2, N // 2, N // 2 + 1, 2**255, 2**128, 2**64 - 1]
    return [number.to_bytes(32, "big") for number in edge]


def _digests() -> list[bytes]:
    return [bytes(32), bytes([0xFF]) * 32, bytes(31) + b"\x01", N.to_bytes(32, "big"), (N - 1).to_bytes(32, "big")]


def compare_signatures(random_count: int = 300) -> int:
    """Sign with both backends. Gives the number of (key, hash) pairs that were compared."""
    from eth_keys import KeyAPI
    from eth_keys.backends import CoinCurveECCBackend, NativeECCBackend

    native, curve = KeyAPI(NativeECCBackend()), KeyAPI(CoinCurveECCBackend())
    pairs = [(key, digest) for key in _keys() for digest in _digests()]
    pairs += [(os.urandom(32), os.urandom(32)) for _ in range(random_count)]
    pairs = [(key, digest) for key, digest in pairs if 0 < int.from_bytes(key, "big") < N]
    for key, digest in pairs:
        a = native.ecdsa_sign(digest, native.PrivateKey(key))
        b = curve.ecdsa_sign(digest, curve.PrivateKey(key))
        if a.to_bytes() != b.to_bytes() or len(a.to_bytes()) != 65:
            raise AssertionError("the two backends gave different signatures")
        public_a = native.private_key_to_public_key(native.PrivateKey(key)).to_bytes()
        public_b = curve.private_key_to_public_key(curve.PrivateKey(key)).to_bytes()
        if public_a != public_b:
            raise AssertionError("the two backends gave different public keys")
        if native.ecdsa_recover(digest, b).to_bytes() != public_a or curve.ecdsa_recover(digest, a).to_bytes() != public_a:
            raise AssertionError("the signature of one backend is not accepted by the other")
    return len(pairs)


def compare_transactions(count: int = 60) -> int:
    """Transactions from the code of the signer (the active backend) against the pure-Python backend."""
    from eth_account.typed_transactions import TypedTransaction
    from eth_keys import KeyAPI
    from eth_keys.backends import NativeECCBackend
    from hexbytes import HexBytes

    from acctpool_signer import hd, tx

    native = KeyAPI(NativeECCBackend())
    for number in range(count):
        key = os.urandom(32)
        sender = hd.address_of(key)
        to = hd.address_of(os.urandom(32))
        native_transfer = number % 2 == 0
        fields = {
            "chain_id": (137, 1, 56, 31337)[number % 4],
            "nonce": number,
            "to": to,
            "value": int.from_bytes(os.urandom(9), "big") if native_transfer else 0,
            "data": b"" if native_transfer else tx.transfer_data(sender, 1 + int.from_bytes(os.urandom(12), "big")),
            "gas": 21000 if native_transfer else 70000,
            "max_fee_per_gas": 10**9 * (number + 1),
            "max_priority_fee_per_gas": 10**9,
        }
        raw_tx, _ = tx.sign(key, sender, *fields.values())
        decoded = TypedTransaction.from_bytes(HexBytes(raw_tx)).as_dict()
        unsigned = TypedTransaction.from_dict(
            {
                "type": 2,
                "chainId": fields["chain_id"],
                "nonce": fields["nonce"],
                "to": to,
                "value": fields["value"],
                "data": fields["data"],
                "gas": fields["gas"],
                "maxFeePerGas": fields["max_fee_per_gas"],
                "maxPriorityFeePerGas": fields["max_priority_fee_per_gas"],
                "accessList": [],
            }
        )
        expected = native.ecdsa_sign(unsigned.hash(), native.PrivateKey(key))
        if (decoded["r"], decoded["s"], decoded["v"]) != (expected.r, expected.s, expected.v):
            raise AssertionError("the transaction signature is not the one of the pure-Python backend")
    return count


def main() -> int:
    backend = active_backend()
    print(f"ok   eth_keys signs with {backend}")
    if backend != "CoinCurveECCBackend":
        print("FAIL the active backend is not coincurve")
        return 1
    import importlib.metadata

    print(f"ok   coincurve {importlib.metadata.version('coincurve')} is installed")
    print(
        f"ok   {compare_signatures()} signatures: coincurve and pure Python give the same 65 bytes, and the same public keys"
    )
    print(f"ok   {compare_transactions()} transactions of the signer code: same r, s, v as from pure Python")
    return 0


if __name__ == "__main__":
    sys.exit(main())
