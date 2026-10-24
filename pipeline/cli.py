"""
LLM refactoring pipeline for any repository analysed by SonarCloud.

    python -m pipeline <command> --repo <path to target repo> [options]

Commands, in pipeline order:

    fetch-issues  Stage 1  download the project's open SonarCloud issues
    extract       Stage 1  turn the issues into target code snippets
    index         Stage 2  index the repository into a vector database
    prompts       Stage 2  build zero-shot, few-shot and repository-aware prompts
    inspect       Stage 2  review the context placed in each prompt
    refactor      Stage 3  send the prompts to an LLM, then apply the code
    apply         Stage 3  write refactored code into the source (--strategy) or
                           into one git branch per strategy
    run                    all of the above (except inspect) in order

Outputs go to pipeline/runs/<repo folder name>/ unless --workdir is given.
Run `python -m pipeline <command> --help` for each command's options.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .common.env import load_dotenv
from .common.sonar_project import read_sonar_properties
from .stage2 import config as stage2_config
from .workspace import Workspace

logger = logging.getLogger('pipeline')

PROVIDER_CHOICES = ['gemini', 'openai', 'claude', 'claude-code']   # see stage3/providers/
EFFORT_CHOICES = ['low', 'medium', 'high', 'xhigh', 'max']


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_fetch_issues(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage1.fetch import fetch_issues
    props = read_sonar_properties(ws.repo_root)
    project = args.project or props.get('sonar.projectKey')
    if not project:
        raise SystemExit("No SonarCloud project key: pass --project or set sonar.projectKey "
                         f"in {ws.repo_root / 'sonar-project.properties'}")
    branch = getattr(args, 'branch', None)
    pull_request = getattr(args, 'pull_request', None)
    if branch or pull_request:
        output = ws.branch_issues_file(branch or f'pr-{pull_request}')
    else:
        output = ws.issues_file
    fetch_issues(project, args.organization or props.get('sonar.organization'), output,
                 branch=branch, pull_request=pull_request)


def cmd_extract(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage1.extract import extract_snippets
    issues = Path(args.issues) if args.issues else ws.issues_file
    if not issues.is_file():
        raise SystemExit(f"{issues} not found; run `python -m pipeline fetch-issues` "
                         "or pass --issues <file exported from SonarQube>")
    extract_snippets(issues, ws.repo_root, ws.snippets_file, args.scope)


def cmd_index(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage2.commands import build_index
    build_index(ws, args.embedding_model)


def cmd_prompts(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage2.commands import generate_prompts
    generate_prompts(ws, budget=args.budget, skip_rag=args.skip_rag,
                     no_vector=args.no_vector, offline=args.offline,
                     refresh_rules=args.refresh_rules, organization=args.organization,
                     embedding_model=args.embedding_model)


def cmd_inspect(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage2.commands import inspect_prompts
    inspect_prompts(ws, args.issue, args.show)


def cmd_refactor(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage3.refactor import run_refactoring
    if not ws.prompts_file.is_file():
        raise SystemExit(f"{ws.prompts_file} not found; run `python -m pipeline prompts` first")
    model = run_refactoring([ws.prompts_file], ws.results_file, args.provider, args.model,
                            args.temperature, args.limit, args.fresh, args.effort)
    if not args.no_apply:
        _apply(ws, model, args.strategy)


def cmd_apply(ws: Workspace, args: argparse.Namespace) -> None:
    from .stage3.providers import default_model
    _apply(ws, args.model or default_model(args.provider), args.strategy)


def _apply(ws: Workspace, model: str, strategy: Optional[str]) -> None:
    from .stage3.apply import ApplyError, apply_in_place, apply_results
    try:
        if strategy:
            logger.info("Applying %s %s results to the working tree", model, strategy)
            report = apply_in_place(ws.repo_root, ws.results_file(model), model, strategy,
                                    ws.applied_file(model, strategy))
            logger.info("%d change(s) written to the source files (uncommitted). Review "
                        "with `git diff`; undo with `git restore <file>`.", report['applied'])
            return
        logger.info("Applying %s results to one branch per strategy", model)
        report = apply_results(ws.repo_root, ws.results_file(model), model,
                               ws.applied_file(model))
    except ApplyError as e:
        raise SystemExit(f"Apply failed: {e}")
    for name, info in report['branches'].items():
        logger.info("%-17s -> %s (%d applied)", name, info['branch'], info['applied'])


def cmd_run(ws: Workspace, args: argparse.Namespace) -> None:
    steps = []
    if args.fetch or (not args.issues and not ws.issues_file.is_file()):
        steps.append(('Stage 1: fetch issues', cmd_fetch_issues))
    steps.append(('Stage 1: extract snippets', cmd_extract))
    if not args.skip_rag and not args.no_vector:
        steps.append(('Stage 2: build index', cmd_index))
    steps.append(('Stage 2: generate prompts', cmd_prompts))
    if not args.no_refactor:
        steps.append(('Stage 3: refactor', cmd_refactor))

    for title, step in steps:
        logger.info("===== %s =====", title)
        step(ws, args)
    logger.info("Done. Outputs are in %s", ws.workdir)


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #

def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('--repo', default='.',
                   help="Target repository root (default: current directory)")
    p.add_argument('--workdir',
                   help="Folder for this run's outputs "
                        "(default: pipeline/runs/<repo folder name>)")
    p.add_argument('-v', '--verbose', action='store_true', help="Enable debug logging")
    return p


def _add_fetch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--project', help="SonarCloud project key (default: sonar.projectKey)")
    p.add_argument('--organization',
                   help="SonarCloud organization (default: sonar.organization)")


def _add_extract_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--issues',
                   help="Issues JSON to use instead of <workdir>/stage1_issues.json, e.g. "
                        "one exported with the sonar CLI")
    p.add_argument('--scope', choices=['ast', 'lines'], default='ast',
                   help="ast: widen each snippet to its enclosing syntax node (default); "
                        "lines: a fixed 3-line buffer")


def _add_index_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--embedding-model', default=stage2_config.DEFAULT_EMBEDDING_MODEL,
                   help="sentence-transformers model; index and prompts must use the "
                        f"same one (default: {stage2_config.DEFAULT_EMBEDDING_MODEL})")


def _add_prompt_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--budget', type=int, default=stage2_config.DEFAULT_CONTEXT_BUDGET_TOKENS,
                   help="Repository-aware context budget in approximate tokens "
                        f"(default: {stage2_config.DEFAULT_CONTEXT_BUDGET_TOKENS})")
    p.add_argument('--skip-rag', action='store_true',
                   help="Only generate zero-shot and few-shot prompts")
    p.add_argument('--no-vector', action='store_true',
                   help="Repository-aware context from the AST only (no vector index)")
    p.add_argument('--offline', action='store_true',
                   help="Do not contact SonarCloud for rule descriptions")
    p.add_argument('--refresh-rules', action='store_true',
                   help="Ignore cached rule descriptions and fetch them again")


def _add_refactor_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--provider', choices=PROVIDER_CHOICES, default='gemini',
                   help="LLM provider: gemini (GEMINI_API_KEY), openai (OPENAI_API_KEY), "
                        "claude (ANTHROPIC_API_KEY, API credits) or claude-code (your "
                        "Claude Code login, e.g. a Pro plan) (default: gemini)")
    p.add_argument('--model',
                   help="Model name (default: the provider's default, e.g. gemini-3.8-flash "
                        "or claude-opus-5-5; for claude-code an alias such as sonnet or "
                        "opus, default: the plan's default model)")
    p.add_argument('--temperature', type=float,
                   help="Sampling temperature for gemini/openai (default: 0.0); "
                        "not supported by Claude")
    p.add_argument('--effort', choices=EFFORT_CHOICES,
                   help="claude / claude-code only: reasoning effort (default: high)")
    p.add_argument('--limit', type=int,
                   help="Only process the first N issues (useful for a trial run)")
    p.add_argument('--fresh', action='store_true',
                   help="Ignore earlier results and redo every call")
    p.add_argument('--no-apply', action='store_true',
                   help="Only write the results JSON; do not touch the source code")
    _add_strategy_arg(p)


def _add_strategy_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument('--strategy', choices=stage2_config.STRATEGIES,
                   help="Write this strategy's code straight into the current source files "
                        "(uncommitted). Without it, one git branch per strategy is created.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m pipeline', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True, metavar='<command>')
    common = [_common()]

    p = sub.add_parser('fetch-issues', parents=common,
                       help="Stage 1: download open issues from SonarCloud")
    _add_fetch_args(p)
    p.add_argument('--branch',
                   help="Read this branch's analysis instead of the main branch's (e.g. a "
                        "refactored branch); saved as stage1_issues_<branch>.json")
    p.add_argument('--pull-request', help="Read this pull request's analysis instead")
    p.set_defaults(func=cmd_fetch_issues)

    p = sub.add_parser('extract', parents=common, help="Stage 1: issues -> target snippets")
    _add_extract_args(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser('index', parents=common,
                       help="Stage 2: index the repository into the vector database")
    _add_index_args(p)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser('prompts', parents=common, help="Stage 2: build the prompts")
    _add_prompt_args(p)
    _add_index_args(p)
    p.add_argument('--organization',
                   help="SonarCloud organization for rule descriptions "
                        "(default: sonar.organization)")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser('inspect', parents=common,
                       help="Stage 2: summarise the context in each prompt")
    p.add_argument('--issue', help="Only this issue (issue key, key prefix, or rule)")
    p.add_argument('--show', choices=stage2_config.STRATEGIES,
                   help="Also print the full prompt text for this strategy")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser('refactor', parents=common, help="Stage 3: run the prompts on an LLM")
    _add_refactor_args(p)
    p.set_defaults(func=cmd_refactor)

    p = sub.add_parser('apply', parents=common,
                       help="Stage 3: write refactored code into one git branch per strategy")
    p.add_argument('--provider', choices=PROVIDER_CHOICES, default='gemini',
                   help="Provider whose results to apply (default: gemini)")
    p.add_argument('--model', help="Model label of the results file "
                                   "(default: the provider's default model)")
    _add_strategy_arg(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser('run', parents=common, help="Run every stage in order")
    _add_fetch_args(p)
    _add_extract_args(p)
    _add_index_args(p)
    _add_prompt_args(p)
    _add_refactor_args(p)
    p.add_argument('--fetch', action='store_true',
                   help="Download issues again even if stage1_issues.json exists")
    p.add_argument('--no-refactor', action='store_true',
                   help="Stop after Stage 2 (no LLM calls)")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(levelname)s: %(message)s')
    # The SDK's HTTP client logs every request at INFO, which buries the
    # pipeline's own logs. Newer openai SDK versions use httpx2.
    for name in ('httpx', 'httpx2'):
        logging.getLogger(name).setLevel(logging.WARNING)
    load_dotenv(Path.cwd() / '.env')

    ws = Workspace.create(args.repo, args.workdir)
    logger.info("Repository: %s", ws.repo_root)
    logger.info("Run folder: %s", ws.workdir)
    try:
        args.func(ws, args)
    except KeyboardInterrupt:
        sys.exit(130)
