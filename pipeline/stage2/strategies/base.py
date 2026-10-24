from typing import List, Protocol

from ..models import ContextChunk, IssueRecord, RuleInfo


class StrategySkipped(Exception):
    """The strategy cannot produce a meaningful prompt for this issue."""


class ContextStrategy(Protocol):
    name: str

    def build(self, issue: IssueRecord, rule: RuleInfo) -> List[ContextChunk]:
        """Return the context chunks for the issue, or raise StrategySkipped."""
        ...
