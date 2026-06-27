"""
tree-sitter AST 分析

按文件扩展名识别语言（C / C++ / Lua），用对应 tree-sitter 解析器提取：
- symbols：定义类节点（函数 / 类 / 结构体 / 枚举 / typedef / 命名空间），含 kind/name/行范围
- node_types：整棵树的节点类型 Counter（用于粗粒度结构画像）

设计要点：
- 解析器在首次使用时惰性构建并缓存，避免导入即崩。
- tree-sitter 0.23 / 0.24 / 0.25 的 Parser 构造 API 不同，做兼容兜底。
- 单文件解析包 try/except，单坏文件不致整目录分析中止（记入 errors）。
- 健壮性优先：取不到名字就存 name=''，不抛。
"""

import os
from collections import Counter

# ============================================================
# 语言映射
# ============================================================
_EXT_TO_LANG = {
    ".lua": "lua",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

# 各语言「定义类」节点类型（出现即视为一个符号）
_SYMBOL_TYPES = {
    "lua": {
        "function_declaration",
        "function_definition",
        "local_function",
    },
    "c": {
        "function_definition",
        "struct_specifier",
        "enum_specifier",
        "type_definition",
    },
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "method_definition",
        "namespace_definition",
    },
}

# 跳过的目录名（含隐藏目录，下方另判 dotfile）
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "build",
    "dist",
    ".cache",
    "third_party",
    "3rdparty",
}

# 单文件大小上限（512KB），避免巨型生成文件拖慢分析
_MAX_FILE_BYTES = 512 * 1024


# ============================================================
# tree-sitter 惰性加载
# ============================================================
_TS_READY = False
_PARSERS: dict[str, object] = {}
_TS_IMPORT_ERROR: Exception | None = None


def _ensure_ts() -> None:
    """
    惰性导入 tree-sitter 及各语言包；失败抛 RuntimeError（清晰错误信息）。

    放在函数内而非模块顶部，是为了：
    1) 让 db.py / routes.py / __init__.py 等不依赖 tree-sitter 的模块可以无副作用导入；
    2) 测试套件在缺包时仍能跑（test_ast_analysis 用 importorskip 兜底）。
    """
    global _TS_READY, _TS_IMPORT_ERROR
    if _TS_READY:
        return
    try:
        import tree_sitter_c  # noqa: F401
        import tree_sitter_cpp  # noqa: F401
        import tree_sitter_lua  # noqa: F401
        from tree_sitter import Language, Parser  # noqa: F401
    except Exception as e:  # ImportError 或其它
        _TS_IMPORT_ERROR = e
        raise RuntimeError(
            "tree-sitter 依赖未安装，请先 `pip install tree-sitter "
            "tree-sitter-c tree-sitter-cpp tree-sitter-lua`"
        ) from e
    _TS_READY = True


def _make_parser(lang_pkg) -> object:
    """
    兼容 tree-sitter 0.23 / 0.24 / 0.25 的构造方式：
        >=0.25 : Parser(language)
        0.23/24: Parser() 然后赋值 .language
    """
    from tree_sitter import Language, Parser

    lang = Language(lang_pkg.language())
    try:
        return Parser(lang)
    except TypeError:
        p = Parser()
        p.language = lang
        return p


def _get_parser(language: str) -> object:
    """按语言名取（惰性构建并缓存的）解析器。"""
    if language in _PARSERS:
        return _PARSERS[language]
    _ensure_ts()
    import tree_sitter_c
    import tree_sitter_cpp
    import tree_sitter_lua

    pkg = {
        "lua": tree_sitter_lua,
        "c": tree_sitter_c,
        "cpp": tree_sitter_cpp,
    }[language]
    parser = _make_parser(pkg)
    _PARSERS[language] = parser
    return parser


# ============================================================
# 符号提取
# ============================================================
def _node_text(node) -> str:
    """安全取节点文本。"""
    try:
        return node.text.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _first_identifier(node) -> str:
    """
    在子树中深度优先找第一个 identifier / type_identifier 节点，返回其文本。

    用于：C 函数定义的名字藏在 function_declarator→identifier；
         typedef/struct 的名字是 type_identifier。
    找不到返回 ''（健壮性优先）。
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in ("identifier", "type_identifier"):
            return _node_text(cur)
        # 保持从左到右顺序：反向入栈
        for child in reversed(cur.children):
            stack.append(child)
    return ""


def _extract_symbol_name(node, language: str) -> str:
    """
    从一个定义节点取名字。

    优先用 tree-sitter 的 name 字段（lua function_declaration / 多数现代语法都有），
    否则回退到子树首个 identifier。
    """
    # 1) 优先 name field
    nm = node.child_by_field_name("name")
    if nm is not None:
        txt = _node_text(nm)
        if txt:
            return txt
    # 2) declarator field（C/C++ function_definition）
    decl = node.child_by_field_name("declarator")
    if decl is not None:
        nm = _node_text(decl.child_by_field_name("declarator")) if decl.child_by_field_name else ""
        if nm:
            return nm
        ident = _first_identifier(decl)
        if ident:
            return ident
    # 3) 子树首个 identifier
    return _first_identifier(node)


def _walk_symbols_and_types(
    root, symbol_types: set[str]
) -> tuple[list[dict], Counter]:
    """
    遍历整棵树：
      - 收集所有节点类型到 Counter（node_types）
      - 命中 symbol_types 的节点产出 symbol 记录

    返回 (symbols, node_types_counter)。
    """
    symbols: list[dict] = []
    node_types: Counter = Counter()
    stack = [root]
    while stack:
        node = stack.pop()
        node_types[node.type] += 1
        if node.type in symbol_types:
            symbols.append(
                {
                    "kind": node.type,
                    "name": "",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                }
            )
        # 子节点按原顺序入栈（反向以保持遍历顺序）
        for child in reversed(node.children):
            stack.append(child)
    return symbols, node_types


def _fill_symbol_names(root, symbols: list[dict], language: str) -> None:
    """
    二次遍历：对每个 symbol 节点补全 name。

    单独一遍是为了不让「取名字」拖慢整树节点类型统计（后者只关心 type）。
    """
    symbol_types = _SYMBOL_TYPES.get(language, set())
    idx = 0
    stack = [root]
    while stack and idx < len(symbols):
        node = stack.pop()
        if node.type in symbol_types:
            # symbols 是按遍历顺序产出的，故可顺序对齐补名
            symbols[idx]["name"] = _extract_symbol_name(node, language)
            idx += 1
        for child in reversed(node.children):
            stack.append(child)


# ============================================================
# 单文件 / 目录
# ============================================================
def analyze_file(path: str, language: str) -> dict:
    """
    解析单个源文件，返回 AST 画像。

    Returns:
        {
          "rel_path": str,         # 调用方填，这里先放绝对 path 占位
          "language": str,
          "symbol_count": int,
          "symbols": [{kind,name,start_line,end_line}, ...],
          "node_types": {type: count, ...}
        }

    依赖未安装时抛 RuntimeError；解析本身出错抛 RuntimeError。
    """
    parser = _get_parser(language)
    with open(path, "rb") as f:
        source = f.read()

    tree = parser.parse(source)
    if tree is None or tree.root_node is None:
        return {
            "rel_path": path,
            "language": language,
            "symbol_count": 0,
            "symbols": [],
            "node_types": {},
        }

    symbol_types = _SYMBOL_TYPES.get(language, set())
    symbols, node_types = _walk_symbols_and_types(tree.root_node, symbol_types)
    _fill_symbol_names(tree.root_node, symbols, language)

    return {
        "rel_path": path,
        "language": language,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "node_types": dict(node_types),
    }


def analyze_directory(root: str, component: str) -> dict:
    """
    遍历目录，对每个支持的源文件做 AST 分析，聚合统计。

    Returns:
        {
          "component": str,
          "files": [{rel_path, language, symbol_count, symbols, node_types}, ...],
          "stats": {
            "files": int, "symbols": int,
            "by_language": {lang: file_count, ...}
          },
          "errors": [{"path": str, "error": str}, ...]
        }

    每个文件包 try/except，单坏文件入 errors 列表、不中断整体。
    tree-sitter 依赖缺失 → 抛 RuntimeError（让上层作为单组件错误上报）。
    """
    files: list[dict] = []
    errors: list[dict] = []
    by_language: Counter = Counter()
    symbols_total = 0

    # 先确认依赖可用，避免走完整遍历后才崩
    _ensure_ts()

    for dirpath, dirnames, filenames in os.walk(root):
        # 原地过滤跳过目录 / 隐藏目录
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            language = _EXT_TO_LANG.get(ext)
            if language is None:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                # 跳过超大文件
                if os.path.getsize(fpath) > _MAX_FILE_BYTES:
                    continue
                result = analyze_file(fpath, language)
                # 相对路径（用于持久化与展示）
                rel = os.path.relpath(fpath, root)
                result["rel_path"] = rel.replace(os.sep, "/")
                files.append(result)
                by_language[language] += 1
                symbols_total += result["symbol_count"]
            except Exception as e:
                errors.append({"path": fpath, "error": str(e)})

    return {
        "component": component,
        "files": files,
        "stats": {
            "files": len(files),
            "symbols": symbols_total,
            "by_language": dict(by_language),
        },
        "errors": errors,
    }
