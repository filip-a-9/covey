"""ULIDs: 48-bit millisecond timestamp plus 80 random bits, Crockford base32."""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """Return a 26-character ULID. `now_ms` overrides the clock (for tests)."""
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    value = (now_ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
