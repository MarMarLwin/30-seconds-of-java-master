"""
Python client for pipeline/tools/dart_ast, which parses Dart files with
package:analyzer and returns their imports, declarations and scope nodes.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from ..workspace import PIPELINE_DIR
from .ast_outline import AstToolClient, Declaration, FileOutline, Scope  # noqa: F401

logger = logging.getLogger(__name__)

DART_AST_TOOL_DIR = PIPELINE_DIR / 'tools' / 'dart_ast'


class DartAstClient(AstToolClient):
    language = 'dart'
    extensions = ('.dart',)

    def __init__(self, repo_root: Path, tool_dir: Path = DART_AST_TOOL_DIR):
        super().__init__(repo_root)
        self.tool_dir = tool_dir

    def _check_available(self) -> bool:
        if not shutil.which('dart'):
            logger.warning("dart is not on PATH; AST scoping and structural "
                           "retrieval are disabled for Dart files")
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

    def _command(self, paths: List[str]) -> Tuple[List[str], Path]:
        return (['dart', 'run', str(self.tool_dir / 'bin' / 'dart_ast.dart'),
                 '--root', str(self.repo_root), *paths], self.tool_dir)

    def preload_paths(self) -> List[str]:
        """Every Dart file under lib/."""
        root = self.repo_root / 'lib'
        if not root.is_dir():
            return []
        return [p.relative_to(self.repo_root).as_posix() for p in sorted(root.rglob('*.dart'))]
