"""
Repository-aware (RAG) context.

Candidates are gathered in priority order and added until the token budget is
used up:

  1. enclosing_class       An outline (fields and member signatures) of the
                           class around the snippet, e.g. the State fields and
                           helper methods a widget's build method uses.
     same_file             For non-Dart files: the rest of the file, with the
                           snippet replaced by a marker.
  2. imported_declaration  Declarations, in files the target imports, of the
                           symbols the snippet uses (found via the Dart AST and
                           import graph). Symbols on the flagged lines first.
                           A bare name matches top-level declarations only; a
                           name after a dot matches members, restricted to the
                           receiver's type when the receiver is a type name
                           (AppLabels.email).
  3. vector                Semantically similar code in the same language from
                           the ChromaDB index.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import (DEFAULT_CONTEXT_BUDGET_TOKENS, MAX_INLINE_BODY_LINES,
                      MAX_SYMBOL_MATCHES, MIN_VECTOR_SCORE, VECTOR_OVERFETCH,
                      VECTOR_TOP_K)
from ..models import ContextChunk, IssueRecord, RuleInfo, fence_language
from ...common.dart_ast import DartAstClient, Declaration, FileOutline
from ..retrieval.import_graph import ImportGraph
from ..retrieval.vector_index import VectorIndex

logger = logging.getLogger(__name__)

# Never worth looking up: Dart keywords, core types, and names so common in
# Flutter code that any match would be a guess.
IGNORED_IDENTIFIERS = set("""
    abstract as assert async await base break case catch class const continue
    covariant default deferred do dynamic else enum export extends extension
    external factory false final finally for get hide if implements import in
    interface is late library mixin new null on operator part required rethrow
    return sealed set show static super switch sync this throw true try typedef
    var void when while with yield
    String int double bool num List Map Set Future Stream Object Function
    Widget BuildContext Key Duration DateTime Iterable
    build context state ref key value data name child children toString
    dispose initState createState of index error e st print debugPrint
""".split())

MAX_OUTLINE_MEMBERS = 30
SAME_FILE_WINDOW_LINES = 60

_IDENTIFIER_RE = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_COMMENT_RE = re.compile(r'//[^\n]*')
_RECEIVER_RE = re.compile(r'([A-Za-z_$][A-Za-z0-9_$]*)\s*[?!]?\.$')


def references_in(code: str) -> List[Tuple[str, Optional[str]]]:
    """
    (name, receiver) for each identifier, in order of first appearance,
    ignoring strings and comments. receiver is None for a bare name, the
    identifier before the dot for `receiver.name`, or '' when the dot follows
    an expression such as a call.
    """
    code = _COMMENT_RE.sub('', _STRING_RE.sub("''", code))
    seen: Dict[Tuple[str, Optional[str]], None] = {}
    for match in _IDENTIFIER_RE.finditer(code):
        name = match.group(0)
        if name in IGNORED_IDENTIFIERS or len(name) < 2:
            continue
        before = code[:match.start()].rstrip()
        receiver = None
        if before.endswith('.'):
            found = _RECEIVER_RE.search(before)
            receiver = found.group(1) if found else ''
        seen.setdefault((name, receiver), None)
    return list(seen)


def _fenced(issue: IssueRecord, header: str, body: str) -> str:
    comment = '#' if fence_language(issue.file_path) == 'yaml' else '//'
    return f"```{fence_language(issue.file_path)}\n{comment} {header}\n{body}\n```"


def _duplicates(a: ContextChunk, b: ContextChunk) -> bool:
    """
    True if `a` repeats `b`: the same range, or quoted code overlapping other
    quoted code. A summary (outline) of a range does not repeat code inside it.
    """
    if (a.source_path, a.start_line, a.end_line) == (b.source_path, b.start_line, b.end_line):
        return True
    if b.summary and a.start_line == a.end_line and a.overlaps(b):
        return True  # a one-line member already shown in full by the outline
    return not a.summary and not b.summary and a.overlaps(b)


def _lines_label(start: int, end: int) -> str:
    return f"line {start}" if start == end else f"lines {start}-{end}"


def _matching(candidates: List[Tuple[FileOutline, Declaration]], receiver: Optional[str]
              ) -> List[Tuple[FileOutline, Declaration]]:
    if receiver is None:
        return [(o, d) for o, d in candidates if d.parent is None]
    if receiver[:1].isupper():
        return [(o, d) for o, d in candidates if d.parent == receiver]
    return [(o, d) for o, d in candidates if d.parent is not None]


class RepoAwareStrategy:
    name = 'repository_aware'

    def __init__(self, repo_root: Path, ast: Optional[DartAstClient],
                 vector: Optional[VectorIndex],
                 budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS):
        self.repo_root = repo_root
        self.ast = ast if ast is not None and ast.available else None
        self.graph = ImportGraph(repo_root, self.ast) if self.ast else None
        self.vector = vector
        self.budget_tokens = budget_tokens
        self._lines: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ #

    def build(self, issue: IssueRecord, rule: RuleInfo) -> List[ContextChunk]:
        candidates: List[ContextChunk] = []
        if issue.language == 'dart':
            if self.graph is not None:
                candidates += self._enclosing_class(issue)
                candidates += self._imported_declarations(issue)
        else:
            candidates += self._same_file(issue)
        if self.vector is not None:
            candidates += self._vector_hits(issue, rule)
        return self._select(candidates)

    def _select(self, candidates: List[ContextChunk]) -> List[ContextChunk]:
        """Take candidates in order, skipping duplicates and anything over budget."""
        selected: List[ContextChunk] = []
        used = 0
        for chunk in candidates:
            if any(_duplicates(chunk, s) for s in selected):
                continue
            cost = chunk.approx_tokens + 1
            if used + cost > self.budget_tokens:
                continue
            selected.append(chunk)
            used += cost
        return selected

    def _file_lines(self, path: str) -> List[str]:
        if path not in self._lines:
            try:
                text = (self.repo_root / path).read_text(encoding='utf-8', errors='replace')
            except OSError:
                text = ''
            self._lines[path] = text.splitlines()
        return self._lines[path]

    # ------------------------------------------------------------------ #
    # 2. Declarations of symbols the snippet uses
    # ------------------------------------------------------------------ #

    def _symbol_table(self, files: List[str]
                      ) -> Dict[str, List[Tuple[FileOutline, Declaration]]]:
        table: Dict[str, List[Tuple[FileOutline, Declaration]]] = {}
        for path, outline in self.ast.outlines(files).items():
            if outline is None:
                continue
            for decl in outline.declarations:
                name = decl.name.split('.')[-1]
                if decl.kind == 'constructor' and '.' not in decl.name:
                    continue  # the type itself is a better match
                table.setdefault(name, []).append((outline, decl))
        return table

    def _imported_declarations(self, issue: IssueRecord) -> List[ContextChunk]:
        files = self.graph.imported_files(issue.file_path)
        if not files:
            return []
        table = self._symbol_table(files)

        flagged = references_in(issue.flagged_code)
        rest = [r for r in references_in(issue.target_snippet) if r not in flagged]
        chunks = []
        for name, receiver in flagged + rest:
            matches = _matching(table.get(name, []), receiver)
            if not matches or len(matches) > MAX_SYMBOL_MATCHES:
                continue
            for outline, decl in matches:
                chunks.append(self._declaration_chunk(issue, outline, decl))
        return chunks

    def _declaration_chunk(self, issue: IssueRecord, outline: FileOutline,
                           decl: Declaration) -> ContextChunk:
        symbol = f"{decl.parent}.{decl.name}" if decl.parent and '.' not in decl.name \
            else decl.name
        if decl.is_type:
            body = self._outline_body(outline, decl)
        elif decl.line_count <= MAX_INLINE_BODY_LINES:
            body = '\n'.join(self._file_lines(outline.path)[decl.start_line - 1:decl.end_line])
        else:
            body = f"{decl.signature} {{ ... }}"
        header = (f"{outline.path}, {_lines_label(decl.start_line, decl.end_line)}: "
                  f"{decl.kind} {symbol}")
        return ContextChunk(text=_fenced(issue, header, body), method='imported_declaration',
                            source_path=outline.path, start_line=decl.start_line,
                            end_line=decl.end_line, symbol=symbol, summary=decl.is_type)

    @staticmethod
    def _outline_body(outline: FileOutline, decl: Declaration,
                      skip: Optional[Tuple[int, int]] = None) -> str:
        members = outline.members_of(decl.name)
        lines = []
        for member in members:
            if skip and member.start_line <= skip[1] and member.end_line >= skip[0]:
                lines.append(f"  // {member.signature} ... (contains the code to refactor)")
                continue
            lines.append(f"  {member.outline_line}")
        if len(lines) > MAX_OUTLINE_MEMBERS:
            lines = lines[:MAX_OUTLINE_MEMBERS] + [
                f"  // ... {len(members) - MAX_OUTLINE_MEMBERS} more member(s)"]
        return f"{decl.signature} {{\n" + '\n'.join(lines) + "\n}"

    # ------------------------------------------------------------------ #
    # 1. Enclosing class / same file
    # ------------------------------------------------------------------ #

    def _enclosing_class(self, issue: IssueRecord) -> List[ContextChunk]:
        outline = self.ast.outline(issue.file_path)
        if outline is None:
            return []
        decl = outline.enclosing_type(issue.snippet_start_line, issue.snippet_end_line)
        if decl is None:
            return []
        body = self._outline_body(outline, decl,
                                  skip=(issue.snippet_start_line, issue.snippet_end_line))
        header = (f"{issue.file_path}, {_lines_label(decl.start_line, decl.end_line)}: "
                  f"enclosing {decl.kind} {decl.name} (outline)")
        return [ContextChunk(text=_fenced(issue, header, body), method='enclosing_class',
                             source_path=issue.file_path, start_line=decl.start_line,
                             end_line=decl.end_line, symbol=decl.name, summary=True)]

    def _same_file(self, issue: IssueRecord) -> List[ContextChunk]:
        lines = self._file_lines(issue.file_path)
        if not lines:
            return []
        start = max(1, issue.snippet_start_line - SAME_FILE_WINDOW_LINES)
        end = min(len(lines), issue.snippet_end_line + SAME_FILE_WINDOW_LINES)
        comment = '#' if fence_language(issue.file_path) == 'yaml' else '//'
        indent = re.match(r'\s*', issue.target_snippet).group(0)
        body = (lines[start - 1:issue.snippet_start_line - 1]
                + [f"{indent}{comment} ... (the code to refactor goes here) ..."]
                + lines[issue.snippet_end_line:end])
        header = f"{issue.file_path}, {_lines_label(start, end)}: rest of the same file"
        return [ContextChunk(text=_fenced(issue, header, '\n'.join(body)), method='same_file',
                             source_path=issue.file_path, start_line=start, end_line=end,
                             summary=True)]

    # ------------------------------------------------------------------ #
    # 3. Vector search
    # ------------------------------------------------------------------ #

    def _vector_hits(self, issue: IssueRecord, rule: RuleInfo) -> List[ContextChunk]:
        query = f"{rule.name}\n{issue.flagged_code}\n{issue.target_snippet}"
        try:
            hits = self.vector.query(query, issue, VECTOR_TOP_K + VECTOR_OVERFETCH)
        except Exception as e:  # a failed lookup should not stop the run
            logger.warning("Vector query failed for %s: %s", issue.issue_key, e)
            return []
        chunks = []
        for hit in hits:
            if hit['score'] < MIN_VECTOR_SCORE:
                continue
            meta = hit['metadata']
            label = meta.get('symbol') or meta.get('kind', '')
            header = (f"{meta['file_path']}, {_lines_label(meta['start_line'], meta['end_line'])}"
                      f": {label} (similar code, score {hit['score']:.2f})")
            chunks.append(ContextChunk(
                text=_fenced(issue, header, hit['document']), method='vector',
                source_path=meta['file_path'], start_line=meta['start_line'],
                end_line=meta['end_line'], score=round(hit['score'], 4), symbol=label,
                summary=meta.get('kind') == 'type_outline'))
            if len(chunks) == VECTOR_TOP_K:
                break
        return chunks

