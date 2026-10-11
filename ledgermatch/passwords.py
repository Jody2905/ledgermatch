"""Password hashing with scrypt from Python's standard library.

scrypt is deliberately slow and memory-hungry, so guessing passwords from a
stolen database is expensive. Each password gets its own random salt.
Stored format:  scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>
"""

import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1
MIN_LENGTH = 10


def hash_password(password):
    if not password or len(password) < MIN_LENGTH:
        raise ValueError(f"Password must be at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)   # constant-time comparison
