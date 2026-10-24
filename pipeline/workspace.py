"""
Where each stage reads and writes for one target repository.

Every target repository gets its own run folder (by default
pipeline/runs/<repo folder name>/), so the target repository stays untouched
and several repositories can be processed side by side:

    stage1_issues.json      SonarQube issues               (fetch-issues)
    stage1_snippets.json    target snippets                (extract)
    chroma_db/              vector index of the repo       (index)
    cache/sonar_rules.json  rule descriptions              (prompts)
    stage2_prompts.json     prompts per strategy           (prompts)
    stage2_manifest.json    run metadata                   (prompts)
    stage3_results_<model>.json  refactored code per strategy, one file per
                                 model so providers can be compared (refactor)
    stage3_applied_<model>.json  which patches went onto which branch (apply)
    stage3_applied_<model>_<strategy>_in_place.json
                                 same, for apply --strategy (working tree)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PIPELINE_DIR = Path(__file__).resolve().parent


def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)


DEFAULT_RUNS_DIR = PIPELINE_DIR / 'runs'


@dataclass(frozen=True)
class Workspace:
    repo_root: Path
    workdir: Path

    @classmethod
    def create(cls, repo: str, workdir: Optional[str] = None) -> 'Workspace':
        repo_root = Path(repo).expanduser().resolve()
        if not repo_root.is_dir():
            raise SystemExit(f"Repository path is not a directory: {repo_root}")
        run_dir = (Path(workdir).expanduser().resolve() if workdir
                   else DEFAULT_RUNS_DIR / repo_root.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(repo_root, run_dir)

    @property
    def issues_file(self) -> Path:
        return self.workdir / 'stage1_issues.json'

    def branch_issues_file(self, branch: str) -> Path:
        """Issues of a refactored branch, kept apart from the baseline issues."""
        return self.workdir / f'stage1_issues_{_safe(branch)}.json'

    @property
    def snippets_file(self) -> Path:
        return self.workdir / 'stage1_snippets.json'

    @property
    def chroma_path(self) -> Path:
        return self.workdir / 'chroma_db'

    @property
    def rule_cache_file(self) -> Path:
        return self.workdir / 'cache' / 'sonar_rules.json'

    @property
    def prompts_file(self) -> Path:
        return self.workdir / 'stage2_prompts.json'

    @property
    def manifest_file(self) -> Path:
        return self.workdir / 'stage2_manifest.json'

    def results_file(self, model: str) -> Path:
        return self.workdir / f'stage3_results_{_safe(model)}.json'

    def applied_file(self, model: str, strategy: Optional[str] = None) -> Path:
        """Report for the branches, or for one strategy applied to the working tree."""
        suffix = f'_{strategy}_in_place' if strategy else ''
        return self.workdir / f'stage3_applied_{_safe(model)}{suffix}.json'

    @property
    def excluded_paths(self) -> tuple:
        """Paths never indexed as repository code: the pipeline and its outputs."""
        return (PIPELINE_DIR, self.workdir)
