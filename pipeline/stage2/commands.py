"""
Stage 2 entry points, called by the pipeline CLI (pipeline/cli.py):

    build_index()       index the target repository into ChromaDB
    generate_prompts()  write stage2_prompts.json and stage2_manifest.json
    inspect_prompts()   print a summary of the context in every prompt
"""

import json
import logging
from typing import Optional

from ..common.dart_ast import DartAstClient
from ..common.sonar_project import read_sonar_properties
from ..workspace import Workspace
from . import config
from .generator import (PromptGenerator, build_manifest, git_state, load_issue_records,
                        write_json)
from .rules import RuleCatalog
from .strategies import FewShotStrategy, RepoAwareStrategy, ZeroShotStrategy

logger = logging.getLogger(__name__)


def build_index(ws: Workspace, embedding_model: str = config.DEFAULT_EMBEDDING_MODEL) -> None:
    from .retrieval.vector_index import build_index as build
    ast = DartAstClient(ws.repo_root)
    build(ws.repo_root, ws.chroma_path, config.COLLECTION_NAME, embedding_model,
          ast if ast.available else None, git_state(ws.repo_root)['git_commit'],
          exclude=ws.excluded_paths)


def generate_prompts(ws: Workspace, budget: int = config.DEFAULT_CONTEXT_BUDGET_TOKENS,
                     skip_rag: bool = False, no_vector: bool = False,
                     offline: bool = False, refresh_rules: bool = False,
                     organization: Optional[str] = None,
                     embedding_model: str = config.DEFAULT_EMBEDDING_MODEL) -> None:
    if not ws.snippets_file.is_file():
        raise SystemExit(f"{ws.snippets_file} not found; run `python -m pipeline extract` first")
    issues = load_issue_records(ws.snippets_file)
    logger.info("Loaded %d snippet record(s) from %s", len(issues), ws.snippets_file)

    organization = organization or read_sonar_properties(ws.repo_root).get('sonar.organization')
    rules = RuleCatalog(ws.rule_cache_file, organization, offline=offline)
    if refresh_rules:
        rules.cache.clear()

    strategies = [ZeroShotStrategy(), FewShotStrategy()]
    vector = None
    if not skip_rag:
        ast = DartAstClient(ws.repo_root)
        ast.preload()
        if not no_vector:
            from .retrieval.vector_index import VectorIndex
            vector = VectorIndex(ws.chroma_path, config.COLLECTION_NAME, embedding_model)
        strategies.append(RepoAwareStrategy(ws.repo_root, ast, vector, budget))

    generator = PromptGenerator(strategies, rules)
    records = []
    for i, issue in enumerate(issues, 1):
        logger.info("[%d/%d] %s:%d (%s)", i, len(issues), issue.file_path,
                    issue.start_line, issue.rule)
        records.append(generator.generate(issue))
    rules.save()

    settings = {
        'repo': str(ws.repo_root),
        'strategies': [s.name for s in strategies],
        'context_budget_tokens': budget,
        'embedding_model': embedding_model if vector else None,
        'vector_index': vector.info if vector else None,
        'max_symbol_matches': config.MAX_SYMBOL_MATCHES,
        'min_vector_score': config.MIN_VECTOR_SCORE,
        'vector_top_k': config.VECTOR_TOP_K,
    }
    write_json(records, ws.prompts_file)
    write_json(build_manifest(records, issues, ws.repo_root, settings), ws.manifest_file)
    logger.info("Saved prompts for %d issue(s) to %s", len(records), ws.prompts_file)


def inspect_prompts(ws: Workspace, issue: Optional[str] = None,
                    show: Optional[str] = None) -> None:
    if not ws.prompts_file.is_file():
        raise SystemExit(f"{ws.prompts_file} not found; run `python -m pipeline prompts` first")
    records = json.loads(ws.prompts_file.read_text(encoding='utf-8'))

    if issue:
        records = [r for r in records if issue in (r['issue_key'], r['rule'])
                   or r['issue_key'].startswith(issue)]
        if not records:
            raise SystemExit(f"No issue matches {issue!r}")

    for record in records:
        _print_record(record, show)


def _print_record(record: dict, show: Optional[str]) -> None:
    print(f"\n=== {record['rule']}  {record['file_path']}:{record['start_line']}")
    print(f"    {record['message']}")
    print(f"    snippet lines {record['snippet_start_line']}-{record['snippet_end_line']} "
          f"(scope: {record['scope_kind']}), "
          f"rule description: {record['rule_info']['description_source']}")
    for name, prompt in record['prompts'].items():
        print(f"  {name:<17} ~{prompt['approx_tokens']} tokens "
              f"(context ~{prompt['context_tokens']})")
        for chunk in prompt['context']:
            print(f"      - {_describe_chunk(chunk)}")
    for name, reason in record.get('skipped_strategies', {}).items():
        print(f"  {name:<17} skipped: {reason}")
    if show and record['prompts'].get(show):
        print(f"\n----- {show} prompt -----\n{record['prompts'][show]['text']}")


def _describe_chunk(chunk: dict) -> str:
    where = chunk['source_path']
    if 'start_line' in chunk:
        where += f":{chunk['start_line']}-{chunk['end_line']}"
    score = f" score={chunk['score']:.2f}" if 'score' in chunk else ''
    return f"{chunk['method']:<21} {where}  {chunk.get('symbol', '')}{score}"
