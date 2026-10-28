"""
Python client for pipeline/tools/java_ast, which parses Java files with the
JDK's own compiler API and returns their imports, declarations and scope nodes.
It runs as a single source file (`java JavaAst.java`), so it needs only a JDK.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from ..workspace import PIPELINE_DIR
from .ast_outline import AstToolClient

logger = logging.getLogger(__name__)

JAVA_AST_TOOL = PIPELINE_DIR / 'tools' / 'java_ast' / 'JavaAst.java'
MIN_JAVA_VERSION = 17

# Build output and tooling directories, never parsed up front.
SKIPPED_DIRS = {'.git', '.gradle', '.idea', 'build', 'out', 'target', 'bin',
                'node_modules', '.scannerwork'}


def java_major_version(version_output: str) -> int:
    """17 from 'openjdk version "17.0.2"', 8 from 'java version "1.8.0_392"'."""
    match = re.search(r'version "(\d+)(?:\.(\d+))?', version_output)
    if not match:
        return 0
    major = int(match.group(1))
    return int(match.group(2) or 0) if major == 1 else major


class JavaAstClient(AstToolClient):
    language = 'java'
    extensions = ('.java',)

    def __init__(self, repo_root: Path, tool: Path = JAVA_AST_TOOL):
        super().__init__(repo_root)
        self.tool = tool

    def _check_available(self) -> bool:
        if not shutil.which('java'):
            logger.warning("java is not on PATH; AST scoping and structural "
                           "retrieval are disabled for Java files")
            return False
        done = subprocess.run(['java', '-version'], capture_output=True, text=True)
        version = java_major_version(done.stderr + done.stdout)
        if version < MIN_JAVA_VERSION:
            logger.warning("The Java AST tool needs JDK %d+ but java is version %s; AST "
                           "scoping and structural retrieval are disabled for Java files",
                           MIN_JAVA_VERSION, version or 'unknown')
            return False
        return True

    def _command(self, paths: List[str]) -> Tuple[List[str], Path]:
        return (['java', str(self.tool), '--root', str(self.repo_root), *paths],
                self.repo_root)

    def preload_paths(self) -> List[str]:
        """Every Java file in the repository outside build output and the pipeline."""
        paths = []
        for path in sorted(self.repo_root.rglob('*.java')):
            rel = path.relative_to(self.repo_root)
            if any(part in SKIPPED_DIRS for part in rel.parts[:-1]):
                continue
            if path.is_relative_to(PIPELINE_DIR):
                continue
            paths.append(rel.as_posix())
        return paths
