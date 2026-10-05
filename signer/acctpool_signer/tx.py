"""The two EIP-1559 (type 2) shapes: a native transfer without data, and transfer(address,uint256) with value 0."""

from eth_account import Account

FUND_GAS = 21000
TRANSFER_SELECTOR = bytes.fromhex("a9059cbb")  # transfer(address,uint256)
UINT256_LIMIT = 2**256


def transfer_data(destination: str, amount: int) -> bytes:
    if not 0 < amount < UINT256_LIMIT:
        raise ValueError("amount out of range")
    return TRANSFER_SELECTOR + bytes(12) + bytes.fromhex(destination[2:]) + amount.to_bytes(32, "big")


def sign(key: bytes, sender: str, chain_id: int, nonce: int, to: str, value: int, data: bytes, gas: int, max_fee: int,
         priority_fee: int) -> tuple[str, str]:  # fmt: skip
    fields = {"type": 2, "chainId": chain_id, "nonce": nonce, "to": to, "value": value, "data": data, "gas": gas}
    fields.update(maxFeePerGas=max_fee, maxPriorityFeePerGas=priority_fee, accessList=[])
    signed = Account.sign_transaction(fields, key)
    raw = bytes(signed.raw_transaction)
    # Self-check before the bytes leave the process: the signature must be from the expected address.
    if Account.recover_transaction(raw) != sender:
        raise RuntimeError("signature self-check failed")
    return "0x" + raw.hex(), "0x" + bytes(signed.hash).hex()  # raw_tx, tx_hash
