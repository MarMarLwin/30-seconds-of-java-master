"""
Python client for pipeline/tools/dart_ast, which parses Dart files with
package:analyzer and returns their imports, declarations and scope nodes.
Used by Stage 1 (snippet scoping) and Stage 2 (retrieval and index chunking).
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..workspace import PIPELINE_DIR

logger = logging.getLogger(__name__)

DART_AST_TOOL_DIR = PIPELINE_DIR / 'tools' / 'dart_ast'
DART_TIMEOUT_SECONDS = 180


@dataclass
class Declaration:
    kind: str           # class, mixin, enum, extension, function, variable,
                        # typedef, method, getter, setter, constructor, field,
                        # enum_constant
    name: str
    parent: Optional[str]
    start_line: int
    end_line: int
    signature: str

    @property
    def is_type(self) -> bool:
        return self.kind in ('class', 'mixin', 'enum', 'extension', 'extension_type')

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def outline_line(self) -> str:
        """How this member appears in its type's outline."""
        if self.kind == 'field':
            return self.signature           # already ends with ';'
        if self.kind == 'enum_constant':
            return self.signature + ','
        return self.signature + ';'


@dataclass
class Scope:
    kind: str           # member, function, statement, argument, catch
    start_line: int
    end_line: int

    def contains(self, start_line: int, end_line: int) -> bool:
        return self.start_line <= start_line and end_line <= self.end_line

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass
class FileOutline:
    path: str
    imports: List[Dict[str, str]] = field(default_factory=list)
    declarations: List[Declaration] = field(default_factory=list)
    scopes: List[Scope] = field(default_factory=list)

    def members_of(self, type_name: str) -> List[Declaration]:
        return [d for d in self.declarations if d.parent == type_name]

    def enclosing_type(self, start_line: int, end_line: int) -> Optional[Declaration]:
        types = [d for d in self.declarations if d.is_type
                 and d.start_line <= start_line and end_line <= d.end_line]
        return min(types, key=lambda d: d.line_count, default=None)


class DartAstClient:
    """Runs the Dart AST tool on demand and caches one outline per file."""

    def __init__(self, repo_root: Path, tool_dir: Path = DART_AST_TOOL_DIR):
        self.repo_root = repo_root
        self.tool_dir = tool_dir
        self._cache: Dict[str, Optional[FileOutline]] = {}
        self.available = self._check_available()

    def _check_available(self) -> bool:
        if not shutil.which('dart'):
            logger.warning("dart is not on PATH; AST scoping and structural "
                           "retrieval are disabled")
            return False
        if not (self.tool_dir / '.dart_tool' / 'package_config.json').is_file():
            logger.info("Resolving Dart AST tool dependencies in %s", self.tool_dir)
            for args in (['dart', 'pub', 'get', '--offline'], ['dart', 'pub', 'get']):
                done = subprocess.run(args, cwd=self.tool_dir, capture_output=True, text=True)
                if done.returncode == 0:
                    break
            else:
                logger.warning("Could not resolve %s: %s", self.tool_dir,
                               done.stderr.strip()[-500:])
                return False
        return True

    def outlines(self, paths: Iterable[str]) -> Dict[str, Optional[FileOutline]]:
        """Return outlines for repo-relative Dart paths (None if unparseable)."""
        wanted = sorted({p for p in paths if p.endswith('.dart')})
        missing = [p for p in wanted if p not in self._cache]
        if missing:
            self._load(missing)
        return {p: self._cache.get(p) for p in wanted}

    def preload(self, source_dir: str = 'lib') -> None:
        """Parse every Dart file under source_dir in one tool run."""
        root = self.repo_root / source_dir
        if root.is_dir():
            self.outlines(p.relative_to(self.repo_root).as_posix()
                          for p in sorted(root.rglob('*.dart')))

    def outline(self, path: str) -> Optional[FileOutline]:
        return self.outlines([path]).get(path)

    def _load(self, paths: List[str]) -> None:
        for path in paths:
            self._cache[path] = None
        if not self.available:
            return
        existing = [p for p in paths if (self.repo_root / p).is_file()]
        if not existing:
            return
        cmd = ['dart', 'run', str(self.tool_dir / 'bin' / 'dart_ast.dart'),
               '--root', str(self.repo_root), *existing]
        try:
            done = subprocess.run(cmd, cwd=self.tool_dir, capture_output=True,
                                  text=True, timeout=DART_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Dart AST tool timed out on %d file(s)", len(existing))
            return
        if done.returncode != 0:
            logger.warning("Dart AST tool failed: %s", done.stderr.strip()[-500:])
            return
        try:
            data = json.loads(done.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning("Dart AST tool returned invalid JSON")
            return
        for entry in data:
            self._cache[entry['path']] = FileOutline(
                path=entry['path'],
                imports=entry.get('imports', []),
                declarations=[Declaration(**d) for d in entry.get('declarations', [])],
                scopes=[Scope(**s) for s in entry.get('scopes', [])],
            )
