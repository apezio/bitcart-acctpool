"""Derivation: published vectors first, then the hardened paths against an independent calculation."""

import os
import re

import helpers
import pytest
from eth_account.hdaccount import HDPath, Mnemonic
from mnemonic import Mnemonic as ReferenceMnemonic

from acctpool_signer import hd

# Public test values (the default accounts of hardhat and anvil). Not secrets.
PUBLIC_WORDS = "test test test test test test test test test test test junk"
PUBLIC_ACCOUNTS = [
    (
        "m/44'/60'/0'/0/0",
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    ),
    (
        "m/44'/60'/0'/0/1",
        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    ),
]

# BIP32 test vector 1 (bips/bip-0032.mediawiki), private keys
BIP32_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
BIP32_VECTOR_1 = [
    ("m", [], "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"),
    ("m/0'", [helpers.HARD], "edb2e14f9ee77d26dd93b4ecede8d16ed408ce149b6cd80b0715a2d911a0afea"),
    ("m/0'/1", [helpers.HARD, 1], "3c6cb8d0f6a264c91ea8b5030fadaa8e538b020f0a387421a12de9319dc93368"),
    ("m/0'/1/2'", [helpers.HARD, 1, 2 + helpers.HARD], "cbce0d719ecf7431d88e6a89fa1483e02e35092af60c042b1df2ff59fa424dca"),
    (
        "m/0'/1/2'/2",
        [helpers.HARD, 1, 2 + helpers.HARD, 2],
        "0f479245fb19a38a1954c5c7c0ebab2f9bdfd96a17563ef28a6a4b1a2a764ef4",
    ),
    (
        "m/0'/1/2'/2/1000000000",
        [helpers.HARD, 1, 2 + helpers.HARD, 2, 1000000000],
        "471b76e389e528d6de6d816857e012c5455051cad6660850e58372a6c3e6e7c8",
    ),
]

# BIP39 test vectors (trezor/python-mnemonic vectors.json), 256 bits of entropy
BIP39_VECTORS = [
    ("00" * 32, " ".join(["abandon"] * 23 + ["art"])),
    (
        "7f" * 32,
        " ".join(["legal", "winner", "thank", "year", "wave", "sausage", "worth", "useful"] * 3)[: -len("useful")] + "title",
    ),
    (
        "80" * 32,
        " ".join(["letter", "advice", "cage", "absurd", "amount", "doctor", "acoustic", "avoid"] * 3)[: -len("avoid")]
        + "bless",
    ),
    ("ff" * 32, " ".join(["zoo"] * 23 + ["vote"])),
]


@pytest.mark.parametrize(("path", "address", "key"), PUBLIC_ACCOUNTS)
def test_published_mnemonic_vector(path, address, key):
    seed = Mnemonic.to_seed(PUBLIC_WORDS, "")
    derived = HDPath(path).derive(seed)
    assert derived.hex() == key
    assert hd.address_of(derived) == address


@pytest.mark.parametrize(("path", "address", "key"), PUBLIC_ACCOUNTS)
def test_independent_calculation_gives_the_published_vector(path, address, key):
    # proves the helper, so that it can prove the hardened paths below
    numbers = [int(p.rstrip("'")) + (helpers.HARD if p.endswith("'") else 0) for p in path.split("/")[1:]]
    derived = helpers.bip32_derive(helpers.bip39_seed(PUBLIC_WORDS), numbers)
    assert derived.hex() == key
    assert helpers.address_from_key(derived) == address


@pytest.mark.parametrize(("path", "numbers", "key"), BIP32_VECTOR_1)
def test_bip32_test_vector_1(path, numbers, key):
    assert HDPath(path).derive(BIP32_SEED).hex() == key
    assert helpers.bip32_derive(BIP32_SEED, numbers).hex() == key


@pytest.mark.parametrize(("entropy", "words"), BIP39_VECTORS)
def test_bip39_vectors(entropy, words):
    assert hd.entropy_to_words(bytes.fromhex(entropy)) == words
    assert hd.words_to_entropy(words).hex() == entropy
    assert ReferenceMnemonic("english").to_entropy(words).hex() == entropy


def test_bip39_seed_with_passphrase_vector():
    # vectors.json, entropy 00..00 (32 bytes), passphrase TREZOR: proves the PBKDF2 helper and the library
    words = BIP39_VECTORS[0][1]
    expected = (
        "bda85446c68413707090a52022edd26a1c9462295029f2e60cd7c4f2bbd30971"
        "70af7a4d73245cafa9c3cca8d561a7c3de6f5d4a10be8ed2a5e608d68f92fcc8"
    )
    assert helpers.bip39_seed(words, "TREZOR").hex() == expected
    assert Mnemonic.to_seed(words, "TREZOR").hex() == expected


def test_paths_are_hardened_on_every_level():
    assert hd.deposit_path(1, 12) == "m/44'/60'/1'/0'/12'"
    assert hd.deposit_path(0, 0) == "m/44'/60'/0'/0'/0'"
    assert hd.deposit_path(9000, 0) == "m/44'/60'/9000'/0'/0'"
    for path in (hd.deposit_path(7, 2**31 - 1), hd.deposit_path(9000, 0)):
        assert all(level.endswith("'") for level in path.split("/")[1:])


@pytest.mark.parametrize("account", [0, 1, 2, 8999, 9001, 2**31 - 1])
@pytest.mark.parametrize("index", [0, 1, 12, 199, 2**31 - 1])
def test_hardened_paths_agree_with_independent_calculation(account, index):
    entropy = os.urandom(32)
    wallet = hd.Wallet(entropy)
    seed = helpers.bip39_seed(ReferenceMnemonic("english").to_mnemonic(entropy))
    expected = helpers.bip32_derive(seed, helpers.deposit_path(account, index))
    assert wallet.deposit_key(account, index) == expected
    assert wallet.deposit_address(account, index) == helpers.address_from_key(expected)


def test_hardened_paths_of_the_published_mnemonic():
    # a second, fixed anchor: the public mnemonic on the signer's own path shape
    seed = Mnemonic.to_seed(PUBLIC_WORDS, "")
    for account, index in [(1, 0), (1, 1), (2, 0), (9000, 0)]:
        expected = helpers.bip32_derive(helpers.bip39_seed(PUBLIC_WORDS), helpers.deposit_path(account, index))
        assert HDPath(hd.deposit_path(account, index)).derive(seed) == expected
    # the hardened path is not the same key as the usual wallet path with the same numbers
    assert HDPath("m/44'/60'/1'/0'/0'").derive(seed) != HDPath("m/44'/60'/1'/0/0").derive(seed)


def test_fee_wallet_agrees_with_independent_calculation():
    entropy = os.urandom(32)
    wallet = hd.Wallet(entropy)
    seed = helpers.bip39_seed(ReferenceMnemonic("english").to_mnemonic(entropy))
    expected = helpers.bip32_derive(seed, helpers.FEE_PATH)
    assert wallet.fee_key() == expected
    assert wallet.fee_address == helpers.address_from_key(expected)


def test_words_and_entropy_round_trip_against_reference_implementation():
    for _ in range(20):
        entropy = os.urandom(32)
        words = ReferenceMnemonic("english").to_mnemonic(entropy)
        assert hd.entropy_to_words(entropy) == words
        assert hd.words_to_entropy(words) == entropy
        assert hd.words_to_entropy("  " + words.upper().replace(" ", "\t ") + "\n") == entropy


@pytest.mark.parametrize(("account", "index"), [(-1, 0), (0, -1), (2**31, 0), (0, 2**31)])
def test_out_of_range_numbers_are_refused(account, index):
    # eth_account accepts 2^31 as a node number (off by one); the signer must not pass it on
    with pytest.raises(ValueError):
        hd.deposit_path(account, index)


def test_fee_account_is_not_a_deposit_account():
    with pytest.raises(ValueError):
        hd.Wallet(os.urandom(32)).deposit_key(9000, 0)


def test_bad_words_are_refused_without_showing_them():
    words = ReferenceMnemonic("english").to_mnemonic(os.urandom(32)).split()
    wrong_checksum = [*words[:-1], "zoo" if words[-1] != "zoo" else "abandon"]
    cases = [words[:23], [*words, "abandon"], [*words[:5], "notaword", *words[6:]], wrong_checksum, words[:12]]
    texts = r"24 words are necessary|word [0-9]+ is not in the BIP39 English word list|the checksum of the words is wrong"
    for case in cases:
        with pytest.raises(hd.SeedError) as info:
            hd.words_to_entropy(" ".join(case))
        # the text is one of three fixed texts: it has a position number and no word of the input
        assert re.fullmatch(texts, str(info.value)), str(info.value)
    # a wrong last word can pass the 8-bit checksum by chance (1 of 256); then it is another valid seed


def test_wallet_does_not_show_key_material():
    entropy = os.urandom(32)
    wallet = hd.Wallet(entropy)
    shown = repr(wallet) + str(wallet) + repr(vars(wallet).keys())
    assert entropy.hex() not in shown
    assert wallet._seed.hex() not in shown
    assert not hasattr(wallet, "entropy")
    assert not hasattr(wallet, "words")
