"""Keystore (SPEC 3.3): format, tamper detection on every part, wrong master key."""

import base64
import json
import os
import stat

import pytest

from acctpool_signer import keystore
from acctpool_signer.errors import KeystoreError


def read(env) -> dict:
    with open(env.paths.keystore) as f:
        return json.load(f)


def write(env, doc: dict) -> None:
    with open(env.paths.keystore, "w") as f:
        json.dump(doc, f)


def flip_bit(hex_text: str, byte: int, bit: int = 0) -> str:
    data = bytearray(bytes.fromhex(hex_text))
    data[byte] ^= 1 << bit
    return data.hex()


def test_format_and_round_trip(env):
    doc = read(env)
    assert set(doc) == {"version", "seeds", "active"}
    assert doc["version"] == 1
    assert len(doc["seeds"]) == 1
    seed = doc["seeds"][0]
    assert set(seed) == {"seed_id", "created", "nonce", "ciphertext", "tag"}
    assert doc["active"] == seed["seed_id"]
    assert len(bytes.fromhex(seed["nonce"])) == 12
    assert len(bytes.fromhex(seed["ciphertext"])) == 32  # the entropy, not the words
    assert len(bytes.fromhex(seed["tag"])) == 16

    loaded = keystore.load(env.paths.keystore, env.master_key)
    assert loaded.active == seed["seed_id"]
    assert loaded.wallet.fee_address == env.fee_address
    assert loaded.wallet.deposit_key(1, 5) == env.key("shop", 5)


def test_seed_id_is_from_the_fee_wallet_address(env):
    import hashlib

    assert read(env)["active"] == hashlib.sha256(env.fee_address.encode()).hexdigest()[:16]


def test_file_mode_is_600_and_no_temp_file_stays(env):
    assert stat.S_IMODE(os.stat(env.paths.keystore).st_mode) == 0o600
    assert os.listdir(env.paths.data) == ["keystore.json"]


def test_no_plaintext_in_the_file(env):
    raw = open(env.paths.keystore, "rb").read()
    for secret in (env.entropy, env.seed, env.fee_key, env.master_key):
        assert secret not in raw
        assert secret.hex().encode() not in raw.lower()
        assert base64.b64encode(secret) not in raw
    for word in env.words.split():
        if len(word) > 4:  # short words can be in a hex string or a key name by chance
            assert word.encode() not in raw


def test_two_keystores_of_one_seed_have_different_nonce_and_ciphertext(env, tmp_path):
    other = str(tmp_path / "second.json")
    keystore.create(other, env.master_key, env.entropy)
    first, second = read(env)["seeds"][0], json.load(open(other))["seeds"][0]
    assert first["seed_id"] == second["seed_id"]
    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]


@pytest.mark.parametrize(("part", "size"), [("ciphertext", 32), ("tag", 16), ("nonce", 12)])
def test_one_changed_bit_is_detected(env, part, size):
    original = read(env)
    for byte in range(size):
        doc = json.loads(json.dumps(original))
        doc["seeds"][0][part] = flip_bit(original["seeds"][0][part], byte, byte % 8)
        write(env, doc)
        with pytest.raises(KeystoreError):
            keystore.load(env.paths.keystore, env.master_key)
    write(env, original)
    keystore.load(env.paths.keystore, env.master_key)


def test_changed_header_is_detected(env):
    original = read(env)
    seed = original["seeds"][0]
    other_id = ("0" if seed["seed_id"][0] != "0" else "1") + seed["seed_id"][1:]
    other_created = ("1999" if not seed["created"].startswith("1999") else "2000") + seed["created"][4:]
    changes = {
        "version": lambda doc: doc.update(version=2),
        "version 0": lambda doc: doc.update(version=0),
        "seed_id": lambda doc: (doc["seeds"][0].update(seed_id=other_id), doc.update(active=other_id)),
        "created": lambda doc: doc["seeds"][0].update(created=other_created),
    }
    for change in changes.values():
        doc = json.loads(json.dumps(original))
        change(doc)
        write(env, doc)
        with pytest.raises(KeystoreError):
            keystore.load(env.paths.keystore, env.master_key)


def test_header_is_authenticated_by_gcm_not_only_by_the_format_check(env):
    """Decrypt by hand with a changed header: the tag must fail. This proves that the header is in the AAD."""
    from Crypto.Cipher import AES

    seed = read(env)["seeds"][0]

    def decrypt(header: str) -> bytes:
        cipher = AES.new(env.master_key, AES.MODE_GCM, nonce=bytes.fromhex(seed["nonce"]))
        cipher.update(header.encode())
        return cipher.decrypt_and_verify(bytes.fromhex(seed["ciphertext"]), bytes.fromhex(seed["tag"]))

    assert decrypt(f"1|{seed['seed_id']}|{seed['created']}") == env.entropy
    for header in (f"2|{seed['seed_id']}|{seed['created']}", f"1|{'0' * 16}|{seed['created']}", f"1|{seed['seed_id']}|x", ""):
        with pytest.raises(ValueError):
            decrypt(header)


def test_wrong_master_key_is_detected(env):
    for wrong in (os.urandom(32), bytes(32), env.master_key[:-1] + bytes([env.master_key[-1] ^ 1])):
        with pytest.raises(KeystoreError):
            keystore.load(env.paths.keystore, wrong)


def test_damaged_structure_is_refused(env):
    original = read(env)
    changes = {
        "active names no seed": lambda doc: doc.update(active="0" * 16),
        "no seeds": lambda doc: doc.update(seeds=[]),
        "extra key": lambda doc: doc.update(debug=True),
        "extra key in seed": lambda doc: doc["seeds"][0].update(words="x"),
        "missing tag": lambda doc: doc["seeds"][0].pop("tag"),
        "short ciphertext": lambda doc: doc["seeds"][0].update(ciphertext=doc["seeds"][0]["ciphertext"][:-2]),
        "upper case hex": lambda doc: doc["seeds"][0].update(tag=doc["seeds"][0]["tag"].upper() + "A"),
        "seed two times": lambda doc: doc["seeds"].append(dict(doc["seeds"][0])),
        "version is a string": lambda doc: doc.update(version="1"),
        "version is true": lambda doc: doc.update(version=True),
    }
    for change in changes.values():
        doc = json.loads(json.dumps(original))
        change(doc)
        write(env, doc)
        with pytest.raises(KeystoreError):
            keystore.load(env.paths.keystore, env.master_key)
    for raw in (b"", b"{", b"[]", open(env.paths.keystore, "rb").read()[:-20]):
        with open(env.paths.keystore, "wb") as f:
            f.write(raw)
        with pytest.raises(KeystoreError):
            keystore.load(env.paths.keystore, env.master_key)


@pytest.mark.parametrize("active", [0, 1])
def test_keystore_with_two_seeds_is_refused(env, active):
    """SPEC 7.6: one seed in version 1. 'active' is not authenticated, so a second seed is not accepted."""
    keystore.load(env.paths.keystore, env.master_key)
    env.add_second_seed(active)
    doc = read(env)
    assert len(doc["seeds"]) == 2
    assert doc["active"] == doc["seeds"][active]["seed_id"]
    with pytest.raises(KeystoreError) as info:
        keystore.load(env.paths.keystore, env.master_key)
    assert "more than one seed" in str(info.value)
    # each of the two entries is correct when it is the only one
    for entry in doc["seeds"]:
        write(env, {"version": 1, "seeds": [entry], "active": entry["seed_id"]})
        assert keystore.load(env.paths.keystore, env.master_key).active == entry["seed_id"]


def test_error_text_has_no_key_material(env):
    doc = read(env)
    doc["seeds"][0]["tag"] = flip_bit(doc["seeds"][0]["tag"], 0)
    write(env, doc)
    with pytest.raises(KeystoreError) as info:
        keystore.load(env.paths.keystore, env.master_key)
    text = str(info.value) + repr(info.value)
    assert env.master_key.hex() not in text
    assert env.entropy.hex() not in text
    assert info.value.__cause__ is None


def test_create_does_not_replace_a_keystore(env):
    before = open(env.paths.keystore, "rb").read()
    with pytest.raises(KeystoreError):
        keystore.create(env.paths.keystore, env.master_key, os.urandom(32))
    assert open(env.paths.keystore, "rb").read() == before
    assert os.listdir(env.paths.data) == ["keystore.json"]


@pytest.mark.parametrize("failing", ["write", "short_write", "fsync"])
def test_a_failed_create_leaves_no_keystore(env, tmp_path, monkeypatch, failing):
    """K1: a write that fails or is short (disk full, I/O error) leaves no keystore, so start, init and restore are
    not blocked by a damaged file: the file comes into place complete or not at all."""

    def full(*args):
        raise OSError(28, "No space left on device")

    if failing == "short_write":
        real = keystore.os.write
        monkeypatch.setattr(keystore.os, "write", lambda fd, data: real(fd, data[:-1]))
    else:
        monkeypatch.setattr(keystore.os, failing, full)
    folder = tmp_path / "new"
    folder.mkdir()
    with pytest.raises(OSError):
        keystore.create(str(folder / "keystore.json"), env.master_key, os.urandom(32))
    assert os.listdir(folder) == []


def test_master_key_file_rules(env, tmp_path):
    assert keystore.read_master_key(env.paths.master_key) == env.master_key
    for name, content in {
        "short": bytes(31),
        "long": bytes(33),
        "empty": b"",
        "hex text": os.urandom(32).hex().encode(),
    }.items():
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(KeystoreError):
            keystore.read_master_key(str(path))
    with pytest.raises(KeystoreError):
        keystore.read_master_key(str(tmp_path / "missing"))
