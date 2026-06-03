"""Fast content fingerprinting for duplicate detection.

The current implementation (size + partial hash + duration) is extremely effective
for real-world camera footage while remaining very fast.
"""

from __future__ import annotations

from pathlib import Path

import xxhash


def content_fingerprint(
    path: str | Path,
    duration: float | None = None,
    sample_bytes: int = 4 * 1024 * 1024,
) -> str:
    """
    Return a compact, highly effective fingerprint string.

    Format:  size:xxh3hex[:duration]
    This catches:
    - Exact duplicates
    - Same clip re-exported with tiny differences
    - Same original from different drives/cards
    """
    p = Path(path).expanduser().resolve()
    size = p.stat().st_size

    h = xxhash.xxh3_64()
    with p.open("rb") as f:
        h.update(f.read(sample_bytes))

    parts = [str(size), h.hexdigest()]
    if duration:
        parts.append(f"{duration:.1f}")
    return ":".join(parts)
