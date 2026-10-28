"""
Finds the repository files whose declarations a file can see: Dart imports
(package: and relative URIs) and Java imports plus the file's own package.
"""

import posixpath
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from ...common.ast_outline import FileOutline
from ...common.ast_router import AstRouter


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


def java_package(outline: FileOutline) -> str:
    return next((i['uri'] for i in outline.imports if i.get('kind') == 'package'), '')


def java_source_root(path: str, package: str) -> Optional[str]:
    """
    The source root a Java file sits in: 'src/main/java' for
    'src/main/java/a/b/C.java' in package 'a.b' ('' for the repository root),
    or None if the path does not match the package.
    """
    directory = posixpath.dirname(path)
    if not package:
        return directory
    suffix = package.replace('.', '/')
    if directory == suffix:
        return ''
    if directory.endswith('/' + suffix):
        return directory[:-len(suffix) - 1]
    return None


class JavaImportResolver:
    """
    Resolves Java imports to files in the repository's source roots
    (src/main/java, src/test/java, ... of every module). JDK and third-party
    classes are not in the repository and resolve to nothing.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._roots: Optional[List[str]] = None
        self._package_files: Dict[str, List[str]] = {}

    @property
    def roots(self) -> List[str]:
        if self._roots is None:
            found = {p.relative_to(self.repo_root).as_posix()
                     for pattern in ('src/*/java', '*/src/*/java')
                     for p in self.repo_root.glob(pattern) if p.is_dir()}
            self._roots = sorted(found)
        return self._roots

    def _roots_for(self, path: str, package: str) -> List[str]:
        own = java_source_root(path, package)
        roots = list(self.roots)
        if own is not None and own not in roots:
            roots.insert(0, own)
        return roots

    def package_files(self, package: str, roots: List[str]) -> List[str]:
        """Every .java file of `package`, across all source roots."""
        key = package + '|' + ','.join(roots)
        if key not in self._package_files:
            files = []
            for root in roots:
                directory = self.repo_root / root / package.replace('.', '/')
                if directory.is_dir():
                    files += [p.relative_to(self.repo_root).as_posix()
                              for p in sorted(directory.glob('*.java'))]
            self._package_files[key] = files
        return self._package_files[key]

    def class_file(self, name: str, roots: List[str]) -> Optional[str]:
        """The file declaring class `a.b.C` (or nested `a.b.C.Inner`), if it is in the repository."""
        parts = name.split('.')
        for end in range(len(parts), 0, -1):
            rel = '/'.join(parts[:end]) + '.java'
            for root in roots:
                candidate = posixpath.join(root, rel) if root else rel
                if (self.repo_root / candidate).is_file():
                    return candidate
        return None

    def imported_files(self, path: str, outline: FileOutline) -> List[str]:
        """Explicitly imported classes and packages first, then the file's own package."""
        package = java_package(outline)
        roots = self._roots_for(path, package)
        found: List[str] = []
        for imp in outline.imports:
            kind, uri = imp.get('kind'), imp.get('uri', '')
            if kind == 'import' and uri.endswith('.*'):
                found += self.package_files(uri[:-2], roots)
            elif kind == 'import':
                found.append(self.class_file(uri, roots))
            elif kind == 'static':
                # a.b.C.member or a.b.C.*: the class is everything before the last part.
                found.append(self.class_file(uri.rsplit('.', 1)[0], roots))
        found += self.package_files(package, roots)

        seen: List[str] = []
        for p in found:
            if p and p != path and p not in seen:
                seen.append(p)
        return seen


class ImportGraph:
    def __init__(self, repo_root: Path, ast: AstRouter):
        self.repo_root = repo_root
        self.ast = ast
        self.package_name = read_package_name(repo_root)
        self.java = JavaImportResolver(repo_root)

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
        Repository files whose declarations are visible from `path`. Dart: its
        direct imports, what those re-export, and the `part` files of each.
        Java: imported classes and packages, and the rest of its own package.
        """
        if path.endswith('.java'):
            outline = self.ast.outline(path)
            return self.java.imported_files(path, outline) if outline else []

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
