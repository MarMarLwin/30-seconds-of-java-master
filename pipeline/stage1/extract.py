"""
Stage 1b: turn SonarQube issues into target code snippets.

Each snippet is widened from SonarQube's flagged lines to the enclosing
syntax node that suits the rule (see scoping.py), e.g. the whole method for a
Cognitive Complexity issue. scope='lines' gives a fixed buffer of
CONTEXT_BUFFER lines instead.

Run with: python -m pipeline extract --repo <path>
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..common.dart_ast import DartAstClient
from .scoping import line_buffer_scope, resolve_scope

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Only issues in files with these extensions are extracted (configuration
# files such as AndroidManifest.xml are skipped).
TARGET_EXTENSIONS = ['.dart', '.yaml', '.yml', '.js', '.jsx', '.ts', '.tsx',
                     '.java', '.kt', '.swift', '.py']

# Number of surrounding lines included before startLine and after endLine
# with --scope lines, or when no syntactic scope applies.
CONTEXT_BUFFER = 3

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_issues(issues_path: Path) -> List[Dict[str, Any]]:
    """
    Load the SonarQube issues JSON.

    Accepts either a bare list of issue objects or the raw SonarQube API
    response shape ({"issues": [...]}).
    """
    try:
        with issues_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("Issues file not found: %s", issues_path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", issues_path, e)
        sys.exit(1)

    if isinstance(data, dict) and isinstance(data.get('issues'), list):
        return data['issues']
    if isinstance(data, list):
        return data

    logger.error("Unexpected JSON structure: expected a list or {'issues': [...]}")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def parse_component_path(component: str) -> Optional[str]:
    """
    Convert "ProjectName:relative/path" into "relative/path".

    Splits on the first ':' only, so any colons inside the path are preserved.
    """
    if not component or ':' not in component:
        return None
    _, file_path = component.split(':', 1)
    return file_path.strip() or None


def has_target_extension(file_path: str) -> bool:
    """Return True if the file ends with one of TARGET_EXTENSIONS (case-insensitive)."""
    lowered = file_path.lower()
    return any(lowered.endswith(ext.lower()) for ext in TARGET_EXTENSIONS)


def resolve_local_path(repo_root: Path, relative_path: str) -> Optional[Path]:
    """
    Resolve a repo-relative path to an absolute path, refusing anything that
    escapes the repository root (e.g. "../../etc/passwd").
    """
    full_path = (repo_root / relative_path).resolve()
    try:
        full_path.relative_to(repo_root)
    except ValueError:
        return None
    return full_path


# --------------------------------------------------------------------------- #
# Snippet extraction
# --------------------------------------------------------------------------- #

def read_file_lines(file_path: Path, cache: Dict[Path, List[str]]) -> List[str]:
    """Read a file's lines once and cache them; many issues often share a file."""
    if file_path not in cache:
        with file_path.open('r', encoding='utf-8', errors='replace') as f:
            cache[file_path] = f.read().splitlines()
    return cache[file_path]


def extract_snippet(lines: List[str], start_line: int, end_line: int, rule: str,
                    file_path: str, ast: Optional[DartAstClient]) -> Dict[str, Any]:
    """
    Return the snippet for an issue: the enclosing syntax node when `ast` is
    given (or the file is a workflow), otherwise the lines
    [start_line - CONTEXT_BUFFER, end_line + CONTEXT_BUFFER], clamped to the
    file bounds. Line numbers are 1-indexed and inclusive.

    Raises IndexError if the issue's own range lies outside the file.
    """
    total = len(lines)
    if start_line < 1 or end_line < start_line:
        raise IndexError(f"invalid line range {start_line}-{end_line}")
    if start_line > total:
        raise IndexError(f"startLine {start_line} exceeds file length ({total} lines)")

    # Clamp endLine in case the file changed since the scan.
    end_line = min(end_line, total)

    if ast is not None:
        scope = resolve_scope(rule, file_path, lines, start_line, end_line, ast)
    else:
        scope = line_buffer_scope(start_line, end_line, total, CONTEXT_BUFFER)

    # Convert 1-indexed inclusive range to 0-indexed slice.
    snippet = '\n'.join(lines[scope.start_line - 1:scope.end_line])

    return {
        'target_snippet': snippet,
        'snippet_start_line': scope.start_line,
        'snippet_end_line': scope.end_line,
        'scope_kind': scope.kind,
    }


def process_issue(issue: Dict[str, Any], repo_root: Path, cache: Dict[Path, List[str]],
                  ast: Optional[DartAstClient]) -> Optional[Dict[str, Any]]:
    """Turn one SonarQube issue into an output record, or None if skipped."""
    key = issue.get('key', '<no-key>')

    relative_path = parse_component_path(issue.get('component', ''))
    if not relative_path:
        logger.warning("[%s] Skipping: cannot parse component %r", key, issue.get('component'))
        return None

    if not has_target_extension(relative_path):
        logger.debug("[%s] Skipping non-target file: %s", key, relative_path)
        return None

    # File-level issues (no textRange) have no snippet to extract.
    text_range = issue.get('textRange')
    if not text_range:
        logger.warning("[%s] Skipping: no textRange (file-level issue) in %s", key, relative_path)
        return None

    try:
        start_line = int(text_range['startLine'])
        end_line = int(text_range.get('endLine', start_line))
    except (KeyError, TypeError, ValueError):
        logger.warning("[%s] Skipping: malformed textRange %r", key, text_range)
        return None

    local_path = resolve_local_path(repo_root, relative_path)
    if local_path is None:
        logger.warning("[%s] Skipping: path escapes repository root: %s", key, relative_path)
        return None

    try:
        lines = read_file_lines(local_path, cache)
        snippet_info = extract_snippet(lines, start_line, end_line,
                                       issue.get('rule') or '', relative_path, ast)
    except FileNotFoundError:
        logger.warning("[%s] File not found: %s", key, local_path)
        return None
    except (IsADirectoryError, PermissionError) as e:
        logger.warning("[%s] Cannot read %s: %s", key, local_path, e)
        return None
    except IndexError as e:
        logger.warning("[%s] Line range error in %s: %s", key, relative_path, e)
        return None

    return {
        'file_path': relative_path,
        'rule': issue.get('rule'),
        'message': issue.get('message'),
        'target_snippet': snippet_info['target_snippet'],
        # Extra metadata so a later pipeline stage can patch the file back.
        'issue_key': key,
        'start_line': start_line,
        'end_line': end_line,
        'snippet_start_line': snippet_info['snippet_start_line'],
        'snippet_end_line': snippet_info['snippet_end_line'],
        'scope_kind': snippet_info['scope_kind'],
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def save_results(results: List[Dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d snippet(s) to %s", len(results), output_path)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def extract_snippets(issues_path: Path, repo_root: Path, output_path: Path,
                     scope: str = 'ast') -> int:
    """Write the snippet records for every usable issue; returns their count."""
    issues = load_issues(issues_path)
    logger.info("Loaded %d issue(s) from %s", len(issues), issues_path)

    # Workflow steps are scoped without the AST tool, so `ast` is still passed
    # when dart is unavailable (Dart issues then fall back to the line buffer).
    ast = DartAstClient(repo_root) if scope == 'ast' else None
    if ast is not None:
        ast.preload()

    cache: Dict[Path, List[str]] = {}
    results = []
    for issue in issues:
        if not isinstance(issue, dict):
            logger.warning("Skipping non-object issue entry: %r", issue)
            continue
        record = process_issue(issue, repo_root, cache, ast)
        if record:
            results.append(record)

    logger.info("Extracted %d / %d issue(s)", len(results), len(issues))
    save_results(results, output_path)
    return len(results)
