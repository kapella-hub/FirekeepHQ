"""Generic AST symbol extractor using tree-sitter."""

from typing import Optional
from tree_sitter_language_pack import get_parser

from .symbols import Symbol, make_symbol_id, compute_content_hash
from .languages import LanguageSpec, LANGUAGE_REGISTRY, _ensure_plugins_loaded

# Node types that represent function expressions (RHS of assignments)
_FUNC_EXPR_TYPES = frozenset({
    "function_expression", "function", "arrow_function",
    "generator_function",
})

# HTTP methods and middleware patterns for route extraction
_HTTP_METHODS = frozenset({
    "get", "post", "put", "delete", "patch", "head", "options", "all", "use", "listen",
})


def parse_file(content: str, filename: str, language: str) -> list[Symbol]:
    """Parse source code and extract symbols using tree-sitter.
    
    Args:
        content: Raw source code
        filename: File path (for ID generation)
        language: Language name (must be in LANGUAGE_REGISTRY)
    
    Returns:
        List of Symbol objects
    """
    _ensure_plugins_loaded()
    if language not in LANGUAGE_REGISTRY:
        return []
    
    spec = LANGUAGE_REGISTRY[language]
    source_bytes = content.encode("utf-8")
    
    # Get parser for this language
    parser = get_parser(spec.ts_language)
    tree = parser.parse(source_bytes)
    
    symbols = []
    _walk_tree(tree.root_node, spec, source_bytes, filename, language, symbols, None)

    # Capture file preamble (imports, module docstring, setup code before first symbol)
    if symbols:
        first_byte = min(s.byte_offset for s in symbols)
        if first_byte > 0:
            preamble_bytes = source_bytes[:first_byte]
            preamble_text = preamble_bytes.decode("utf-8").strip()
            if preamble_text:
                first_line = min(s.line for s in symbols)
                preamble = Symbol(
                    id=make_symbol_id(filename, "__preamble__", "module"),
                    file=filename,
                    name="__preamble__",
                    qualified_name="__preamble__",
                    kind="module",
                    language=language,
                    signature=f"# {filename}",
                    docstring="",
                    line=1,
                    end_line=max(1, first_line - 1),
                    byte_offset=0,
                    byte_length=first_byte,
                    content_hash=compute_content_hash(preamble_bytes),
                )
                symbols.insert(0, preamble)

    # Disambiguate overloaded symbols (same ID)
    symbols = _disambiguate_overloads(symbols)

    return symbols


def _walk_tree(
    node,
    spec: LanguageSpec,
    source_bytes: bytes,
    filename: str,
    language: str,
    symbols: list,
    parent_symbol: Optional[Symbol] = None
):
    """Recursively walk the AST and extract symbols."""
    # Check if this node is a symbol
    if node.type in spec.symbol_node_types:
        symbol = _extract_symbol(
            node, spec, source_bytes, filename, language, parent_symbol
        )
        if symbol:
            symbols.append(symbol)
            parent_symbol = symbol

    # Check for assigned functions (e.g., res.send = function() {}, const foo = () => {})
    assigned = _extract_assigned_function(
        node, spec, source_bytes, filename, language, parent_symbol
    )
    symbols.extend(assigned)

    # Check for route/middleware registrations
    route = _extract_route_registration(node, spec, source_bytes, filename, language)
    if route:
        symbols.append(route)

    # Check for constant/variable patterns (module-level assignments)
    if node.type in spec.constant_patterns and parent_symbol is None:
        const_result = _extract_constant(node, spec, source_bytes, filename, language)
        if const_result:
            if isinstance(const_result, list):
                symbols.extend(const_result)
            else:
                symbols.append(const_result)
    
    # Recurse into children
    for child in node.children:
        _walk_tree(child, spec, source_bytes, filename, language, symbols, parent_symbol)


def _extract_symbol(
    node,
    spec: LanguageSpec,
    source_bytes: bytes,
    filename: str,
    language: str,
    parent_symbol: Optional[Symbol] = None
) -> Optional[Symbol]:
    """Extract a Symbol from an AST node."""
    kind = spec.symbol_node_types[node.type]

    # Refine kind for languages where multiple constructs share a node type
    if node.type == "class_declaration" and spec.ts_language in ("kotlin", "swift"):
        first_keyword = node.children[0].type if node.children else ""
        # Check for modifiers wrapper (e.g., data class)
        if first_keyword == "modifiers" and len(node.children) > 1:
            first_keyword = node.children[1].type
        if first_keyword in ("interface", "protocol", "enum", "struct"):
            kind = "type"

    # Skip nodes with errors
    if node.has_error:
        return None

    # Extract name
    name = _extract_name(node, spec, source_bytes)
    if not name:
        return None

    # Build qualified name
    if parent_symbol:
        qualified_name = f"{parent_symbol.name}.{name}"
        kind = "method" if kind == "function" else kind
    else:
        qualified_name = name
    
    # Build signature
    signature = _build_signature(node, spec, source_bytes)
    
    # Extract docstring
    docstring = _extract_docstring(node, spec, source_bytes)
    
    # Extract decorators
    decorators = _extract_decorators(node, spec, source_bytes)
    
    # Compute content hash
    symbol_bytes = source_bytes[node.start_byte:node.end_byte]
    c_hash = compute_content_hash(symbol_bytes)

    # Create symbol
    symbol = Symbol(
        id=make_symbol_id(filename, qualified_name, kind),
        file=filename,
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        language=language,
        signature=signature,
        docstring=docstring,
        decorators=decorators,
        parent=parent_symbol.id if parent_symbol else None,
        line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        byte_offset=node.start_byte,
        byte_length=node.end_byte - node.start_byte,
        content_hash=c_hash,
    )
    
    return symbol


def _extract_name(node, spec: LanguageSpec, source_bytes: bytes) -> Optional[str]:
    """Extract the name from an AST node."""
    # Handle special cases first
    if node.type == "arrow_function":
        # Arrow functions get name from parent variable_declarator
        return None
    
    # Handle type_declaration in Go - name is in type_spec child
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                name_node = child.child_by_field_name("name")
                if name_node:
                    return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
        return None

    # C: function_definition has name inside declarator > function_declarator > declarator
    if node.type == "function_definition" and spec.ts_language == "c":
        decl = node.child_by_field_name("declarator")
        if decl and decl.type == "function_declarator":
            name_node = decl.child_by_field_name("declarator")
            if name_node:
                return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
        return None

    # C: type_definition - find the type_identifier child (the alias name, last one)
    if node.type == "type_definition" and spec.ts_language == "c":
        # The typedef name is the last type_identifier child
        last_name = None
        for child in node.children:
            if child.type == "type_identifier":
                last_name = source_bytes[child.start_byte:child.end_byte].decode("utf-8")
        return last_name

    # Rust: impl_item has no "name" field; the type being implemented is in the "type" field
    if node.type == "impl_item" and spec.ts_language == "rust":
        type_node = node.child_by_field_name("type")
        if type_node:
            return source_bytes[type_node.start_byte:type_node.end_byte].decode("utf-8")
        return None

    # Kotlin: uses child node types instead of field names
    if spec.ts_language == "kotlin":
        if node.type == "function_declaration":
            for child in node.children:
                if child.type == "simple_identifier":
                    return source_bytes[child.start_byte:child.end_byte].decode("utf-8")
            return None
        if node.type in ("class_declaration", "object_declaration"):
            for child in node.children:
                if child.type == "type_identifier":
                    return source_bytes[child.start_byte:child.end_byte].decode("utf-8")
            return None

    if node.type not in spec.name_fields:
        return None
    
    field_name = spec.name_fields[node.type]
    name_node = node.child_by_field_name(field_name)
    
    if name_node:
        return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
    
    return None


def _build_signature(node, spec: LanguageSpec, source_bytes: bytes) -> str:
    """Build a clean signature from AST node."""
    # Find the body child to determine where signature ends
    body = node.child_by_field_name("body")

    # Kotlin uses child types instead of field names for body
    if not body:
        for child in node.children:
            if child.type in ("function_body", "class_body", "enum_class_body"):
                body = child
                break

    if body:
        # Signature is from start of node to start of body
        end_byte = body.start_byte
    else:
        end_byte = node.end_byte
    
    sig_bytes = source_bytes[node.start_byte:end_byte]
    sig_text = sig_bytes.decode("utf-8").strip()
    
    # Clean up: remove trailing '{', ':', etc.
    sig_text = sig_text.rstrip("{: \n\t")
    
    return sig_text


def _extract_docstring(node, spec: LanguageSpec, source_bytes: bytes) -> str:
    """Extract docstring using language-specific strategy."""
    if spec.docstring_strategy == "next_sibling_string":
        return _extract_python_docstring(node, source_bytes)
    elif spec.docstring_strategy == "preceding_comment":
        return _extract_preceding_comments(node, source_bytes)
    return ""


def _extract_python_docstring(node, source_bytes: bytes) -> str:
    """Extract Python docstring from first statement in body."""
    body = node.child_by_field_name("body")
    if not body or body.child_count == 0:
        return ""
    
    # Find first expression_statement in body (function docstrings)
    for child in body.children:
        if child.type == "expression_statement":
            # Check if it's a string
            expr = child.child_by_field_name("expression")
            if expr and expr.type == "string":
                doc = source_bytes[expr.start_byte:expr.end_byte].decode("utf-8")
                return _strip_quotes(doc)
            # Handle tree-sitter-python 0.21+ string format
            if child.child_count > 0:
                first = child.children[0]
                if first.type in ("string", "concatenated_string"):
                    doc = source_bytes[first.start_byte:first.end_byte].decode("utf-8")
                    return _strip_quotes(doc)
        # Class docstrings are directly string nodes in the block
        elif child.type == "string":
            doc = source_bytes[child.start_byte:child.end_byte].decode("utf-8")
            return _strip_quotes(doc)
    
    return ""


def _strip_quotes(text: str) -> str:
    """Strip quotes from a docstring."""
    text = text.strip()
    if text.startswith('"""') and text.endswith('"""'):
        return text[3:-3].strip()
    if text.startswith("'''") and text.endswith("'''"):
        return text[3:-3].strip()
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1].strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].strip()
    return text


def _extract_preceding_comments(node, source_bytes: bytes) -> str:
    """Extract comments that immediately precede a node."""
    comments = []

    # Walk backwards through siblings
    prev = node.prev_named_sibling
    while prev and prev.type in ("comment", "line_comment", "block_comment"):
        comment_text = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8")
        comments.insert(0, comment_text)
        prev = prev.prev_named_sibling

    # Fallback: if no comments found and this is the first child of its parent,
    # check if the parent's preceding sibling is a comment (e.g., Ruby body_statement)
    if not comments and node.parent:
        parent = node.parent
        prev_of_parent = parent.prev_named_sibling
        while prev_of_parent and prev_of_parent.type in ("comment", "line_comment", "block_comment"):
            comment_text = source_bytes[prev_of_parent.start_byte:prev_of_parent.end_byte].decode("utf-8")
            comments.insert(0, comment_text)
            prev_of_parent = prev_of_parent.prev_named_sibling

    if not comments:
        return ""
    
    docstring = "\n".join(comments)
    return _clean_comment_markers(docstring)


def _clean_comment_markers(text: str) -> str:
    """Clean comment markers from docstring."""
    lines = text.split("\n")
    cleaned = []
    
    for line in lines:
        line = line.strip()
        # Remove leading comment markers
        if line.startswith("/**"):
            line = line[3:]
        elif line.startswith("/*"):
            line = line[2:]
        elif line.startswith("///"):
            line = line[3:]
        elif line.startswith("//!"):
            line = line[3:]
        elif line.startswith("//"):
            line = line[2:]
        elif line.startswith("#"):
            line = line[1:]
        elif line.startswith("*"):
            line = line[1:]
        
        # Remove trailing */
        if line.endswith("*/"):
            line = line[:-2]
        
        cleaned.append(line.strip())
    
    return "\n".join(cleaned).strip()


def _extract_decorators(node, spec: LanguageSpec, source_bytes: bytes) -> list[str]:
    """Extract decorators/attributes from a node."""
    if not spec.decorator_node_type:
        return []
    
    decorators = []
    
    # Walk backwards through siblings to find decorators
    prev = node.prev_named_sibling
    while prev and prev.type == spec.decorator_node_type:
        decorator_text = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8")
        decorators.insert(0, decorator_text.strip())
        prev = prev.prev_named_sibling
    
    return decorators


def _extract_route_registration(
    node,
    spec: LanguageSpec,
    source_bytes: bytes,
    filename: str,
    language: str,
) -> Optional[Symbol]:
    """Extract route/middleware registrations like app.get('/users', handler)."""
    if language not in ("javascript", "typescript"):
        return None

    # Look for call_expression nodes
    if node.type != "call_expression":
        return None

    func = node.child_by_field_name("function")
    if not func or func.type != "member_expression":
        return None

    # Get the method name (e.g., "get" from "app.get")
    prop = func.child_by_field_name("property")
    if not prop:
        return None
    method_name = source_bytes[prop.start_byte:prop.end_byte].decode("utf-8")

    if method_name not in _HTTP_METHODS:
        return None

    # Get the object (e.g., "app" from "app.get")
    obj = func.child_by_field_name("object")
    obj_name = source_bytes[obj.start_byte:obj.end_byte].decode("utf-8") if obj else ""

    # Get first argument (route path if it's a string)
    args = node.child_by_field_name("arguments")
    route_path = ""
    if args:
        for child in args.children:
            if child.type in ("string", "string_fragment", "template_string"):
                raw = source_bytes[child.start_byte:child.end_byte].decode("utf-8")
                route_path = raw.strip("'\"`")
                break

    # Build the symbol
    if route_path:
        display_name = f"{method_name.upper()} {route_path}"
        qualified_name = f"{obj_name}.{method_name}({route_path})"
    else:
        display_name = f"{obj_name}.{method_name}"
        qualified_name = f"{obj_name}.{method_name}"

    sig_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8").strip()
    # Truncate long registrations
    if len(sig_text) > 120:
        sig_text = sig_text[:120] + "..."

    symbol_bytes = source_bytes[node.start_byte:node.end_byte]
    c_hash = compute_content_hash(symbol_bytes)

    return Symbol(
        id=make_symbol_id(filename, qualified_name, "route"),
        file=filename,
        name=display_name,
        qualified_name=qualified_name,
        kind="route",
        language=language,
        signature=sig_text,
        docstring="",
        line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        byte_offset=node.start_byte,
        byte_length=node.end_byte - node.start_byte,
        content_hash=c_hash,
    )


def _extract_constant(
    node, spec: LanguageSpec, source_bytes: bytes, filename: str, language: str
) -> Optional[Symbol]:
    """Extract a constant or module-level variable."""
    # Python-style assignment (covers both constants and variables)
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left and left.type == "identifier":
            name = source_bytes[left.start_byte:left.end_byte].decode("utf-8")

            # Skip dunder names - they're Python internals
            if name.startswith("__") and name.endswith("__"):
                return None

            # Determine kind based on naming convention
            is_constant = name.isupper() or (len(name) > 1 and name[0].isupper() and "_" in name)
            kind = "constant" if is_constant else "variable"

            # Skip trivial variables (simple literals for non-constant names)
            right = node.child_by_field_name("right")
            if right and not is_constant:
                if right.type in ("string", "integer", "float", "true", "false", "none"):
                    return None

            sig = source_bytes[node.start_byte:node.end_byte].decode("utf-8").strip()
            const_bytes = source_bytes[node.start_byte:node.end_byte]
            c_hash = compute_content_hash(const_bytes)

            return Symbol(
                id=make_symbol_id(filename, name, kind),
                file=filename,
                name=name,
                qualified_name=name,
                kind=kind,
                language=language,
                signature=sig[:100],
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                byte_offset=node.start_byte,
                byte_length=node.end_byte - node.start_byte,
                content_hash=c_hash,
            )

    # JS/TS: lexical_declaration (const/let) and variable_declaration (var)
    if node.type in ("lexical_declaration", "variable_declaration"):
        results = []
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                value_node = child.child_by_field_name("value")
                if not name_node:
                    continue
                # Skip if value is a function (handled by _extract_assigned_function)
                if value_node and value_node.type in _FUNC_EXPR_TYPES:
                    continue
                name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                is_constant = name.isupper() or (len(name) > 1 and name[0].isupper() and "_" in name)
                kind = "constant" if is_constant else "variable"

                # Skip trivial variables
                if value_node:
                    value_type = value_node.type
                    # Skip require() results - already captured as imports
                    if value_type == "call_expression":
                        func_child = value_node.child_by_field_name("function")
                        if func_child and source_bytes[func_child.start_byte:func_child.end_byte].decode("utf-8") == "require":
                            continue
                    # Skip simple literals for non-constant names
                    if not is_constant and value_type in ("string", "number", "true", "false", "null", "undefined"):
                        continue

                sig = source_bytes[node.start_byte:node.end_byte].decode("utf-8").strip()
                const_bytes = source_bytes[node.start_byte:node.end_byte]
                c_hash = compute_content_hash(const_bytes)
                results.append(Symbol(
                    id=make_symbol_id(filename, name, kind),
                    file=filename,
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    language=language,
                    signature=sig[:100],
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    byte_offset=node.start_byte,
                    byte_length=node.end_byte - node.start_byte,
                    content_hash=c_hash,
                ))
        if results:
            return results

    return None


def _extract_assigned_function(
    node,
    spec: LanguageSpec,
    source_bytes: bytes,
    filename: str,
    language: str,
    parent_symbol: Optional[Symbol] = None,
) -> list[Symbol]:
    """Extract functions assigned to variables or properties.

    Captures patterns like:
    - res.send = function send(body) { ... }     (property assignment)
    - exports.foo = function() { ... }           (CommonJS export)
    - const handler = () => { ... }              (variable with arrow function)
    - const render = function(opts) { ... }      (variable with function expression)
    """
    func_node = None
    name = None
    alias_names = []

    # Pattern 1: expression_statement > assignment_expression where RHS is function
    # Also handles chained aliases like: res.contentType = res.type = function() {}
    if node.type == "expression_statement":
        for child in node.children:
            if child.type in ("assignment_expression", "assignment"):
                left = child.child_by_field_name("left")
                right = child.child_by_field_name("right")
                # Unwrap chained assignments (a = b = function() {})
                # Collect all assigned names, use the innermost left as primary
                names = []
                while right and right.type in ("assignment_expression", "assignment"):
                    if left:
                        names.append(
                            source_bytes[left.start_byte:left.end_byte].decode("utf-8")
                        )
                    left = right.child_by_field_name("left")
                    right = right.child_by_field_name("right")
                if right and right.type in _FUNC_EXPR_TYPES and left:
                    func_node = right
                    name = source_bytes[left.start_byte:left.end_byte].decode("utf-8")
                    # Store alias names for later (the outer names in the chain)
                    alias_names = names
                break  # only check first meaningful child

    # Pattern 2: variable declaration where value is a function
    elif node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                value = child.child_by_field_name("value")
                name_node = child.child_by_field_name("name")
                if value and value.type in _FUNC_EXPR_TYPES and name_node:
                    func_node = value
                    name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                break

    if not func_node or not name:
        return []

    # Classify the primary name (handle exports, prototypes, etc.)
    display_name, qualified_name, kind = _classify_assigned_name(name)

    # Build signature from the function node
    func_sig = _build_signature(func_node, spec, source_bytes)
    sig = f"{name} = {func_sig}" if func_sig else name

    # Extract docstring from preceding comments on the statement
    docstring = _extract_preceding_comments(node, source_bytes)

    symbol_bytes = source_bytes[node.start_byte:node.end_byte]
    c_hash = compute_content_hash(symbol_bytes)

    primary = Symbol(
        id=make_symbol_id(filename, qualified_name, kind),
        file=filename,
        name=display_name,
        qualified_name=qualified_name,
        kind=kind,
        language=language,
        signature=sig,
        docstring=docstring,
        parent=parent_symbol.id if parent_symbol else None,
        line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        byte_offset=node.start_byte,
        byte_length=node.end_byte - node.start_byte,
        content_hash=c_hash,
    )
    result = [primary]

    # Create alias symbols for chained assignments (e.g., res.set = res.header = function() {})
    # alias_names populated by chained assignment unwrapping above
    for alias in alias_names:
        a_display, a_qualified, a_kind = _classify_assigned_name(alias)
        alias_sig = f"{alias} → {qualified_name}"
        result.append(Symbol(
            id=make_symbol_id(filename, a_qualified, a_kind),
            file=filename,
            name=a_display,
            qualified_name=a_qualified,
            kind=a_kind,
            language=language,
            signature=alias_sig,
            docstring=f"Alias for {qualified_name}",
            parent=parent_symbol.id if parent_symbol else None,
            line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            byte_offset=node.start_byte,
            byte_length=node.end_byte - node.start_byte,
            content_hash=c_hash,
        ))

    return result


def _classify_assigned_name(full_name: str) -> tuple[str, str, str]:
    """Classify an assigned function name and return (display_name, qualified_name, kind).

    Handles:
    - Foo.prototype.bar → ("bar", "Foo.bar", "method")
    - module.exports.foo → ("foo", "foo", "function")
    - exports.foo → ("foo", "foo", "function")
    - res.send → ("send", "res.send", "method")
    - handler → ("handler", "handler", "function")
    """
    # Prototype pattern: Foo.prototype.bar
    if ".prototype." in full_name:
        parts = full_name.split(".prototype.")
        class_name = parts[0]
        method_name = parts[1] if len(parts) > 1 else full_name
        return method_name, f"{class_name}.{method_name}", "method"

    # CommonJS export patterns
    if full_name.startswith("module.exports."):
        short = full_name[len("module.exports."):]
        return short, short, "function"
    if full_name.startswith("exports."):
        short = full_name[len("exports."):]
        return short, short, "function"
    if full_name == "module.exports":
        return "module.exports", "module.exports", "function"

    # Other dotted names (e.g., res.send, app.use)
    if "." in full_name:
        short = full_name.split(".")[-1]
        return short, full_name, "method"

    # Simple name
    return full_name, full_name, "function"


def _disambiguate_overloads(symbols: list[Symbol]) -> list[Symbol]:
    """Append ordinal suffix to symbols with duplicate IDs.

    E.g., if two symbols have ID "file.py::foo#function", they become
    "file.py::foo#function~1" and "file.py::foo#function~2".
    """
    from collections import Counter

    id_counts = Counter(s.id for s in symbols)
    # Only process IDs that appear more than once
    duplicated = {sid for sid, count in id_counts.items() if count > 1}

    if not duplicated:
        return symbols

    # Track ordinals per duplicate ID and build old->new ID mapping
    ordinals: dict[str, int] = {}
    id_remap: dict[str, str] = {}  # old_id -> new_id (first occurrence only, for parent fixup)
    result = []
    for sym in symbols:
        if sym.id in duplicated:
            old_id = sym.id
            ordinals[old_id] = ordinals.get(old_id, 0) + 1
            sym.id = f"{old_id}~{ordinals[old_id]}"
            # Record the first occurrence's new ID so children can find their parent
            if old_id not in id_remap:
                id_remap[old_id] = sym.id
        result.append(sym)

    # Fix up parent references that point to now-renamed IDs
    for sym in result:
        if sym.parent and sym.parent in id_remap:
            sym.parent = id_remap[sym.parent]

    return result
