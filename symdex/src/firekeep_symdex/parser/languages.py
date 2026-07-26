"""Language registry with LanguageSpec definitions for all supported languages."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LanguageSpec:
    """Specification for extracting symbols from a language's AST."""
    # tree-sitter language name (for tree-sitter-language-pack)
    ts_language: str

    # Node types that represent extractable symbols
    # Maps node_type -> symbol kind
    symbol_node_types: dict[str, str]

    # How to extract the symbol name from a node
    # Maps node_type -> child field name containing the name
    name_fields: dict[str, str]

    # How to extract parameters/signature beyond the name
    # Maps node_type -> child field name for parameters
    param_fields: dict[str, str]

    # Return type extraction (if language supports it)
    # Maps node_type -> child field name for return type
    return_type_fields: dict[str, str]

    # Docstring extraction strategy
    # "next_sibling_string" = Python (expression_statement after def)
    # "first_child_comment" = JS/TS (/** */ before function)
    # "preceding_comment" = Go/Rust/Java (// or /* */ before decl)
    docstring_strategy: str

    # Decorator/attribute node type (if any)
    decorator_node_type: Optional[str]

    # Node types that indicate nesting (methods inside classes)
    container_node_types: list[str]

    # Additional extraction: constants, type aliases
    constant_patterns: list[str]   # Node types for constants
    type_patterns: list[str]       # Node types for type definitions


# File extension to language mapping
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
}


# Python specification
PYTHON_SPEC = LanguageSpec(
    ts_language="python",
    symbol_node_types={
        "function_definition": "function",
        "class_definition": "class",
    },
    name_fields={
        "function_definition": "name",
        "class_definition": "name",
    },
    param_fields={
        "function_definition": "parameters",
    },
    return_type_fields={
        "function_definition": "return_type",
    },
    docstring_strategy="next_sibling_string",
    decorator_node_type="decorator",
    container_node_types=["class_definition"],
    constant_patterns=["assignment"],
    type_patterns=["type_alias_statement"],
)


# JavaScript specification
JAVASCRIPT_SPEC = LanguageSpec(
    ts_language="javascript",
    symbol_node_types={
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
        "generator_function_declaration": "function",
    },
    name_fields={
        "function_declaration": "name",
        "class_declaration": "name",
        "method_definition": "name",
        "generator_function_declaration": "name",
    },
    param_fields={
        "function_declaration": "parameters",
        "method_definition": "parameters",
        "arrow_function": "parameters",
        "generator_function_declaration": "parameters",
    },
    return_type_fields={},
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=["class_declaration", "class"],
    constant_patterns=["lexical_declaration", "variable_declaration"],
    type_patterns=[],
)


# TypeScript specification
TYPESCRIPT_SPEC = LanguageSpec(
    ts_language="typescript",
    symbol_node_types={
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "arrow_function": "function",
        "interface_declaration": "type",
        "type_alias_declaration": "type",
        "enum_declaration": "type",
    },
    name_fields={
        "function_declaration": "name",
        "class_declaration": "name",
        "method_definition": "name",
        "interface_declaration": "name",
        "type_alias_declaration": "name",
        "enum_declaration": "name",
    },
    param_fields={
        "function_declaration": "parameters",
        "method_definition": "parameters",
        "arrow_function": "parameters",
    },
    return_type_fields={
        "function_declaration": "return_type",
        "method_definition": "return_type",
        "arrow_function": "return_type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type="decorator",
    container_node_types=["class_declaration", "class"],
    constant_patterns=["lexical_declaration", "variable_declaration"],
    type_patterns=["interface_declaration", "type_alias_declaration", "enum_declaration"],
)


# Go specification
GO_SPEC = LanguageSpec(
    ts_language="go",
    symbol_node_types={
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    name_fields={
        "function_declaration": "name",
        "method_declaration": "name",
        "type_declaration": "name",
    },
    param_fields={
        "function_declaration": "parameters",
        "method_declaration": "parameters",
    },
    return_type_fields={
        "function_declaration": "result",
        "method_declaration": "result",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=[],
    constant_patterns=["const_declaration"],
    type_patterns=["type_declaration"],
)


# Rust specification
RUST_SPEC = LanguageSpec(
    ts_language="rust",
    symbol_node_types={
        "function_item": "function",
        "struct_item": "type",
        "enum_item": "type",
        "trait_item": "type",
        "impl_item": "class",
        "type_item": "type",
    },
    name_fields={
        "function_item": "name",
        "struct_item": "name",
        "enum_item": "name",
        "trait_item": "name",
        "type_item": "name",
    },
    param_fields={
        "function_item": "parameters",
    },
    return_type_fields={
        "function_item": "return_type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type="attribute_item",
    container_node_types=["impl_item", "trait_item"],
    constant_patterns=["const_item", "static_item"],
    type_patterns=["struct_item", "enum_item", "trait_item", "type_item"],
)


# Java specification
JAVA_SPEC = LanguageSpec(
    ts_language="java",
    symbol_node_types={
        "method_declaration": "method",
        "constructor_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "type",
        "enum_declaration": "type",
    },
    name_fields={
        "method_declaration": "name",
        "constructor_declaration": "name",
        "class_declaration": "name",
        "interface_declaration": "name",
        "enum_declaration": "name",
    },
    param_fields={
        "method_declaration": "parameters",
        "constructor_declaration": "parameters",
    },
    return_type_fields={
        "method_declaration": "type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type="marker_annotation",
    container_node_types=["class_declaration", "interface_declaration", "enum_declaration"],
    constant_patterns=["field_declaration"],
    type_patterns=["interface_declaration", "enum_declaration"],
)


# PHP specification
PHP_SPEC = LanguageSpec(
    ts_language="php",
    symbol_node_types={
        "function_definition": "function",
        "class_declaration": "class",
        "method_declaration": "method",
        "interface_declaration": "type",
        "trait_declaration": "type",
        "enum_declaration": "type",
    },
    name_fields={
        "function_definition": "name",
        "class_declaration": "name",
        "method_declaration": "name",
        "interface_declaration": "name",
        "trait_declaration": "name",
        "enum_declaration": "name",
    },
    param_fields={
        "function_definition": "parameters",
        "method_declaration": "parameters",
    },
    return_type_fields={
        "function_definition": "return_type",
        "method_declaration": "return_type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type="attribute",  # PHP 8 #[Attribute] syntax
    container_node_types=["class_declaration", "trait_declaration", "interface_declaration"],
    constant_patterns=["const_declaration"],
    type_patterns=["interface_declaration", "trait_declaration", "enum_declaration"],
)


# C specification
C_SPEC = LanguageSpec(
    ts_language="c",
    symbol_node_types={
        "function_definition": "function",
        "struct_specifier": "type",
        "enum_specifier": "type",
        "type_definition": "type",
    },
    name_fields={
        # function_definition uses nested declarator; handled in _extract_name
        "struct_specifier": "name",
        "enum_specifier": "name",
    },
    param_fields={
        # Parameters are inside the nested function_declarator
    },
    return_type_fields={
        "function_definition": "type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=[],
    constant_patterns=[],
    type_patterns=["struct_specifier", "enum_specifier", "type_definition"],
)


# C# specification
CSHARP_SPEC = LanguageSpec(
    ts_language="csharp",
    symbol_node_types={
        "method_declaration": "method",
        "constructor_declaration": "method",
        "class_declaration": "class",
        "struct_declaration": "type",
        "interface_declaration": "type",
        "enum_declaration": "type",
    },
    name_fields={
        "method_declaration": "name",
        "constructor_declaration": "name",
        "class_declaration": "name",
        "struct_declaration": "name",
        "interface_declaration": "name",
        "enum_declaration": "name",
    },
    param_fields={
        "method_declaration": "parameters",
        "constructor_declaration": "parameters",
    },
    return_type_fields={
        "method_declaration": "returns",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=["class_declaration", "struct_declaration", "interface_declaration"],
    constant_patterns=["field_declaration"],
    type_patterns=["interface_declaration", "enum_declaration", "struct_declaration"],
)


# Ruby specification
RUBY_SPEC = LanguageSpec(
    ts_language="ruby",
    symbol_node_types={
        "method": "function",
        "singleton_method": "function",
        "class": "class",
        "module": "class",
    },
    name_fields={
        "method": "name",
        "singleton_method": "name",
        "class": "name",
        "module": "name",
    },
    param_fields={
        "method": "parameters",
        "singleton_method": "parameters",
    },
    return_type_fields={},
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=["class", "module"],
    constant_patterns=[],
    type_patterns=[],
)


# Kotlin specification
KOTLIN_SPEC = LanguageSpec(
    ts_language="kotlin",
    symbol_node_types={
        "function_declaration": "function",
        "class_declaration": "class",
        "object_declaration": "class",
    },
    name_fields={
        # Kotlin uses child types, not field names; handled in _extract_name
    },
    param_fields={},
    return_type_fields={},
    docstring_strategy="preceding_comment",
    decorator_node_type="annotation",
    container_node_types=["class_declaration", "object_declaration"],
    constant_patterns=[],
    type_patterns=["class_declaration"],
)


# Swift specification
SWIFT_SPEC = LanguageSpec(
    ts_language="swift",
    symbol_node_types={
        "function_declaration": "function",
        "class_declaration": "class",
        "struct_declaration": "type",
        "enum_declaration": "type",
        "protocol_declaration": "type",
    },
    name_fields={
        "function_declaration": "name",
        "class_declaration": "name",
        "struct_declaration": "name",
        "enum_declaration": "name",
        "protocol_declaration": "name",
    },
    param_fields={},
    return_type_fields={
        "function_declaration": "return_type",
    },
    docstring_strategy="preceding_comment",
    decorator_node_type=None,
    container_node_types=["class_declaration", "struct_declaration", "enum_declaration", "protocol_declaration"],
    constant_patterns=[],
    type_patterns=["class_declaration", "struct_declaration", "enum_declaration", "protocol_declaration"],
)


# Language registry
LANGUAGE_REGISTRY = {
    "python": PYTHON_SPEC,
    "javascript": JAVASCRIPT_SPEC,
    "typescript": TYPESCRIPT_SPEC,
    "go": GO_SPEC,
    "rust": RUST_SPEC,
    "java": JAVA_SPEC,
    "php": PHP_SPEC,
    "c": C_SPEC,
    "csharp": CSHARP_SPEC,
    "ruby": RUBY_SPEC,
    "kotlin": KOTLIN_SPEC,
    "swift": SWIFT_SPEC,
}


_plugins_loaded = False


def _ensure_plugins_loaded():
    """Lazily load custom language plugins on first access."""
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    load_custom_languages()


def load_custom_languages():
    """Load custom language specs from JSON files in ~/.code-index/languages/.

    Also checks the CODE_INDEX_PATH environment variable for an alternative base path.
    Invalid specs emit warnings but do not crash.
    """
    import json
    import os
    import warnings
    from pathlib import Path

    base_path = os.environ.get("CODE_INDEX_PATH")
    if base_path:
        lang_dir = Path(base_path) / "languages"
    else:
        lang_dir = Path.home() / ".code-index" / "languages"

    if not lang_dir.is_dir():
        return

    required_fields = {
        "name", "extensions", "ts_language", "symbol_node_types",
        "name_fields", "param_fields", "return_type_fields",
        "docstring_strategy", "container_node_types",
        "constant_patterns", "type_patterns",
    }

    for json_file in lang_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            warnings.warn(f"Failed to read language plugin {json_file.name}: {e}")
            continue

        missing = required_fields - set(data.keys())
        if missing:
            warnings.warn(
                f"Language plugin {json_file.name} missing fields: {', '.join(sorted(missing))}"
            )
            continue

        try:
            spec = LanguageSpec(
                ts_language=data["ts_language"],
                symbol_node_types=data["symbol_node_types"],
                name_fields=data["name_fields"],
                param_fields=data["param_fields"],
                return_type_fields=data["return_type_fields"],
                docstring_strategy=data["docstring_strategy"],
                decorator_node_type=data.get("decorator_node_type"),
                container_node_types=data["container_node_types"],
                constant_patterns=data["constant_patterns"],
                type_patterns=data["type_patterns"],
            )
        except (TypeError, KeyError) as e:
            warnings.warn(f"Invalid language spec in {json_file.name}: {e}")
            continue

        lang_name = data["name"]
        LANGUAGE_REGISTRY[lang_name] = spec

        for ext in data["extensions"]:
            LANGUAGE_EXTENSIONS[ext] = lang_name
