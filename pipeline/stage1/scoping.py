"""
Choose how much code each issue's target snippet covers.

SonarQube's textRange marks only the flagged token(s): for S3776 that is just
the method name, so a fixed line buffer would hide the method the model is
asked to simplify. Instead the snippet is widened to the smallest enclosing
syntax node of a kind that suits the rule (see RULE_SCOPES).

The chosen range is written back as snippet_start_line/snippet_end_line, so
later stages patch exactly the lines the model was shown.
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..common.dart_ast import DartAstClient, Scope

logger = logging.getLogger(__name__)

# Scope kinds, from the Dart AST tool or computed here:
#   member      method, constructor or top-level function
#   function    innermost closure, or the member if there is none
#   expression  innermost named argument or statement
#   catch       catch clause
#   statement   innermost statement
#   yaml_step   one step of a GitHub Actions job
RULE_SCOPES = {
    'dart:S3776': 'member',      # complexity is a property of the whole function
    'dart:S7115': 'function',    # the async gap and the context use are in one body
    'dart:S3358': 'expression',
    'dart:S7055': 'catch',
}
DEFAULT_DART_SCOPE = 'statement'
DEFAULT_WORKFLOW_SCOPE = 'yaml_step'

# Which AST scope kinds satisfy each requested scope.
SCOPE_CANDIDATES = {
    'member': ('member',),
    'function': ('function', 'member'),
    'expression': ('argument', 'statement'),
    'catch': ('catch',),
    'statement': ('statement',),
}

# A scope larger than this is not useful as a snippet; fall back to lines.
MAX_SCOPE_LINES = 160

# Used when no syntactic scope applies.
FALLBACK_BUFFER_LINES = 3


@dataclass
class SnippetScope:
    start_line: int
    end_line: int
    kind: str


def scope_kind_for(rule: str, file_path: str) -> Optional[str]:
    if file_path.endswith('.dart'):
        return RULE_SCOPES.get(rule, DEFAULT_DART_SCOPE)
    if file_path.startswith('.github/workflows/') and rule.startswith('githubactions:'):
        return DEFAULT_WORKFLOW_SCOPE
    return None


def line_buffer_scope(start_line: int, end_line: int, total_lines: int,
                      buffer: int = FALLBACK_BUFFER_LINES) -> SnippetScope:
    return SnippetScope(max(1, start_line - buffer), min(total_lines, end_line + buffer),
                        'lines')


def smallest_enclosing(scopes: Sequence[Scope], kinds: Tuple[str, ...],
                       start_line: int, end_line: int) -> Optional[Scope]:
    """Smallest scope of one of `kinds` containing the range; earlier kinds win ties."""
    matches = [s for s in scopes if s.kind in kinds and s.contains(start_line, end_line)]
    if not matches:
        return None
    return min(matches, key=lambda s: (s.line_count, kinds.index(s.kind)))


def dart_scope(scopes: Sequence[Scope], kind: str, start_line: int,
               end_line: int) -> Optional[SnippetScope]:
    node = smallest_enclosing(scopes, SCOPE_CANDIDATES[kind], start_line, end_line)
    if node is None or node.line_count > MAX_SCOPE_LINES:
        return None
    return SnippetScope(node.start_line, node.end_line, kind)


_STEP_RE = re.compile(r'^(\s*)-\s')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def yaml_step_scope(lines: List[str], start_line: int, end_line: int) -> Optional[SnippetScope]:
    """
    The GitHub Actions step ("- name: ..." / "- uses: ..." list item under
    `steps:`) containing the flagged lines.
    """
    steps_line = None
    for i in range(start_line - 1, -1, -1):
        if lines[i].strip() == 'steps:':
            steps_line = i
            break
    if steps_line is None:
        return None

    step_start = None
    for i in range(start_line - 1, steps_line, -1):
        match = _STEP_RE.match(lines[i])
        if match and _indent(lines[i]) > _indent(lines[steps_line]):
            step_start = i
            break
    if step_start is None:
        return None
    step_indent = _indent(lines[step_start])

    step_end = step_start
    for i in range(step_start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # The next step, a dedent, or a comment introducing the next step.
        if _indent(lines[i]) <= step_indent:
            break
        step_end = i
    if step_end + 1 < end_line:
        return None
    return SnippetScope(step_start + 1, step_end + 1, 'yaml_step')


def resolve_scope(rule: str, file_path: str, lines: List[str], start_line: int,
                  end_line: int, ast: Optional[DartAstClient]) -> SnippetScope:
    kind = scope_kind_for(rule, file_path)
    scope = None
    if kind == 'yaml_step':
        scope = yaml_step_scope(lines, start_line, end_line)
    elif kind and ast is not None:
        outline = ast.outline(file_path)
        if outline is not None:
            scope = dart_scope(outline.scopes, kind, start_line, end_line)

    if scope is None:
        if kind:
            logger.info("No %s scope for %s:%d (%s); using a %d-line buffer",
                        kind, file_path, start_line, rule, FALLBACK_BUFFER_LINES)
        scope = line_buffer_scope(start_line, end_line, len(lines))
    return scope
