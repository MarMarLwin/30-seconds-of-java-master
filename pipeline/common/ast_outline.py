"""
The syntactic outline of a source file (imports, declarations, scope nodes),
shared by every language, and the base client that gets it from a parser tool
(tools/dart_ast, tools/java_ast). Each tool prints the same JSON format.
Used by Stage 1 (snippet scoping) and Stage 2 (retrieval and index chunking).
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SECONDS = 180

# Files per tool run, to stay well below the OS command-line length limit.
MAX_FILES_PER_RUN = 400

TYPE_KINDS = ('class', 'mixin', 'enum', 'extension', 'extension_type',
              'interface', 'record', 'annotation')


@dataclass
class Declaration:
    kind: str           # types: class, mixin, enum, extension, extension_type,
                        #   interface, record, annotation
                        # others: function, variable, typedef, method, getter,
                        #   setter, constructor, field, enum_constant
    name: str
    parent: Optional[str]
    start_line: int
    end_line: int
    signature: str

    @property
    def is_type(self) -> bool:
        return self.kind in TYPE_KINDS

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
        if self.is_type:
            return self.signature + ' { ... }'
        return self.signature + ';'


@dataclass
class Scope:
    kind: str           # Dart: member, function, statement, argument, catch
                        # Java: type, member, function, statement, catch, try, loop
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


class AstToolClient:
    """
    Runs one language's parser tool on demand and caches one outline per file.
    Whether the tool can run is checked on first use, so a missing SDK only
    warns when the repository has files in that language.
    """

    language = ''
    extensions: Tuple[str, ...] = ()

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._cache: Dict[str, Optional[FileOutline]] = {}
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = self._check_available()
        return self._available

    def handles(self, path: str) -> bool:
        return path.endswith(self.extensions)

    # Subclasses implement these three.
    def _check_available(self) -> bool:
        raise NotImplementedError

    def _command(self, paths: List[str]) -> Tuple[List[str], Path]:
        """The command line and working directory for parsing `paths`."""
        raise NotImplementedError

    def preload_paths(self) -> List[str]:
        """Repo-relative paths worth parsing up front, in one tool run."""
        raise NotImplementedError

    def outlines(self, paths: Iterable[str]) -> Dict[str, Optional[FileOutline]]:
        """Return outlines for the repo-relative paths this client handles (None if unparseable)."""
        wanted = sorted({p for p in paths if self.handles(p)})
        missing = [p for p in wanted if p not in self._cache]
        if missing:
            self._load(missing)
        return {p: self._cache.get(p) for p in wanted}

    def outline(self, path: str) -> Optional[FileOutline]:
        return self.outlines([path]).get(path)

    def preload(self) -> None:
        paths = self.preload_paths()
        if paths:
            self.outlines(paths)

    def _load(self, paths: List[str]) -> None:
        for path in paths:
            self._cache[path] = None
        existing = [p for p in paths if (self.repo_root / p).is_file()]
        if not existing or not self.available:
            return
        for i in range(0, len(existing), MAX_FILES_PER_RUN):
            self._run(existing[i:i + MAX_FILES_PER_RUN])

    def _run(self, paths: List[str]) -> None:
        cmd, cwd = self._command(paths)
        try:
            done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=TOOL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("%s AST tool timed out on %d file(s)", self.language, len(paths))
            return
        if done.returncode != 0:
            logger.warning("%s AST tool failed: %s", self.language, done.stderr.strip()[-500:])
            return
        try:
            data = json.loads(done.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            logger.warning("%s AST tool returned invalid JSON", self.language)
            return
        for entry in data:
            self._cache[entry['path']] = FileOutline(
                path=entry['path'],
                imports=entry.get('imports', []),
                declarations=[Declaration(**d) for d in entry.get('declarations', [])],
                scopes=[Scope(**s) for s in entry.get('scopes', [])],
            )
