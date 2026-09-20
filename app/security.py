"""Password storage.

PBKDF2-HMAC-SHA256 from the standard library: no extra dependency, no native
build step, and strong enough for this service. The stored string carries its
own algorithm, iteration count and salt, so the work factor can be raised later
without invalidating existing passwords.
"""

import hashlib
import hmac
import os
from base64 import b64decode, b64encode

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 240_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "$".join(
        [ALGORITHM, str(ITERATIONS), b64encode(salt).decode(), b64encode(digest).decode()]
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A user with no password set can never log in."""
    if not stored:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        expected = b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), b64decode(salt_b64), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)
