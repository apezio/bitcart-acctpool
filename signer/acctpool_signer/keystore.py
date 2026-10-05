"""Encrypted seed (SPEC 3.3, 7.6): AES-256-GCM of the BIP39 entropy, "<version>|<seed_id>|<created>" authenticated."""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime

from Crypto.Cipher import AES  # noqa: S413  (this is pycryptodome, which SPEC 3.3 names)

from .errors import KeystoreError
from .hd import ENTROPY_BYTES, Wallet

VERSION, MASTER_KEY_BYTES, NONCE_BYTES, TAG_BYTES = 1, 32, 12, 16
FORMATS = {"seed_id": r"[0-9a-f]{16}", "created": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
           "nonce": f"[0-9a-f]{{{2 * NONCE_BYTES}}}", "ciphertext": f"[0-9a-f]{{{2 * ENTROPY_BYTES}}}",
           "tag": f"[0-9a-f]{{{2 * TAG_BYTES}}}"}  # fmt: skip


@dataclass(frozen=True)
class Keystore:
    active: str  # the seed id
    wallet: Wallet


def seed_id_of(wallet: Wallet) -> str:
    return hashlib.sha256(wallet.fee_address.encode("ascii")).hexdigest()[:16]


def read_master_key(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            key = f.read(MASTER_KEY_BYTES + 1)
    except OSError as e:
        raise KeystoreError(f"cannot read the master key file {path}: {e.strerror}") from None
    if len(key) != MASTER_KEY_BYTES:
        raise KeystoreError(f"the master key file {path} must have exactly {MASTER_KEY_BYTES} bytes")
    return key


def _seed(doc: object) -> dict[str, str]:
    """The one seed entry of a keystore document, with every field in its format."""
    if not isinstance(doc, dict) or set(doc) != {"version", "seeds", "active"} or repr(doc["version"]) != str(VERSION):
        raise KeystoreError("keystore: wrong structure or version")
    # "active" is not authenticated: with two seeds a changed file could select the other one.
    if not isinstance(doc["seeds"], list) or len(doc["seeds"]) != 1:
        raise KeystoreError("keystore: no seed or more than one seed; version 1 has one seed (SPEC 7.6)")
    entry = doc["seeds"][0]
    if not isinstance(entry, dict) or set(entry) != set(FORMATS) or doc["active"] != entry["seed_id"]:
        raise KeystoreError("keystore: wrong seed structure, or the active seed is not in the file")
    for name, pattern in FORMATS.items():
        if not isinstance(entry[name], str) or re.fullmatch(pattern, entry[name]) is None:
            raise KeystoreError(f"keystore: {name} is damaged")
    return entry


def load(path: str, master_key: bytes) -> Keystore:
    """Decrypt the seed. Any changed byte makes this fail; the caller then refuses to start."""
    try:
        with open(path, "rb") as f:
            entry = _seed(json.loads(f.read()))
    except OSError as e:
        raise KeystoreError(f"cannot read the keystore {path}: {e.strerror}") from None
    except ValueError:
        raise KeystoreError("keystore: not valid JSON") from None
    cipher = AES.new(master_key, AES.MODE_GCM, nonce=bytes.fromhex(entry["nonce"]))
    cipher.update(f"{VERSION}|{entry['seed_id']}|{entry['created']}".encode())
    try:
        entropy = cipher.decrypt_and_verify(bytes.fromhex(entry["ciphertext"]), bytes.fromhex(entry["tag"]))
    except ValueError:
        raise KeystoreError("keystore: authentication failed (wrong master key or changed file)") from None
    wallet = Wallet(entropy)
    if seed_id_of(wallet) != entry["seed_id"]:
        raise KeystoreError("keystore: seed_id does not agree with the seed")
    return Keystore(active=entry["seed_id"], wallet=wallet)


def create(path: str, master_key: bytes, entropy: bytes) -> Keystore:
    """Write a new keystore with one seed: a temp file in the same folder (fsync), then a link to the name, which
    never replaces a file (also not a link): the keystore is complete or not there. Then fsync of the folder."""
    wallet = Wallet(entropy)
    seed_id, created, nonce = seed_id_of(wallet), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), os.urandom(NONCE_BYTES)
    cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
    cipher.update(f"{VERSION}|{seed_id}|{created}".encode())
    ciphertext, tag = cipher.encrypt_and_digest(entropy)
    seed = {"seed_id": seed_id, "created": created, "nonce": nonce.hex(), "ciphertext": ciphertext.hex(), "tag": tag.hex()}
    data = json.dumps({"version": VERSION, "seeds": [seed], "active": seed_id}, indent=1).encode() + b"\n"
    folder = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".keystore-")  # mode 600
    try:
        if os.write(fd, data) != len(data):
            raise OSError(0, "short write")
        os.fsync(fd)
        os.link(temp, path)
    except FileExistsError:
        raise KeystoreError(f"a keystore exists at {path}; it is not replaced") from None
    finally:
        os.close(fd)
        os.unlink(temp)
    dir_fd = os.open(folder, os.O_RDONLY)
    os.fsync(dir_fd)
    os.close(dir_fd)
    return Keystore(active=seed_id, wallet=wallet)
