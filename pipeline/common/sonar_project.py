"""Reads a target repository's sonar-project.properties."""

from pathlib import Path
from typing import Dict


def read_sonar_properties(repo_root: Path) -> Dict[str, str]:
    """All key=value pairs from sonar-project.properties (empty if absent)."""
    props = repo_root / 'sonar-project.properties'
    if not props.is_file():
        return {}
    values = {}
    for line in props.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '!')):
            continue
        key, sep, value = line.partition('=')
        if sep:
            values[key.strip()] = value.strip()
    return values
