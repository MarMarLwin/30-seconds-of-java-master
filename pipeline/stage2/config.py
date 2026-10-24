"""Stage 2 tuning defaults (retrieval budget, thresholds, index settings)."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# Curated data shipped with the pipeline. File locations for each run are in
# pipeline/workspace.py.
FEW_SHOT_EXAMPLES_FILE = PACKAGE_DIR / 'data' / 'few_shot_examples.yaml'
RULE_DESCRIPTIONS_FILE = PACKAGE_DIR / 'data' / 'rule_descriptions.yaml'

# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

STRATEGIES = ['zero_shot', 'few_shot', 'repository_aware']

# --------------------------------------------------------------------------- #
# Repository-aware retrieval
# --------------------------------------------------------------------------- #

# Maximum size of the repository-aware context section, in approximate tokens
# (characters / 4). Chunks are added in priority order until it is full.
DEFAULT_CONTEXT_BUDGET_TOKENS = 1500

# A symbol that matches more declarations than this is too ambiguous to be
# worth including (e.g. "build" matches every widget).
MAX_SYMBOL_MATCHES = 3

# Method or function bodies longer than this are shown as a signature only.
MAX_INLINE_BODY_LINES = 25

# Vector hits below this cosine similarity are dropped as noise.
MIN_VECTOR_SCORE = 0.45
VECTOR_TOP_K = 3
VECTOR_OVERFETCH = 6

# --------------------------------------------------------------------------- #
# Vector index
# --------------------------------------------------------------------------- #

COLLECTION_NAME = 'repo_code'
DEFAULT_EMBEDDING_MODEL = 'all-MiniLM-L6-v2'

# Files with these extensions are indexed.
INDEX_EXTENSIONS = ('.dart', '.yaml', '.yml', '.js', '.ts', '.java', '.kt', '.swift', '.gradle')

# Directories that hold build output, dependencies or tooling, not source code.
# The pipeline itself and the run folder are excluded by path (see
# Workspace.excluded_paths).
EXCLUDED_DIRS = {
    '.git', '.dart_tool', '.idea', '.vscode', '.scannerwork', 'build',
    'node_modules', 'Pods', '.gradle', '__pycache__', '.venv', 'venv',
}

# Generated files add noise to retrieval.
EXCLUDED_SUFFIXES = ('.g.dart', '.freezed.dart', '.mocks.dart')

# Non-Dart files (and Dart members too long to embed whole) are split into
# overlapping line windows.
CHUNK_LINES = 40
CHUNK_OVERLAP = 10
MAX_MEMBER_CHUNK_LINES = 80

# ChromaDB rejects very large single writes.
WRITE_BATCH_SIZE = 500
