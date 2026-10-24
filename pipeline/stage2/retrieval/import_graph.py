"""Resolves Dart import URIs to files inside the repository."""

import posixpath
import re
from pathlib import Path
from typing import List, Optional, Set

from ...common.dart_ast import DartAstClient


def read_package_name(repo_root: Path) -> Optional[str]:
    """The `name:` from pubspec.yaml, used to resolve package:<name>/ imports."""
    pubspec = repo_root / 'pubspec.yaml'
    if not pubspec.is_file():
        return None
    match = re.search(r'^name:\s*([A-Za-z0-9_]+)', pubspec.read_text(encoding='utf-8'),
                      re.MULTILINE)
    return match.group(1) if match else None


def resolve_uri(uri: str, from_path: str, package_name: Optional[str]) -> Optional[str]:
    """
    Map an import URI to a repo-relative path, or None for SDK and third-party
    packages (dart:, package:flutter/, ...), which are not in the repository.
    """
    if not uri or uri.startswith('dart:'):
        return None
    if uri.startswith('package:'):
        if not package_name or not uri.startswith(f'package:{package_name}/'):
            return None
        return 'lib/' + uri[len(f'package:{package_name}/'):]
    if ':' in uri:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), uri))
    return None if resolved.startswith('..') else resolved


class ImportGraph:
    def __init__(self, repo_root: Path, ast: DartAstClient):
        self.repo_root = repo_root
        self.ast = ast
        self.package_name = read_package_name(repo_root)

    def _resolved(self, path: str, kinds: Set[str]) -> List[str]:
        outline = self.ast.outline(path)
        if outline is None:
            return []
        out = []
        for imp in outline.imports:
            if imp.get('kind') not in kinds:
                continue
            target = resolve_uri(imp.get('uri', ''), path, self.package_name)
            if target and (self.repo_root / target).is_file():
                out.append(target)
        return out

    def parts_of(self, path: str) -> List[str]:
        """Files joined to this library with `part` (e.g. generated .g.dart)."""
        return self._resolved(path, {'part'})

    def imported_files(self, path: str) -> List[str]:
        """
        Repository files whose declarations are visible from `path`: its direct
        imports, what those re-export, and the `part` files of each.
        """
        seen: List[str] = []

        def add(p: str) -> None:
            if p != path and p not in seen:
                seen.append(p)

        direct = self._resolved(path, {'import'})
        for target in direct:
            add(target)
            for exported in self._resolved(target, {'export'}):
                add(exported)
        for library in list(seen):
            for part in self.parts_of(library):
                add(part)
        return seen
