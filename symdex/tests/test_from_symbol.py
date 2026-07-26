"""Tests for from_symbol enrichment in call references."""


from firekeep_symdex.parser.references import extract_references


class TestFromSymbolPython:
    """Test from_symbol tracking in Python code."""

    def test_call_inside_function_gets_from_symbol(self):
        code = """\
def greet(name):
    print(name)
"""
        refs = extract_references(code, "app.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "print"
        assert calls[0]["from_symbol"] == "app.py::greet#function"

    def test_call_inside_method_gets_from_symbol(self):
        code = """\
class Greeter:
    def greet(self, name):
        print(name)
"""
        refs = extract_references(code, "app.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "print"
        assert calls[0]["from_symbol"] == "app.py::Greeter.greet#method"

    def test_call_at_module_level_has_none_from_symbol(self):
        code = """\
print("hello")
"""
        refs = extract_references(code, "app.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "print"
        assert calls[0]["from_symbol"] is None

    def test_imports_always_have_none_from_symbol(self):
        code = """\
import os

def foo():
    import sys
    os.path.join("a", "b")
"""
        refs = extract_references(code, "app.py", "python")
        imports = [r for r in refs if r["type"] == "import"]
        for imp in imports:
            assert imp["from_symbol"] is None

    def test_multiple_functions_get_correct_from_symbol(self):
        code = """\
def foo():
    bar()

def baz():
    qux()
"""
        refs = extract_references(code, "mod.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 2
        bar_call = next(c for c in calls if c["name"] == "bar")
        qux_call = next(c for c in calls if c["name"] == "qux")
        assert bar_call["from_symbol"] == "mod.py::foo#function"
        assert bar_call["from_symbol"] != qux_call["from_symbol"]
        assert qux_call["from_symbol"] == "mod.py::baz#function"

    def test_nested_calls_share_enclosing_symbol(self):
        code = """\
def process():
    result = transform(clean(data))
"""
        refs = extract_references(code, "pipe.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 2
        for call in calls:
            assert call["from_symbol"] == "pipe.py::process#function"

    def test_class_with_multiple_methods(self):
        code = """\
class Service:
    def start(self):
        self.connect()

    def stop(self):
        self.disconnect()
"""
        refs = extract_references(code, "svc.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        connect_call = next(c for c in calls if c["name"] == "self.connect")
        disconnect_call = next(c for c in calls if c["name"] == "self.disconnect")
        assert connect_call["from_symbol"] == "svc.py::Service.start#method"
        assert disconnect_call["from_symbol"] == "svc.py::Service.stop#method"


class TestFromSymbolJavaScript:
    """Test from_symbol tracking in JavaScript code."""

    def test_call_inside_function_declaration(self):
        code = """\
function greet(name) {
    console.log(name);
}
"""
        refs = extract_references(code, "app.js", "javascript")
        calls = [r for r in refs if r["type"] == "call"]
        # console.log is a call_expression with member access
        assert len(calls) >= 1
        for call in calls:
            assert call["from_symbol"] == "app.js::greet#function"

    def test_call_at_module_level_js(self):
        code = """\
console.log("hello");
"""
        refs = extract_references(code, "app.js", "javascript")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) >= 1
        for call in calls:
            assert call["from_symbol"] is None

    def test_method_inside_class(self):
        code = """\
class Greeter {
    greet(name) {
        console.log(name);
    }
}
"""
        refs = extract_references(code, "app.js", "javascript")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) >= 1
        for call in calls:
            assert call["from_symbol"] == "app.js::Greeter.greet#method"

    def test_imports_stay_none_js(self):
        code = """\
import express from 'express';

function handler() {
    express();
}
"""
        refs = extract_references(code, "app.js", "javascript")
        imports = [r for r in refs if r["type"] == "import"]
        assert len(imports) >= 1
        for imp in imports:
            assert imp["from_symbol"] is None


class TestFromSymbolEdgeCases:
    """Edge cases for from_symbol tracking."""

    def test_empty_file(self):
        refs = extract_references("", "empty.py", "python")
        assert refs == []

    def test_unknown_language(self):
        refs = extract_references("import foo", "file.xyz", "brainfuck")
        assert refs == []

    def test_function_with_no_calls(self):
        code = """\
def noop():
    pass
"""
        refs = extract_references(code, "app.py", "python")
        calls = [r for r in refs if r["type"] == "call"]
        assert len(calls) == 0
