"""BIP39 + BIP32 (eth_account.hdaccount), hardened on every level: m/44'/60'/<account>'/0'/<index>'; the fee wallet
is account 9000. No message has key material."""

import hashlib
import hmac

from eth_account import Account
from eth_account.hdaccount import HDPath, Mnemonic
from eth_account.types import Language

from .config import FEE_ACCOUNT, HARDENED_LIMIT

ENTROPY_BYTES = 32
WORD_COUNT = 24


class SeedError(ValueError):
    """The words or the entropy are not a valid 24-word BIP39 seed. The message never has the words."""


def entropy_to_words(entropy: bytes) -> str:
    if len(entropy) != ENTROPY_BYTES:
        raise SeedError("entropy must be 32 bytes")
    return Mnemonic(Language.ENGLISH).to_mnemonic(entropy)


def words_to_entropy(words: str) -> bytes:
    """24 English BIP39 words to the 32 bytes of entropy. Checks the BIP39 checksum."""
    parts = words.lower().split()
    if len(parts) != WORD_COUNT:
        raise SeedError("24 words are necessary")
    position = {word: i for i, word in enumerate(Mnemonic(Language.ENGLISH).wordlist)}
    for number, word in enumerate(parts, start=1):
        if word not in position:
            raise SeedError(f"word {number} is not in the BIP39 English word list")
    bits = int("".join(f"{position[word]:011b}" for word in parts), 2)  # 256 bits of entropy + 8 bits of checksum
    entropy = (bits >> 8).to_bytes(ENTROPY_BYTES, "big")
    if not hmac.compare_digest(bytes([bits & 0xFF]), hashlib.sha256(entropy).digest()[:1]):
        raise SeedError("the checksum of the words is wrong")
    return entropy


def deposit_path(account: int, index: int) -> str:
    if not 0 <= account < HARDENED_LIMIT or not 0 <= index < HARDENED_LIMIT:
        raise ValueError("account and index must be from 0 to 2^31-1")
    return f"m/44'/60'/{account}'/0'/{index}'"


def address_of(key: bytes) -> str:
    return Account.from_key(key).address


class Wallet:
    """One seed in memory. Child keys are derived for one use and not kept."""

    def __init__(self, entropy: bytes) -> None:
        # BIP39 seed with an empty passphrase, so that a standard wallet can import the 24 words.
        self._seed = Mnemonic.to_seed(entropy_to_words(entropy), "")
        self.fee_address = address_of(self.fee_key())

    def __repr__(self) -> str:
        return "Wallet(<hidden>)"

    def deposit_key(self, account: int, index: int) -> bytes:
        if account == FEE_ACCOUNT:
            raise ValueError("the fee wallet account is not a store account")
        return HDPath(deposit_path(account, index)).derive(self._seed)

    def fee_key(self) -> bytes:
        return HDPath(deposit_path(FEE_ACCOUNT, 0)).derive(self._seed)

    def deposit_address(self, account: int, index: int) -> str:
        return address_of(self.deposit_key(account, index))
