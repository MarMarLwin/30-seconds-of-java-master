from typing import List

from ..models import ContextChunk, IssueRecord, RuleInfo


class ZeroShotStrategy:
    """Snippet and rule description only: the baseline."""

    name = 'zero_shot'

    def build(self, issue: IssueRecord, rule: RuleInfo) -> List[ContextChunk]:
        return []
