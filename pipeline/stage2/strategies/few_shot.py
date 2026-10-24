from pathlib import Path
from typing import Dict, List

import yaml

from ..config import FEW_SHOT_EXAMPLES_FILE
from ..models import ContextChunk, IssueRecord, RuleInfo, fence_language
from .base import StrategySkipped


def load_examples(path: Path) -> Dict[str, List[Dict[str, str]]]:
    with path.open('r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    for rule, examples in data.items():
        for example in examples:
            missing = {'title', 'before', 'after'} - example.keys()
            if missing:
                raise ValueError(f"{path}: example for {rule} is missing {sorted(missing)}")
    return data


class FewShotStrategy:
    """Curated before/after examples for the issue's rule."""

    name = 'few_shot'

    def __init__(self, examples_path: Path = FEW_SHOT_EXAMPLES_FILE):
        self.examples_path = examples_path
        self.examples = load_examples(examples_path)

    def build(self, issue: IssueRecord, rule: RuleInfo) -> List[ContextChunk]:
        examples = self.examples.get(issue.rule)
        if not examples:
            raise StrategySkipped(f"no curated example for {issue.rule} in "
                                  f"{self.examples_path.name}")
        fence = fence_language(issue.file_path)
        return [
            ContextChunk(
                text=(f"Example {i}: {ex['title']}\n"
                      f"Before:\n```{fence}\n{ex['before'].rstrip()}\n```\n"
                      f"After:\n```{fence}\n{ex['after'].rstrip()}\n```"),
                method='few_shot',
                source_path=self.examples_path.name,
                symbol=ex['title'],
            )
            for i, ex in enumerate(examples, 1)
        ]
