# LLM refactoring pipeline

Fixes SonarQube issues in a repository with an LLM under three context
strategies (zero-shot, few-shot, repository-aware RAG), so the strategies can
be compared. The pipeline is self-contained in this folder and runs against
any local repository that SonarCloud has analysed.

| Stage | Command | What it does | Output (in the run folder) |
|---|---|---|---|
| 1 | `fetch-issues` | download the project's open SonarCloud issues | `stage1_issues.json` |
| 1 | `extract` | cut a target snippet for each issue, scoped to the enclosing AST node | `stage1_snippets.json` |
| 2 | `index` | index the repository's code into a vector database | `chroma_db/` |
| 2 | `prompts` | build the three prompts per issue | `stage2_prompts.json`, `stage2_manifest.json` |
| 2 | `inspect` | print what context each prompt contains | (console) |
| 3 | `refactor` | send every prompt to the LLM, extract the code, then apply it | `stage3_results_<model>.json` + branches |
| 3 | `apply` | write the refactored code into one git branch per strategy (no LLM calls) | `refactor/<model>/<strategy>` branches, `stage3_applied_<model>.json` |
| | `run` | `fetch-issues` (if needed) → `extract` → `index` → `prompts` → `refactor` | all of the above |

Stages 4 (build and test runner) and 5 (evaluation matrix) are not implemented yet.

## How to run

### 1. One-time setup

Requirements: Python 3.9+, and the Dart SDK if the target repository is a
Dart/Flutter project (used to parse Dart code; without it the pipeline still
runs with simpler snippet scoping and context).

```sh
cd <this repository>
pip install -r pipeline/requirements.txt
```

Put the LLM API key in a `.env` file in the directory you run the commands
from, or export it:

```sh
GEMINI_API_KEY=...        # default provider (https://aistudio.google.com/apikey)
ANTHROPIC_API_KEY=...     # only for --provider claude (https://platform.claude.com)
OPENAI_API_KEY=...        # only for --provider openai
SONAR_TOKEN=...           # optional: private SonarCloud projects, and official
                          # rule descriptions (see "Rule descriptions" below)
```

### 2. Run it on a repository

Always run from the folder that contains `pipeline/`, and point `--repo` at
the target repository. Every command takes `--repo` (default: the current
directory) and `--workdir` (default: `pipeline/runs/<repo folder name>/`).

```sh
# This repository
python3 -m pipeline run --repo . --no-refactor      # Stages 1-2 only, no LLM cost
python3 -m pipeline inspect --repo .                # check the context in each prompt
python3 -m pipeline refactor --repo . --limit 1     # Stage 3 trial: 1 issue, 3 calls
python3 -m pipeline refactor --repo .               # Stage 3: all issues

# Any other repository analysed by SonarCloud
python3 -m pipeline run --repo ~/code/other-app --no-refactor
python3 -m pipeline refactor --repo ~/code/other-app
```

Or step by step:

```sh
python3 -m pipeline fetch-issues --repo .   # needs sonar.projectKey / sonar.organization
python3 -m pipeline extract      --repo .
python3 -m pipeline index        --repo .   # again whenever the code changes
python3 -m pipeline prompts      --repo .
python3 -m pipeline inspect      --repo . --issue dart:S7115 --show repository_aware
python3 -m pipeline refactor     --repo . --limit 1
```

Useful options (see `python3 -m pipeline <command> --help`):

| Option | Command | Meaning |
|---|---|---|
| `--project KEY --organization ORG` | `fetch-issues`, `run` | when the repo has no `sonar-project.properties` |
| `--issues FILE` | `extract`, `run` | use an issues JSON exported elsewhere (e.g. `sonar list issues ... > file`) instead of fetching |
| `--scope lines` | `extract`, `run` | fixed ±3 line snippets instead of AST scoping (ablation) |
| `--skip-rag` / `--no-vector` | `prompts`, `run` | no repository-aware prompt / AST context only |
| `--budget N` | `prompts`, `run` | repository-aware context size in approx. tokens (default 1500) |
| `--provider claude` | `refactor`, `run` | Claude through the API (credits; default `claude-opus-5-5`) |
| `--provider claude-code` | `refactor`, `run` | Claude through your Claude Code login (e.g. Pro plan) |
| `--effort low…max` | `refactor`, `run` | Claude reasoning effort (default `high`) |
| `--provider openai --model gpt-4o` | `refactor`, `run` | another LLM |
| `--fresh` | `refactor`, `run` | redo every LLM call instead of reusing earlier results |

Stage 3 saves after every issue and skips calls that already succeeded with
the same model, settings and prompt, so an interrupted run resumes where it
stopped and regenerating the prompts re-runs only the calls whose prompts
changed.

### Applying the refactorings to the source code

After the LLM calls, `refactor` writes the code back into the repository as
one local git branch per strategy (skip with `--no-apply`; re-run with
`apply --provider <p> [--model <m>]` without new LLM calls):

```
<current branch>                      baseline
 ├─ refactor/<model>/zero-shot        only zero-shot patches
 ├─ refactor/<model>/few-shot         only few-shot patches
 └─ refactor/<model>/repository-aware only repository-aware patches
```

- Each branch starts from the current commit and gets one commit, made in a
  temporary git worktree: your checkout and uncommitted changes are never
  touched. **Commit your work first**, since branches start from `HEAD`.
- A patch replaces `snippet_start_line`..`snippet_end_line`. It is skipped if
  the call failed, or if those lines no longer match the snippet sent to the
  model (file changed or not committed).
- Overlapping patches: the larger one is applied (e.g. a whole-method S3776
  rewrite) and the ones inside it are recorded as `conflict`. SonarCloud
  still re-checks those issues on the branch.
- Re-applying replaces a pipeline branch, but refuses if the branch has
  commits you made yourself.
- Branches are local. Push them (`git push origin refactor/<model>/<strategy>`)
  for SonarCloud to analyse each one, then compare issues per branch.
- `stage3_applied_<model>.json` lists, per branch, every issue as `applied`,
  `conflict` or `skipped` with the reason, plus the base and branch commits.

### Measuring the refactored code

`fetch-issues` reads SonarCloud's latest analysis, so the issue count only
changes after SonarCloud has analysed the refactored code:

1. **Let CI analyse feature branches.** The workflow must run on pushes to
   the strategy branches, not only to `main`. Commit this to the baseline
   branch *before* creating the strategy branches, so they all inherit it:

   ```yaml
   on:
     push:
       branches:
         - main
         - 'refactor/**'
         - 'feat/**'
     pull_request:
       types: [opened, synchronize, reopened]
   ```

2. **Commit and push the refactored code.** Strategy branches from `apply`
   are already committed: `git push origin refactor/<model>/<strategy>`.
   Code applied with `apply --strategy` is uncommitted: commit it first.
3. **Wait for the SonarCloud job** in GitHub Actions to finish.
4. **Fetch that branch's issues** (saved next to the baseline, which is not
   overwritten):

   ```sh
   python3 -m pipeline fetch-issues --repo . --branch refactor/<model>/<strategy>
   # -> stage1_issues_refactor_<model>_<strategy>.json
   ```

   `--pull-request <number>` reads a pull request's analysis instead, but
   SonarCloud reports only *new* issues on pull requests, so use branches to
   compare full issue counts.

### LLM providers (Stage 3)

| `--provider` | Credentials | Default model | Notes |
|---|---|---|---|
| `gemini` (default) | `GEMINI_API_KEY` | `gemini-3.8-flash` | temperature 0, low reasoning effort |
| `claude` | `ANTHROPIC_API_KEY` (API credits) | `claude-opus-5-5` | see below |
| `claude-code` | your Claude Code login (e.g. a Pro plan), no API key | the plan's default | see below |
| `openai` | `OPENAI_API_KEY` | `gpt-4o` | temperature 0 |

Every provider gets the same system prompt and user prompts. With Claude:

- **No temperature.** Current Claude models don't accept sampling parameters,
  so runs are not exactly repeatable. Reasoning depth is set with `--effort`
  (default `high`). Thinking is always on with Opus 5.5, and its tokens are
  counted in `completion_tokens`.
- **Refusal fallbacks are on** (`fallbacks: "default"`) for Opus 5.5, Opus 5,
  Fable 5.1 and Sonnet 5.5: if the model declines a request, the API re-runs
  it on a fallback model. Each result records `served_by` and `fallback_used`,
  so you can exclude or separately report answers that did not come from the
  requested model.
- A declined request (`finish_reason: refusal`) is recorded as an error with
  its category.

`claude-code` runs each prompt through the installed Claude Code CLI
(`claude -p`), billed to the subscription it is logged in with, so no API
credits are needed. Each call is a fresh session with Stage 3's system prompt
in place of Claude Code's, no tools, `--safe-mode` (no CLAUDE.md, skills,
plugins, hooks or MCP servers) and an empty working directory.
`ANTHROPIC_API_KEY` is removed from its environment, because Claude Code bills
to an API key whenever one is set. Notes:

- Pass `--model sonnet` or `--model opus` (whatever the plan offers) for
  reproducible experiments; without it the plan's default model is used. The
  results file is named after `--model` (`stage3_results_opus.json`), or
  `stage3_results_claude-code.json` without it, and each result records the
  exact model in `served_by`.
- The plan's usage limits apply. When a limit is hit the run stops; re-run the
  same command later and it resumes where it stopped.
- `equivalent_api_cost_usd` is what the call would cost on the API; it is not
  charged on a subscription. Claude Code can also make small side calls (e.g.
  on Haiku), listed in `model_usage` but not counted in `prompt_tokens` /
  `completion_tokens`.
- Results are less controlled than direct API calls (Claude Code still adds
  its own request framing), so report them as obtained through Claude Code.

Every result records `prompt_tokens`, `completion_tokens`, `total_tokens`, the
provider's full `usage` object (cache and reasoning token detail), `model`,
`settings` and `finish_reason`. Each model writes its own results file in
the run folder (`stage3_results_gemini-3.8-flash.json`,
`stage3_results_claude-opus-5-5.json`, ...), so running a second provider on
the same prompts never overwrites the first one's results.

### 3. Adapting to a new repository or language

| What | Works out of the box | Add for better results |
|---|---|---|
| Issues | any SonarCloud project | |
| Snippet scope | Dart (AST), GitHub Actions steps; other files get ±3 lines | a scoping rule in `stage1/scoping.py` |
| Rule descriptions | rule name from SonarCloud | `SONAR_TOKEN` (official text) or `stage2/data/rule_descriptions.yaml` |
| Few-shot examples | none: the few-shot prompt is skipped for that rule | examples in `stage2/data/few_shot_examples.yaml` |
| Repository context | same-file context and vector search for any language; Dart also gets the enclosing class and imported declarations | |

## Stage 2 design

```
stage1_snippets.json ───────────┐
SonarCloud rules (cached) ──────┼─► IssueRecord + RuleInfo
                                │
                ┌───────────────┼──────────────────┐
                ▼               ▼                  ▼
           ZeroShot          FewShot          RepoAware
           (nothing)    few_shot_examples   1. enclosing class / rest of file
                             .yaml          2. imported declarations (AST + imports)
                                            3. similar code (vector search)
                └───────────────┼──────────────────┘
                                ▼
                template.render_prompt()   ← one template for every strategy
                                ▼
                     stage2_prompts.json
```

**Controlled comparison.** All prompts for an issue come from one template
(`stage2/template.py`); only the `## Context` section differs, so result
differences can be attributed to the context. `tests/test_template.py`
enforces this. Stage 3 adds the same system prompt to every call.

**Snippet scope.** SonarQube's `textRange` marks only the flagged token (for
S3776, just a method name). `stage1/scoping.py` widens it to the enclosing
syntax node that suits the rule:

| Rule | Scope |
|---|---|
| `dart:S3776` cognitive complexity | whole method |
| `dart:S7115` context across async gaps | innermost closure (or method) |
| `dart:S3358` nested ternary | innermost named argument or statement |
| `dart:S7055` wildcard identifier | catch clause |
| other Dart rules | innermost statement |
| `githubactions:*` | the workflow step |
| anything else | ±3 lines |

The scope becomes `snippet_start_line`/`snippet_end_line`, which later stages
use to patch the file.

**Rule descriptions** (`stage2/rules.py`). Fetched from SonarCloud and cached
in `cache/sonar_rules.json`. SonarCloud returns the description body only to
an authenticated caller, so without `SONAR_TOKEN` the curated summaries in
`stage2/data/rule_descriptions.yaml` are used. Each prompt record states the
source in `rule_info.description_source` (`sonarcloud`, `curated` or
`name_only`). After setting a token, run `prompts --refresh-rules`.

**Few-shot examples** (`stage2/data/few_shot_examples.yaml`). Keyed by rule
and written from scratch: never copy code from the target repository, or the
example leaks the answer. A rule without examples gets no few-shot prompt
(listed under `skipped_strategies`) rather than a duplicate of zero-shot.

**Repository-aware retrieval** (`stage2/strategies/repo_aware.py`). Candidates
are added in priority order until the budget is full:

1. `enclosing_class`: outline (fields and member signatures) of the class
   around the snippet; for non-Dart files, `same_file`: the rest of the file.
2. `imported_declaration`: declarations of the symbols the snippet uses, in
   the files it imports. A bare name matches top-level declarations, `x.name`
   matches members, `Type.name` only members of `Type`; names with more than
   3 matches are dropped as ambiguous.
3. `vector`: the most similar chunks in the same language (cosine ≥ 0.45, at
   most 3).

Each chunk records its method, file, lines, symbol and score in
`prompts.<strategy>.context`, so retrieval quality can be analysed with the
results.

**Dart AST** (`tools/dart_ast`, called by `common/dart_ast.py`). A small Dart
package that parses files with `package:analyzer` (syntax only) and prints
imports, declarations and scope nodes as JSON.

**Vector index** (`stage2/retrieval/vector_index.py`). Dart files are chunked
along the AST (one chunk per method or declaration, plus one outline per
type); other files use 40-line windows. The pipeline folder and run folder
are never indexed. The index records its embedding model and git commit, and
prompts refuse an index built with a different model.

**Reproducibility.** `stage2_manifest.json` records the git commit,
uncommitted files, settings and index metadata. Each prompt has a `sha256`,
which Stage 3 stores with its result.

## Layout

```
pipeline/
  cli.py                    `python -m pipeline` commands
  workspace.py              run folder and file names per repository
  requirements.txt
  common/                   dart_ast.py, sonar_project.py, env.py (shared helpers)
  stage1/                   fetch.py, extract.py, scoping.py
  stage2/
    commands.py             index / prompts / inspect
    config.py               budget, thresholds, index settings
    models.py               IssueRecord, RuleInfo, ContextChunk
    rules.py                rule descriptions + cache
    template.py             the shared prompt template
    generator.py            runs the strategies, writes prompts and manifest
    strategies/             zero_shot.py, few_shot.py, repo_aware.py
    retrieval/              import_graph.py, vector_index.py
    data/                   few_shot_examples.yaml, rule_descriptions.yaml
  stage3/refactor.py        prompts → results, resumable
  stage3/apply.py           results → one git branch per strategy
  stage3/providers/         openai_compat.py (Gemini, OpenAI), claude.py (API),
                            claude_code.py (Claude Code CLI / subscription)
  tools/dart_ast/           Dart AST dumper
  tests/                    python3 -m unittest discover -s pipeline/tests -t .
  runs/<repo>/              outputs, one folder per target repository
```
