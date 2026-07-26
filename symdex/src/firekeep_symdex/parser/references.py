"""Extract import and call references from source code using tree-sitter."""

from tree_sitter_language_pack import get_parser

from .languages import LANGUAGE_REGISTRY, _ensure_plugins_loaded
from .symbols import make_symbol_id


def extract_references(content: str, filename: str, language: str) -> list[dict]:
    """Extract import and call references from source code.

    Args:
        content: Raw source code.
        filename: File path (for context).
        language: Language name (must be in LANGUAGE_REGISTRY).

    Returns:
        List of dicts: {"type": "import"|"call", "name": str, "line": int, "from_symbol": str|None}
    """
    _ensure_plugins_loaded()
    if language not in LANGUAGE_REGISTRY:
        return []

    spec = LANGUAGE_REGISTRY[language]
    source_bytes = content.encode("utf-8")

    parser = get_parser(spec.ts_language)
    tree = parser.parse(source_bytes)

    refs: list[dict] = []
    _walk_for_references(tree.root_node, source_bytes, language, refs,
                         filename=filename, enclosing_symbol=None)
    return refs


def _extract_symbol_name(node, spec, source_bytes: bytes):
    """Extract symbol name from an AST node (lightweight version for reference tracking)."""
    # Handle Go type_declaration specially (name is inside type_spec child)
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                name_node = child.child_by_field_name("name")
                if name_node:
                    return _node_text(name_node, source_bytes)
        return None

    # C: function_definition has name inside declarator > function_declarator > declarator
    if node.type == "function_definition" and spec.ts_language == "c":
        decl = node.child_by_field_name("declarator")
        if decl and decl.type == "function_declarator":
            name_node = decl.child_by_field_name("declarator")
            if name_node:
                return _node_text(name_node, source_bytes)
        return None

    # Kotlin: uses child node types instead of field names
    if spec.ts_language == "kotlin":
        for child in node.children:
            if child.type == "simple_identifier":
                return _node_text(child, source_bytes)
        return None

    # Standard field-based name extraction
    if node.type not in spec.name_fields:
        return None

    field_name = spec.name_fields[node.type]
    name_node = node.child_by_field_name(field_name)
    if name_node:
        return _node_text(name_node, source_bytes)
    return None


def _walk_for_references(node, source_bytes: bytes, language: str, refs: list[dict],
                         filename: str = "", enclosing_symbol=None,
                         enclosing_name: str = ""):
    """Recursively walk AST to find import and call nodes."""
    spec = LANGUAGE_REGISTRY.get(language)
    if spec and node.type in spec.symbol_node_types:
        kind = spec.symbol_node_types[node.type]
        name = _extract_symbol_name(node, spec, source_bytes)
        if name:
            # Build qualified name with parent context (e.g., ClassName.method_name)
            if enclosing_name and kind in ("function", "method"):
                qualified_name = f"{enclosing_name}.{name}"
                kind = "method"
            else:
                qualified_name = name
            enclosing_symbol = make_symbol_id(filename, qualified_name, kind)
            enclosing_name = qualified_name if kind == "class" else enclosing_name

    _extract_node_references(node, source_bytes, language, refs, from_symbol=enclosing_symbol)
    for child in node.children:
        _walk_for_references(child, source_bytes, language, refs, filename, enclosing_symbol,
                             enclosing_name)


def _extract_node_references(node, source_bytes: bytes, language: str, refs: list[dict],
                             from_symbol=None):
    """Extract references from a single AST node based on language."""
    node_type = node.type
    line = node.start_point[0] + 1

    # --- Imports ---
    if language == "python":
        if node_type == "import_statement":
            # import X, import X.Y
            for child in node.children:
                if child.type == "dotted_name":
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})
        elif node_type == "import_from_statement":
            # from X import Y, Z
            module = None
            for child in node.children:
                if child.type == "dotted_name" and module is None:
                    module = _node_text(child, source_bytes)
                elif child.type in ("dotted_name", "identifier") and module is not None:
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": f"{module}.{name}", "line": line, "from_symbol": None})
                elif child.type == "aliased_import" and module is not None:
                    # from X import Y as Z — extract the original name
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node, source_bytes)
                        refs.append({"type": "import", "name": f"{module}.{name}", "line": line, "from_symbol": None})
                elif child.type == "import_prefix":
                    continue
            # If no named imports found (e.g. from X import *), record module
            if module and not any(r["line"] == line for r in refs):
                refs.append({"type": "import", "name": module, "line": line, "from_symbol": None})

    elif language in ("javascript", "typescript"):
        if node_type == "import_statement":
            source = node.child_by_field_name("source")
            if source:
                name = _node_text(source, source_bytes).strip("'\"")
                refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "go":
        if node_type == "import_spec":
            path_node = node.child_by_field_name("path")
            if path_node:
                name = _node_text(path_node, source_bytes).strip('"')
                refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "rust":
        if node_type == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg:
                name = _node_text(arg, source_bytes)
                refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "java":
        if node_type == "import_declaration":
            for child in node.children:
                if child.type == "scoped_identifier":
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})
                    break

    elif language == "php":
        if node_type == "namespace_use_declaration":
            for child in node.children:
                if child.type == "namespace_use_clause":
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "c":
        if node_type == "preproc_include":
            path_node = node.child_by_field_name("path")
            if path_node:
                name = _node_text(path_node, source_bytes).strip('"<>')
                refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "csharp":
        if node_type == "using_directive":
            for child in node.children:
                if child.type in ("identifier", "qualified_name"):
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})
                    break

    elif language == "ruby":
        if node_type == "call" and node.child_count > 0:
            method = node.child_by_field_name("method")
            if method and _node_text(method, source_bytes) in ("require", "require_relative"):
                args = node.child_by_field_name("arguments")
                if args:
                    for child in args.children:
                        if child.type == "string":
                            name = _node_text(child, source_bytes).strip("'\"")
                            refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})

    elif language == "kotlin":
        if node_type == "import_header":
            # Extract full import path (skip the 'import' keyword token)
            for child in node.children:
                if child.type not in ("import",):
                    name = _node_text(child, source_bytes).strip()
                    if name:
                        refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})
                        break

    elif language == "swift":
        if node_type == "import_declaration":
            for child in node.children:
                if child.type == "identifier":
                    name = _node_text(child, source_bytes)
                    refs.append({"type": "import", "name": name, "line": line, "from_symbol": None})
                    break

    # --- Calls (language-agnostic for common patterns) ---
    if node_type == "call" and language == "python":
        func = node.child_by_field_name("function")
        if func:
            name = _node_text(func, source_bytes)
            refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "call_expression" and language in ("javascript", "typescript", "go", "rust", "c"):
        func = node.child_by_field_name("function")
        if func:
            name = _node_text(func, source_bytes)
            # Detect CommonJS require() as imports in JS/TS
            if language in ("javascript", "typescript") and name in ("require", "require.resolve"):
                args = node.child_by_field_name("arguments")
                if args:
                    for child in args.children:
                        if child.type == "string":
                            mod = _node_text(child, source_bytes).strip("'\"")
                            refs.append({"type": "import", "name": mod, "line": line, "from_symbol": None})
                            break
            else:
                refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "method_invocation" and language == "java":
        name_node = node.child_by_field_name("name")
        obj_node = node.child_by_field_name("object")
        if name_node:
            name = _node_text(name_node, source_bytes)
            if obj_node:
                name = f"{_node_text(obj_node, source_bytes)}.{name}"
            refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "function_call_expression" and language == "php":
        func = node.child_by_field_name("function")
        if func:
            name = _node_text(func, source_bytes)
            refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "member_call_expression" and language == "php":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node, source_bytes)
            refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "invocation_expression" and language == "csharp":
        func = node.child_by_field_name("function")
        if func:
            name = _node_text(func, source_bytes)
            refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "call" and language == "ruby":
        method = node.child_by_field_name("method")
        if method:
            method_name = _node_text(method, source_bytes)
            # Skip require/require_relative - already captured as imports
            if method_name not in ("require", "require_relative"):
                name = method_name
                receiver = node.child_by_field_name("receiver")
                if receiver:
                    name = f"{_node_text(receiver, source_bytes)}.{name}"
                refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})

    elif node_type == "call_expression" and language in ("kotlin", "swift"):
        # Kotlin/Swift: first child is the function identifier
        for child in node.children:
            if child.type in ("simple_identifier", "navigation_expression"):
                name = _node_text(child, source_bytes)
                refs.append({"type": "call", "name": name, "line": line, "from_symbol": from_symbol})
                break


def _node_text(node, source_bytes: bytes) -> str:
    """Get the text content of an AST node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8")
