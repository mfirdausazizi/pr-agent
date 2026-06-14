import ast
import re
from dataclasses import dataclass
from pathlib import Path

from pr_agent.algo.types import FilePatchInfo

try:
    from pr_agent.algo.repo_context.changed_ranges import ChangedRange
except ImportError:
    @dataclass
    class ChangedRange:
        line_start: int
        line_end: int


try:
    from pr_agent.algo.repo_context.models import ChangedSymbol
except ImportError:
    @dataclass
    class ChangedSymbol:
        path: str
        name: str
        kind: str
        language: str
        line_start: int
        line_end: int


_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_KEYWORDS = {
    "and",
    "as",
    "async",
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "def",
    "delete",
    "do",
    "else",
    "except",
    "export",
    "exports",
    "finally",
    "for",
    "from",
    "function",
    "if",
    "import",
    "in",
    "let",
    "module",
    "new",
    "pass",
    "return",
    "switch",
    "try",
    "var",
    "while",
    "with",
    "yield",
}


def extract_changed_symbols(
    diff_files: list[FilePatchInfo],
    changed_ranges_by_file: dict[str, list[ChangedRange]] | None = None,
    max_symbols=30,
) -> list[ChangedSymbol]:
    symbols = []
    for file_patch in diff_files:
        path = file_patch.filename
        ranges = _ranges_for_file(file_patch, changed_ranges_by_file)
        language = _language_for_path(path)
        if file_patch.head_file and ranges and language == "python":
            symbols.extend(_extract_python_symbols(file_patch, ranges))
        elif (
            file_patch.head_file
            and ranges
            and language in {"javascript", "typescript"}
        ):
            symbols.extend(
                _extract_javascript_symbols(file_patch, ranges, language)
            )
        else:
            symbols.extend(_extract_fallback_symbols(file_patch))
    return _dedupe_and_cap(symbols, max_symbols)


def _ranges_for_file(
    file_patch: FilePatchInfo,
    changed_ranges_by_file: dict[str, list[ChangedRange]] | None,
) -> list[ChangedRange]:
    if changed_ranges_by_file:
        ranges = changed_ranges_by_file.get(file_patch.filename)
        if ranges is not None:
            return ranges
    return _added_ranges_from_patch(
        file_patch.filename,
        file_patch.patch or "",
    )


def _language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    return ""


def _line_start(changed_range: ChangedRange) -> int:
    return getattr(
        changed_range,
        "line_start",
        getattr(
            changed_range,
            "start",
            getattr(changed_range, "start_line", 0),
        ),
    )


def _line_end(changed_range: ChangedRange) -> int:
    return getattr(
        changed_range,
        "line_end",
        getattr(changed_range, "end", getattr(changed_range, "end_line", 0)),
    )


def _overlaps(start: int, end: int, changed_range: ChangedRange) -> bool:
    return (
        start <= _line_end(changed_range)
        and end >= _line_start(changed_range)
    )


def _extract_python_symbols(
    file_patch: FilePatchInfo,
    ranges: list[ChangedRange],
) -> list[ChangedSymbol]:
    try:
        tree = ast.parse(file_patch.head_file)
    except SyntaxError:
        return _extract_fallback_symbols(file_patch)

    parent_by_node = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_by_node[child] = parent

    candidates = []
    for node in ast.walk(tree):
        if not isinstance(
            node,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ):
            continue
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", node.lineno)
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        if kind == "function" and isinstance(
            parent_by_node.get(node),
            ast.ClassDef,
        ):
            kind = "method"
        candidates.append((node.name, kind, line_start, line_end))

    symbols = []
    for changed_range in ranges:
        overlapping = [
            candidate
            for candidate in candidates
            if _overlaps(candidate[2], candidate[3], changed_range)
        ]
        if not overlapping:
            continue
        smallest = min(
            overlapping,
            key=lambda candidate: (candidate[3] - candidate[2], candidate[2]),
        )
        symbols.append(
            ChangedSymbol(
                path=file_patch.filename,
                name=smallest[0],
                kind=smallest[1],
                language="python",
                line_start=smallest[2],
                line_end=smallest[3],
            )
        )
    return symbols


def _extract_javascript_symbols(
    file_patch: FilePatchInfo,
    ranges: list[ChangedRange],
    language: str,
) -> list[ChangedSymbol]:
    symbols = []
    lines = file_patch.head_file.splitlines()
    for changed_range in ranges:
        start = max(_line_start(changed_range), 1)
        end = min(_line_end(changed_range), len(lines))
        for line_number in range(start, end + 1):
            symbol = _symbol_from_javascript_line(
                file_patch.filename,
                lines[line_number - 1],
                line_number,
                language,
            )
            if symbol:
                symbols.append(symbol)
    return symbols


def _symbol_from_javascript_line(
    path: str,
    line: str,
    line_number: int,
    language: str,
) -> ChangedSymbol | None:
    patterns = [
        (
            rf"\bexport\s+(?:async\s+)?function\s+({_IDENTIFIER})\b",
            "function",
        ),
        (rf"\b(?:async\s+)?function\s+({_IDENTIFIER})\b", "function"),
        (
            rf"\b(?:const|let|var)\s+({_IDENTIFIER})\s*=\s*(?:async\s*)?"
            rf"(?:\([^)]*\)|{_IDENTIFIER})(?:\s*:\s*[^=]+)?\s*=>",
            "function",
        ),
        (rf"\bclass\s+({_IDENTIFIER})\b", "class"),
        (rf"\bmodule\.exports\.({_IDENTIFIER})\s*=", "function"),
        (rf"\bexports\.({_IDENTIFIER})\s*=", "function"),
        (
            rf"^\s*(?:async\s+)?({_IDENTIFIER})\s*\([^)]*\)\s*\{{",
            "method",
        ),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, line)
        if not match:
            continue
        name = match.group(1)
        if name in _KEYWORDS:
            continue
        return ChangedSymbol(
            path=path,
            name=name,
            kind=kind,
            language=language,
            line_start=line_number,
            line_end=line_number,
        )
    return None


def _extract_fallback_symbols(
    file_patch: FilePatchInfo,
) -> list[ChangedSymbol]:
    language = _language_for_path(file_patch.filename)
    if not language:
        language = "unknown"
    symbols = []
    for line_number, line in _added_lines_from_patch(file_patch.patch or ""):
        for name, kind in _fallback_identifiers(line):
            symbols.append(
                ChangedSymbol(
                    path=file_patch.filename,
                    name=name,
                    kind=kind,
                    language=language,
                    line_start=line_number,
                    line_end=line_number,
                )
            )
    return symbols


def _fallback_identifiers(line: str) -> list[tuple[str, str]]:
    identifiers = []
    for pattern, kind in [
        (rf"\bclass\s+({_IDENTIFIER})\b", "class"),
        (rf"\b(?:def|function)\s+({_IDENTIFIER})\b", "function"),
        (rf"\b({_IDENTIFIER})\s*\(", "function"),
    ]:
        for match in re.finditer(pattern, line):
            name = match.group(1)
            if len(name) > 2 and name not in _KEYWORDS:
                identifiers.append((name, kind))
    return identifiers


def _make_changed_range(
    path: str,
    line_start: int,
    line_end: int,
) -> ChangedRange:
    try:
        return ChangedRange(
            path=path,
            start_line=line_start,
            end_line=line_end,
        )
    except TypeError:
        return ChangedRange(line_start=line_start, line_end=line_end)


def _added_ranges_from_patch(path: str, patch: str) -> list[ChangedRange]:
    return [
        _make_changed_range(path, line_number, line_number)
        for line_number, _ in _added_lines_from_patch(patch)
    ]


def _added_lines_from_patch(patch: str) -> list[tuple[int, str]]:
    added_lines = []
    new_line_number = 0
    for line in patch.splitlines():
        hunk_match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk_match:
            new_line_number = int(hunk_match.group(1))
            continue
        if line.startswith("+++") or not line:
            continue
        if line.startswith("+"):
            added_lines.append((new_line_number, line[1:]))
            new_line_number += 1
        elif line.startswith("-"):
            continue
        else:
            new_line_number += 1
    return added_lines


def _dedupe_and_cap(
    symbols: list[ChangedSymbol],
    max_symbols: int,
) -> list[ChangedSymbol]:
    deduped = []
    seen = set()
    for symbol in symbols:
        key = (symbol.path, symbol.name, symbol.kind, symbol.line_start)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(symbol)
        if len(deduped) >= max_symbols:
            break
    return deduped
