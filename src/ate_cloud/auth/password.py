"""Password hashing using pwdlib with Argon2.

pwdlib is the modern replacement for passlib (FastAPI docs updated 2025-2026).
Uses Argon2id for resistance against GPU/ASIC brute-force attacks.
"""

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_hasher = PasswordHash((Argon2Hasher(),))


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id.

    Args:
        password: Plaintext password to hash.

    Returns:
        Argon2 hash string.
    """
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against an Argon2 hash.

    Args:
        password: Plaintext password to check.
        hashed: Previously computed Argon2 hash.

    Returns:
        True if the password matches the hash, False otherwise.
    """
    return _hasher.verify(password, hashed)
