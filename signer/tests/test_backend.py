"""Signing backend (SPEC 7.6). The proof itself is backend_proof.py; the image test runs the same file in the image.

The stock base image has no coincurve. scripts/test-signer.sh installs it in the test container from
signer/requirements.txt (hash-pinned) and sets SGN_BACKEND=coincurve. With SGN_BACKEND=native it installs
nothing: then the comparison tests are skipped and all other tests run on the pure-Python backend.
"""

import os
import re
from pathlib import Path

import backend_proof
import pytest

from acctpool_signer.__main__ import signing_backend

WANTED = os.environ.get("SGN_BACKEND", "")
REQUIREMENTS = Path(__file__).parent.parent / "requirements.txt"


def test_active_backend_is_the_one_that_the_test_run_asked_for():
    if not WANTED:
        pytest.skip("SGN_BACKEND is not set")
    assert signing_backend() == WANTED
    assert backend_proof.active_backend() == {"coincurve": "CoinCurveECCBackend", "native": "NativeECCBackend"}[WANTED]


def test_requirements_file_has_one_package_with_version_and_hash():
    lines = [line for line in REQUIREMENTS.read_text().splitlines() if line.strip() and not line.startswith("#")]
    text = " ".join(line.strip().rstrip("\\").strip() for line in lines)
    assert re.fullmatch(r"coincurve==[0-9]+\.[0-9]+\.[0-9]+ --hash=sha256:[0-9a-f]{64}", text), text


def test_installed_version_is_the_pinned_version():
    pytest.importorskip("coincurve")
    import importlib.metadata

    pinned = re.search(r"coincurve==([0-9.]+)", REQUIREMENTS.read_text()).group(1)
    assert importlib.metadata.version("coincurve") == pinned
    assert importlib.metadata.requires("coincurve") in (None, [])  # no package comes with it


def test_both_backends_give_the_same_signatures():
    pytest.importorskip("coincurve")
    assert backend_proof.compare_signatures(200) >= 200


def test_transactions_of_the_signer_are_equal_to_the_pure_python_result():
    # with the pure-Python backend active this compares the backend with itself: it proves nothing, so it is skipped
    pytest.importorskip("coincurve")
    if signing_backend() != "coincurve":
        pytest.skip("the active backend is not coincurve")
    assert backend_proof.compare_transactions(40) == 40


def test_proof_script_result(capsys):
    code = backend_proof.main()
    out = capsys.readouterr().out
    if signing_backend() == "coincurve":
        assert code == 0
        assert out.count("ok  ") == 4
        assert "FAIL" not in out
    else:
        assert code == 1
        assert "FAIL the active backend is not coincurve" in out
