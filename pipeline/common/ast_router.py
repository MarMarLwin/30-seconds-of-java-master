"""
One entry point for every language with a parser tool: each call is passed to
the client for the file's extension. Files in other languages get no outline
(None), just like files a tool could not parse.
"""

from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .ast_outline import AstToolClient, FileOutline
from .dart_ast import DartAstClient
from .java_ast import JavaAstClient


class AstRouter:
    def __init__(self, repo_root: Path, clients: Optional[List[AstToolClient]] = None):
        self.repo_root = repo_root
        self.clients = clients if clients is not None else [DartAstClient(repo_root),
                                                            JavaAstClient(repo_root)]

    @property
    def available(self) -> bool:
        """True when at least one language can be parsed; unknown languages return None."""
        return bool(self.clients)

    def client_for(self, path: str) -> Optional[AstToolClient]:
        return next((c for c in self.clients if c.handles(path)), None)

    def outlines(self, paths: Iterable[str]) -> Dict[str, Optional[FileOutline]]:
        paths = list(paths)
        result: Dict[str, Optional[FileOutline]] = {}
        for client in self.clients:
            result.update(client.outlines(paths))
        return result

    def outline(self, path: str) -> Optional[FileOutline]:
        client = self.client_for(path)
        return client.outline(path) if client else None

    def preload(self) -> None:
        for client in self.clients:
            client.preload()

    @property
    def languages(self) -> List[str]:
        """Languages whose tool actually ran (for the index metadata)."""
        return [c.language for c in self.clients if c._available]
