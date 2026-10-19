"""Find demo files and group split recordings (`<name>-p1.dem`, `<name>-p2.dem`) into one match."""

import re
from pathlib import Path

PART_RE = re.compile(r"^(?P<base>.+)-p(?P<part>\d+)$")


def find_matches(root: str | Path) -> dict[Path, list[Path]]:
    """Map `<dir>/<match name>` -> the match's demo files in part order."""
    groups: dict[Path, list[tuple[int, Path]]] = {}
    for path in sorted(Path(root).rglob("*.dem")):
        m = PART_RE.match(path.stem)
        base, part = (m["base"], int(m["part"])) if m else (path.stem, 0)
        groups.setdefault(path.parent / base, []).append((part, path))
    return {key: [p for _, p in sorted(parts)] for key, parts in groups.items()}
