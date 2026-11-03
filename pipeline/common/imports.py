"""
Import statements of Java, Kotlin and Dart files.

The snippet sent to the model starts below the file's imports, so a fix that
needs a new type (e.g. Map instead of HashMap) cannot add its import there.
These helpers let Stage 2 show the model the existing imports, and let Stage 3
merge the imports the model asks for into the file header and drop the ones a
patch left unused.
"""

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

_IMPORT_RE = {
    'java': re.compile(r'^import\s+(?P<static>static\s+)?(?P<name>[\w.]+\*?)\s*;\s*$'),
    'kotlin': re.compile(r'^import\s+(?P<name>[\w.`]+\*?)(?:\s+as\s+(?P<alias>\w+))?\s*;?\s*$'),
    'dart': re.compile(r'''^import\s+['"](?P<name>[^'"]+)['"][^;]*;\s*$'''),
}
_HEADER_RE = re.compile(r'^(package|library)\b')


def import_language(file_path: str) -> Optional[str]:
    """The import syntax a file uses, or None for files without imports."""
    for ext, lang in (('.java', 'java'), ('.kt', 'kotlin'), ('.kts', 'kotlin'),
                      ('.dart', 'dart')):
        if file_path.lower().endswith(ext):
            return lang
    return None


def is_import(line: str, language: str) -> bool:
    return bool(_IMPORT_RE[language].match(line.strip()))


def normalize(statement: str, language: str) -> str:
    """One canonical spelling per import, so duplicates are recognised."""
    text = ' '.join(statement.split())
    if language in ('java', 'dart') and not text.endswith(';'):
        text += ';'
    return text.replace(' ;', ';')


def _sort_key(statement: str, language: str) -> Tuple[int, str]:
    """(group, name): Java static imports are group 0, every other import group 1."""
    match = _IMPORT_RE[language].match(statement)
    group = 0 if language == 'java' and match and match.group('static') else 1
    return (group, match.group('name') if match else statement)


def _simple_name(statement: str, language: str) -> Optional[str]:
    """The name an import brings into scope, or None if it can't be told."""
    match = _IMPORT_RE[language].match(statement.strip())
    if not match or language == 'dart' or match.group('name').endswith('*'):
        return None
    if language == 'kotlin' and match.group('alias'):
        return match.group('alias')
    return match.group('name').rsplit('.', 1)[-1].strip('`')


def import_indices(lines: Sequence[str], language: str) -> List[int]:
    return [i for i, line in enumerate(lines) if is_import(line, language)]


def file_imports(lines: Sequence[str], language: str, before_line: int) -> Optional[str]:
    """
    The import block above line before_line (1-based), for the prompt. None
    when the snippet itself reaches into the imports (the model sees them).
    """
    indices = import_indices(lines, language)
    if indices and indices[-1] + 1 >= before_line:
        return None
    return '\n'.join(lines[i].strip() for i in indices)


def read_file_imports(repo_root: Path, file_path: str, before_line: int) -> Optional[str]:
    language = import_language(file_path)
    if not language:
        return None
    try:
        lines = (repo_root / file_path).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return file_imports(lines, language, before_line)


def split_leading_imports(code: List[str], original: List[str], language: str
                          ) -> Tuple[List[str], List[str]]:
    """
    Models sometimes put new imports at the top of the returned code. Unless
    the original snippet had imports there too, move them out of the code.
    """
    if any(is_import(line, language) for line in original):
        return code, []
    i = 0
    imports = []
    while i < len(code) and (not code[i].strip() or is_import(code[i], language)):
        if code[i].strip():
            imports.append(code[i].strip())
        i += 1
    return (code[i:], imports) if imports else (code, [])


def _header_end(lines: Sequence[str]) -> int:
    """Index after the leading comments and package/library line."""
    end = 0
    in_comment = False
    for i, line in enumerate(lines):
        text = line.strip()
        if in_comment or text.startswith('/*'):
            in_comment = '*/' not in text
            end = i + 1
        elif not text or text.startswith('//'):
            continue
        elif _HEADER_RE.match(text):
            return i + 1
        else:
            break
    return end


def _insert(lines: List[str], statement: str, language: str) -> None:
    """Insert one import in sorted position within its group."""
    key = _sort_key(statement, language)
    indices = import_indices(lines, language)
    group = [i for i in indices if _sort_key(lines[i].strip(), language)[0] == key[0]]
    if group:
        after = [i for i in group if _sort_key(lines[i].strip(), language) > key]
        lines.insert(after[0] if after else group[-1] + 1, statement)
    elif indices and key[0] == 0:              # first static import
        lines[indices[0]:indices[0]] = [statement, '']
    elif indices:                              # first non-static import
        lines[indices[-1] + 1:indices[-1] + 1] = ['', statement]
    else:
        at = _header_end(lines)
        block = [statement]
        if at > 0 and lines[at - 1].strip():
            block.insert(0, '')
        if at >= len(lines) or lines[at].strip():
            block.append('')
        lines[at:at] = block


def merge_imports(lines: List[str], wanted: Iterable[str], language: str
                  ) -> Tuple[List[str], List[str]]:
    """
    Add each wanted import the file does not have yet, in sorted position
    within its group (Java: static imports first, then the others, separated
    by a blank line). Returns the new lines and the imports added.
    """
    lines = list(lines)
    present = {normalize(lines[i], language) for i in import_indices(lines, language)}
    added = []
    for statement in sorted({normalize(s, language) for s in wanted},
                            key=lambda s: _sort_key(s, language)):
        if statement in present or not is_import(statement, language):
            continue
        _insert(lines, statement, language)
        present.add(statement)
        added.append(statement)
    return lines, added


def used_imports(lines: Sequence[str], code: str, language: str) -> Set[str]:
    """Imports of the file whose name appears in code."""
    found = set()
    for i in import_indices(lines, language):
        name = _simple_name(lines[i], language)
        if name and re.search(rf'\b{re.escape(name)}\b', code):
            found.add(normalize(lines[i], language))
    return found


def remove_unused_imports(lines: List[str], candidates: Iterable[str], language: str
                          ) -> Tuple[List[str], List[str]]:
    """
    Remove the candidate imports whose name no longer appears outside the
    import lines. Only candidates are touched, so imports that were already
    unused before the patch stay for SonarQube to report.
    """
    candidates = set(candidates)
    indices = import_indices(lines, language)
    body = '\n'.join(line for i, line in enumerate(lines) if i not in set(indices))
    drop = []
    for i in indices:
        statement = normalize(lines[i], language)
        name = _simple_name(lines[i], language)
        if statement in candidates and name and not re.search(rf'\b{re.escape(name)}\b', body):
            drop.append(i)
    if not drop:
        return lines, []
    removed = [normalize(lines[i], language) for i in drop]
    kept = [line for i, line in enumerate(lines) if i not in set(drop)]
    # Collapse the blank lines left behind by an emptied import group.
    cleaned: List[str] = []
    for line in kept:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    return cleaned, removed


def header_end_line(lines: Sequence[str], language: Optional[str]) -> int:
    """Last line (1-based) of the package/import header; patches start below it."""
    last = _header_end(lines)
    if language:
        indices = import_indices(lines, language)
        if indices:
            last = max(last, indices[-1] + 1)
    return last
