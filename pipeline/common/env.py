"""Minimal .env loader, so API keys need not be exported by hand."""

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """
    Set KEY=VALUE lines from `path` as environment variables. Variables that
    are already set win, so an exported key overrides the file.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):]
        key, sep, value = line.partition('=')
        if sep and key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
