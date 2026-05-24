"""Load + version `soul.md` — Wolfpaw's global agent persona.

The Soul file is loaded once per process. Its `version` is the first 12
hex chars of the SHA-256 of its content, which lets us stamp
`threads.soul_version` so plans/threads remain tied to the persona that
was active when they ran (procedural-memory retrieval can use this as a
filter in a later step).

Path resolution:
    1. `settings.soul_path` if set
    2. `<repo_root>/soul.md` (relative to this file)
    3. None → loader raises FileNotFoundError; agents degrade by
       passing `soul=None` to `build_system_prompt`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()


@dataclass(frozen=True)
class Soul:
    content: str
    version: str   # 12-char SHA-256 prefix
    source: str    # path or "(default)" — diagnostic


def default_soul_path() -> Path:
    """`<repo_root>/soul.md` relative to this file. Used when
    `settings.soul_path` is empty."""
    # src/wolfpaw/persona/soul.py → src/wolfpaw/persona → src/wolfpaw → src → repo
    return Path(__file__).resolve().parents[3] / "soul.md"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def load_soul(path: Path | None = None) -> Soul:
    """Read the Soul file from disk. Raises FileNotFoundError if missing.
    Use `get_soul()` for the cached singleton."""
    if path is None:
        settings = get_settings()
        path = Path(settings.soul_path) if settings.soul_path else default_soul_path()
    if not path.exists():
        raise FileNotFoundError(f"soul file not found at {path}")
    content = path.read_text(encoding="utf-8")
    return Soul(content=content, version=_hash(content), source=str(path))


_cached: Soul | None = None


def get_soul() -> Soul:
    """Process-wide singleton. Loaded lazily on first call.

    Tests / dev can call `reset_soul()` to drop the cache (e.g. after
    editing the file)."""
    global _cached
    if _cached is None:
        _cached = load_soul()
        log.info(
            "persona.soul.loaded",
            version=_cached.version,
            source=_cached.source,
            chars=len(_cached.content),
        )
    return _cached


def reset_soul() -> None:
    """Test/dev hook — drop the cached Soul so the next call re-reads."""
    global _cached
    _cached = None


def set_soul_for_test(content: str, *, version: str | None = None) -> Soul:
    """Install a Soul directly without reading any file. Tests use this
    to control prompt content without touching the filesystem."""
    global _cached
    _cached = Soul(
        content=content,
        version=version or _hash(content),
        source="(test override)",
    )
    return _cached
