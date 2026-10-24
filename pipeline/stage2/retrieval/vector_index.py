"""
ChromaDB index of the repository, used for the vector part of repository-aware
retrieval.

Dart files are chunked along the AST: one chunk per method, constructor,
top-level function or variable, plus a signature-only outline per type, so a
hit is a whole meaningful unit rather than an arbitrary 40-line window. Other
files fall back to overlapping line windows.
"""

import datetime
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..config import (CHUNK_LINES, CHUNK_OVERLAP, EXCLUDED_DIRS, EXCLUDED_SUFFIXES,
                      INDEX_EXTENSIONS, MAX_MEMBER_CHUNK_LINES, WRITE_BATCH_SIZE)
from ..models import IssueRecord, language_of
from ...common.dart_ast import DartAstClient, FileOutline

logger = logging.getLogger(__name__)


def _require_libraries():
    # The Dart AST tool runs as a subprocess after the tokenizer is loaded;
    # without this, Hugging Face tokenizers warns on every fork.
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        logger.error("Vector retrieval requires chromadb and sentence-transformers "
                     "(pip install chromadb sentence-transformers): %s", e)
        sys.exit(1)
    return chromadb, SentenceTransformer


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def iter_source_files(repo_root: Path, exclude: Sequence[Path] = ()) -> Iterator[Path]:
    """Yield indexable files under repo_root, skipping excluded directories."""
    for path in sorted(repo_root.rglob('*')):
        rel_parts = path.relative_to(repo_root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
            continue
        if any(path.is_relative_to(ex) for ex in exclude):
            continue
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(EXCLUDED_SUFFIXES):
            continue
        if name.endswith(INDEX_EXTENSIONS):
            yield path


def _chunk(rel_path: str, start: int, end: int, text: str, kind: str,
           symbol: str = '') -> Dict[str, Any]:
    return {
        'id': f"{rel_path}:{start}-{end}:{kind}:{symbol}",
        'document': text,
        'metadata': {'file_path': rel_path, 'start_line': start, 'end_line': end,
                     'kind': kind, 'symbol': symbol, 'language': language_of(rel_path)},
    }


def line_window_chunks(rel_path: str, lines: List[str], first_line: int = 1,
                       kind: str = 'lines', symbol: str = '') -> List[Dict[str, Any]]:
    step = CHUNK_LINES - CHUNK_OVERLAP
    chunks = []
    for offset in range(0, len(lines), step):
        window = lines[offset:offset + CHUNK_LINES]
        if '\n'.join(window).strip():
            start = first_line + offset
            chunks.append(_chunk(rel_path, start, start + len(window) - 1,
                                 '\n'.join(window), kind, symbol))
        if offset + CHUNK_LINES >= len(lines):
            break
    return chunks


def dart_chunks(rel_path: str, lines: List[str], outline: FileOutline) -> List[Dict[str, Any]]:
    chunks = []
    for decl in outline.declarations:
        if decl.is_type:
            members = outline.members_of(decl.name)
            body = '\n'.join(f"  {m.outline_line}" for m in members)
            text = f"{decl.signature} {{\n{body}\n}}"
            chunks.append(_chunk(rel_path, decl.start_line, decl.end_line, text,
                                 'type_outline', decl.name))
            continue
        if decl.kind in ('field', 'enum_constant'):
            continue  # covered by the type outline
        symbol = f"{decl.parent}.{decl.name}" if decl.parent else decl.name
        body = lines[decl.start_line - 1:decl.end_line]
        if len(body) > MAX_MEMBER_CHUNK_LINES:
            chunks.extend(line_window_chunks(rel_path, body, decl.start_line,
                                             decl.kind, symbol))
        else:
            chunks.append(_chunk(rel_path, decl.start_line, decl.end_line,
                                 '\n'.join(body), decl.kind, symbol))
    return chunks


def chunk_repository(repo_root: Path, ast: Optional[DartAstClient],
                     exclude: Sequence[Path] = ()) -> List[Dict[str, Any]]:
    files = list(iter_source_files(repo_root, exclude))
    rel_paths = [p.relative_to(repo_root).as_posix() for p in files]
    outlines = ast.outlines(rel_paths) if ast is not None else {}

    chunks: List[Dict[str, Any]] = []
    for path, rel_path in zip(files, rel_paths):
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError as e:
            logger.warning("Cannot read %s: %s", path, e)
            continue
        outline = outlines.get(rel_path)
        if outline is not None and outline.declarations:
            chunks.extend(dart_chunks(rel_path, lines, outline))
        else:
            chunks.extend(line_window_chunks(rel_path, lines))
    logger.info("Split %d file(s) into %d chunk(s)", len(files), len(chunks))
    return chunks


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def build_index(repo_root: Path, chroma_path: Path, collection_name: str, model_name: str,
                ast: Optional[DartAstClient], git_commit: Optional[str],
                exclude: Sequence[Path] = ()) -> None:
    chromadb, sentence_transformer_cls = _require_libraries()
    chunks = chunk_repository(repo_root, ast, exclude)
    if not chunks:
        logger.error("No indexable files found under %s", repo_root)
        sys.exit(1)

    logger.info("Loading embedding model %s", model_name)
    model = sentence_transformer_cls(model_name)

    client = chromadb.PersistentClient(path=str(chroma_path))
    # Rebuild from scratch so deleted or renamed files don't leave stale chunks.
    try:
        client.delete_collection(collection_name)
        logger.info("Replaced existing collection %r", collection_name)
    except Exception:
        pass  # Collection did not exist yet.
    collection = client.create_collection(
        name=collection_name,
        metadata={
            'hnsw:space': 'cosine',
            'embedding_model': model_name,
            'git_commit': git_commit or 'unknown',
            'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'chunking': 'dart_ast' if ast is not None and ast.available else 'lines',
        },
    )

    for i in range(0, len(chunks), WRITE_BATCH_SIZE):
        batch = chunks[i:i + WRITE_BATCH_SIZE]
        documents = [c['document'] for c in batch]
        collection.add(
            ids=[c['id'] for c in batch],
            documents=documents,
            metadatas=[c['metadata'] for c in batch],
            embeddings=model.encode(documents, show_progress_bar=False).tolist(),
        )
    logger.info("Collection %r in %s now holds %d chunk(s)",
                collection_name, chroma_path, collection.count())


class VectorIndex:
    """Read side of the index, used while generating prompts."""

    def __init__(self, chroma_path: Path, collection_name: str, model_name: str):
        chromadb, sentence_transformer_cls = _require_libraries()
        if not chroma_path.is_dir():
            logger.error("Vector index not found: %s (run `python -m pipeline index` first)",
                         chroma_path)
            sys.exit(1)
        client = chromadb.PersistentClient(path=str(chroma_path))
        try:
            self.collection = client.get_collection(name=collection_name)
        except Exception as e:  # chromadb raises different types across versions
            logger.error("Cannot open collection %r in %s: %s (run `python -m pipeline index` first)",
                         collection_name, chroma_path, e)
            sys.exit(1)

        self.info = dict(self.collection.metadata or {})
        built_with = self.info.get('embedding_model')
        if built_with and built_with != model_name:
            logger.error("Index was built with %s but --embedding-model is %s; "
                         "rebuild the index or pass the same model", built_with, model_name)
            sys.exit(1)
        if self.info.get('hnsw:space') != 'cosine':
            logger.error("Collection %r predates the AST-based index; run `python -m pipeline index`",
                         collection_name)
            sys.exit(1)

        logger.info("Loading embedding model %s", model_name)
        self.model = sentence_transformer_cls(model_name)

    def query(self, text: str, issue: IssueRecord, n_results: int) -> List[Dict[str, Any]]:
        """
        Nearest chunks in the issue's language, excluding any that overlap the
        snippet itself. Each hit has document, metadata and a cosine score.
        """
        embedding = self.model.encode(text, show_progress_bar=False).tolist()
        result = self.collection.query(
            query_embeddings=[embedding],
            n_results=min(n_results, self.collection.count()),
            where={'language': issue.language},
            include=['documents', 'metadatas', 'distances'],
        )
        hits = []
        for doc, meta, dist in zip(result['documents'][0], result['metadatas'][0],
                                   result['distances'][0]):
            if not doc or issue.overlaps(meta['file_path'], meta['start_line'],
                                         meta['end_line']):
                continue
            hits.append({'document': doc, 'metadata': meta, 'score': 1.0 - dist})
        return hits
