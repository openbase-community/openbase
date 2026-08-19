"""Phrase normalization and memory-hard verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata

MIN_PHRASE_WORDS = 6
MIN_PHRASE_CHARS = 24


def normalize_phrase(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.translate(str.maketrans({"’": "'", "‘": "'", "`": "'"}))
    normalized = " ".join(normalized.split())
    return re.sub(r"[.?!]+$", "", normalized).strip()


def validate_new_phrase(value: str) -> str:
    normalized = normalize_phrase(value)
    if len(normalized) < MIN_PHRASE_CHARS or len(normalized.split()) < MIN_PHRASE_WORDS:
        raise ValueError(
            f"Safe phrase must contain at least {MIN_PHRASE_WORDS} words and {MIN_PHRASE_CHARS} characters."
        )
    return normalized


def derive_verifier(normalized_phrase: str, salt: bytes) -> str:
    # scrypt is available through Python's audited OpenSSL binding and keeps
    # offline guesses expensive without introducing a plaintext fallback.
    derived = hashlib.scrypt(
        normalized_phrase.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return base64.b64encode(derived).decode("ascii")


def verify_phrase(value: str, *, salt_b64: str, verifier_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(verifier_b64, validate=True)
        actual = base64.b64decode(derive_verifier(normalize_phrase(value), salt), validate=True)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
