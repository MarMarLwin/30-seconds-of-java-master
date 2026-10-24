"""Data passed between the Stage 2 components."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def approx_tokens(text: str) -> int:
    """Rough, model-independent token estimate (about 4 characters per token)."""
    return (len(text) + 3) // 4


def language_of(file_path: str) -> str:
    """Language label used for code fences and for grouping retrieval results."""
    path = file_path.lower()
    if path.startswith('.github/workflows/'):
        return 'github-actions'
    for ext, lang in (('.dart', 'dart'), ('.yaml', 'yaml'), ('.yml', 'yaml'),
                      ('.js', 'javascript'), ('.ts', 'typescript'),
                      ('.java', 'java'), ('.kt', 'kotlin'), ('.swift', 'swift'),
                      ('.gradle', 'groovy')):
        if path.endswith(ext):
            return lang
    return ''


def fence_language(file_path: str) -> str:
    lang = language_of(file_path)
    return 'yaml' if lang == 'github-actions' else lang


@dataclass
class IssueRecord:
    """One Stage 1 snippet record: a SonarQube issue and the code it covers."""

    issue_key: str
    rule: str
    message: str
    file_path: str
    start_line: int
    end_line: int
    snippet_start_line: int
    snippet_end_line: int
    target_snippet: str
    scope_kind: str = 'lines'

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IssueRecord':
        return cls(
            issue_key=data.get('issue_key') or
            f"{data['file_path']}:{data.get('start_line')}:{data.get('rule')}",
            rule=data.get('rule') or '',
            message=(data.get('message') or '').strip(),
            file_path=data['file_path'],
            start_line=int(data['start_line']),
            end_line=int(data.get('end_line', data['start_line'])),
            snippet_start_line=int(data.get('snippet_start_line', data['start_line'])),
            snippet_end_line=int(data.get('snippet_end_line', data['end_line'])),
            target_snippet=data['target_snippet'],
            scope_kind=data.get('scope_kind', 'lines'),
        )

    @property
    def language(self) -> str:
        return language_of(self.file_path)

    @property
    def flagged_code(self) -> str:
        """Only the lines SonarQube flagged, without the surrounding scope."""
        offset = self.start_line - self.snippet_start_line
        count = self.end_line - self.start_line + 1
        return '\n'.join(self.target_snippet.splitlines()[offset:offset + count])

    def overlaps(self, file_path: str, start_line: int, end_line: int) -> bool:
        """True if the given range intersects this issue's snippet."""
        return (file_path == self.file_path and start_line <= self.snippet_end_line
                and end_line >= self.snippet_start_line)


@dataclass
class RuleInfo:
    key: str
    name: str
    type: str = ''
    impacts: List[str] = field(default_factory=list)
    description: str = ''
    # 'sonarcloud' (official text), 'curated' (rule_descriptions.yaml) or
    # 'name_only' (nothing better was available).
    description_source: str = 'name_only'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContextChunk:
    """One piece of context placed in a prompt, with where it came from."""

    text: str
    # How the chunk was found: few_shot, imported_declaration,
    # enclosing_class, same_file or vector.
    method: str
    source_path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    symbol: Optional[str] = None
    score: Optional[float] = None
    # True when the text summarises its line range (e.g. a class outline)
    # rather than quoting it, so it does not duplicate code inside that range.
    summary: bool = False

    @property
    def approx_tokens(self) -> int:
        return approx_tokens(self.text)

    def overlaps(self, other: 'ContextChunk') -> bool:
        if self.source_path != other.source_path:
            return False
        if None in (self.start_line, self.end_line, other.start_line, other.end_line):
            return False
        return self.start_line <= other.end_line and self.end_line >= other.start_line

    def to_dict(self) -> Dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if v is not None}
        data['approx_tokens'] = self.approx_tokens
        return data
