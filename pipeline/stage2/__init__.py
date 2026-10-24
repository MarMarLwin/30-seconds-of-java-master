"""
Stage 2: context retrieval and prompt generation.

For every SonarQube issue extracted in Stage 1, build one prompt per context
strategy. All prompts share a single template (see template.py) and differ
only in their "## Context" section, so differences in Stage 3/5 results can be
attributed to the context alone:

    zero_shot         no context
    few_shot          curated before/after examples for the rule
    repository_aware  code from this repository, retrieved via the Dart AST
                      (imported declarations, enclosing class) and vector search
"""
