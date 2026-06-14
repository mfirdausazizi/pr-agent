from pr_agent.algo.repo_context.symbol_extractor import (
    ChangedRange,
    extract_changed_symbols,
)
from pr_agent.algo.types import FilePatchInfo


def _file(filename, head_file="", patch=""):
    return FilePatchInfo(
        base_file="",
        head_file=head_file,
        patch=patch,
        filename=filename,
    )


def _symbols_by_name(symbols):
    return {symbol.name: symbol for symbol in symbols}


def _range(path, line_start, line_end):
    try:
        return ChangedRange(
            path=path,
            start_line=line_start,
            end_line=line_end,
        )
    except TypeError:
        return ChangedRange(line_start=line_start, line_end=line_end)


def test_extracts_changed_python_symbols():
    head_file = "\n".join(
        [
            "def unchanged():",
            "    return 1",
            "",
            "def changed_function():",
            "    value = 1",
            "    return value",
            "",
            "async def changed_async():",
            "    return 2",
            "",
            "class ChangedClass:",
            "    class_attr = 1",
            "",
            "    def changed_method(self):",
            "        return self.class_attr",
        ]
    )
    symbols = extract_changed_symbols(
        [_file("example.py", head_file=head_file)],
        {
            "example.py": [
                _range("example.py", 5, 5),
                _range("example.py", 8, 8),
                _range("example.py", 12, 12),
                _range("example.py", 14, 14),
            ]
        },
    )

    by_name = _symbols_by_name(symbols)
    assert by_name["changed_function"].kind == "function"
    assert by_name["changed_function"].language == "python"
    assert (
        by_name["changed_function"].line_start,
        by_name["changed_function"].line_end,
    ) == (4, 6)
    assert by_name["changed_async"].kind == "function"
    assert (
        by_name["changed_async"].line_start,
        by_name["changed_async"].line_end,
    ) == (8, 9)
    assert by_name["ChangedClass"].kind == "class"
    assert (
        by_name["ChangedClass"].line_start,
        by_name["ChangedClass"].line_end,
    ) == (11, 15)
    assert by_name["changed_method"].kind == "method"
    assert (
        by_name["changed_method"].line_start,
        by_name["changed_method"].line_end,
    ) == (14, 15)


def test_extracts_changed_javascript_and_typescript_symbols():
    js_head = "\n".join(
        [
            "export function exportedName() {",
            "  return 1;",
            "}",
            "const arrowName = (value) => value + 1;",
            "class ExampleClass {",
            "  methodName(value) {",
            "    return value;",
            "  }",
            "}",
            "module.exports.moduleName = function () {};",
            "exports.exportName = () => {};",
        ]
    )
    ts_head = "let typedArrow = (value: number): number => value + 1;"

    symbols = extract_changed_symbols(
        [
            _file("example.js", head_file=js_head),
            _file("typed.ts", head_file=ts_head),
        ],
        {
            "example.js": [
                _range("example.js", 1, 1),
                _range("example.js", 4, 4),
                _range("example.js", 5, 6),
                _range("example.js", 10, 11),
            ],
            "typed.ts": [_range("typed.ts", 1, 1)],
        },
    )

    by_name = _symbols_by_name(symbols)
    assert by_name["exportedName"].kind == "function"
    assert by_name["exportedName"].language == "javascript"
    assert by_name["arrowName"].kind == "function"
    assert by_name["ExampleClass"].kind == "class"
    assert by_name["methodName"].kind == "method"
    assert by_name["moduleName"].kind == "function"
    assert by_name["exportName"].kind == "function"
    assert by_name["typedArrow"].language == "typescript"


def test_extracts_enclosing_javascript_function_for_body_change():
    head_file = "\n".join(
        [
            "const Common = {",
            "  db_delete: async function(table, data) {",
            "    let query = `DELETE FROM ${table}`;",
            "    if (!Array.isArray(data)) {",
            "      throw new Error('blocked');",
            "    }",
            "    return query;",
            "  },",
            "};",
        ]
    )

    symbols = extract_changed_symbols(
        [_file("core/common.js", head_file=head_file)],
        {"core/common.js": [_range("core/common.js", 4, 5)]},
    )

    by_name = _symbols_by_name(symbols)
    assert by_name["db_delete"].kind == "method"
    assert by_name["db_delete"].line_start == 2


def test_extracts_fallback_identifiers_from_patch_when_head_file_missing():
    patch = "\n".join(
        [
            "@@ -0,0 +1,4 @@",
            "+def fallback_function():",
            "+    return helper_call()",
            "+class FallbackClass:",
            "+    pass",
        ]
    )
    symbols = extract_changed_symbols(
        [_file("missing.txt", head_file=None, patch=patch)]
    )

    names = {symbol.name for symbol in symbols}
    assert {
        "fallback_function",
        "helper_call",
        "FallbackClass",
    }.issubset(names)


def test_deduplicates_symbols_and_applies_max_symbols_cap():
    head_file = "\n".join(
        [
            "def first():",
            "    return 1",
            "",
            "def second():",
            "    return 2",
            "",
            "def third():",
            "    return 3",
        ]
    )
    symbols = extract_changed_symbols(
        [_file("many.py", head_file=head_file)],
        {
            "many.py": [
                _range("many.py", 1, 2),
                _range("many.py", 2, 2),
                _range("many.py", 4, 5),
                _range("many.py", 7, 8),
            ]
        },
        max_symbols=2,
    )

    assert [symbol.name for symbol in symbols] == ["first", "second"]
