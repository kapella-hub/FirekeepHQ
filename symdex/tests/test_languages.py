"""Tests for language-specific parsing."""

from firekeep_symdex.parser import parse_file


JAVASCRIPT_SOURCE = '''
/** Greet a user. */
function greet(name) {
    return `Hello, ${name}!`;
}

class Calculator {
    /** Add two numbers. */
    add(a, b) {
        return a + b;
    }
}

const MAX_RETRY = 5;
'''


def test_parse_javascript():
    """Test JavaScript parsing."""
    symbols = parse_file(JAVASCRIPT_SOURCE, "app.js", "javascript")
    
    # Should have function, class, method, constant
    func = next((s for s in symbols if s.name == "greet"), None)
    assert func is not None
    assert func.kind == "function"
    assert "Greet a user" in func.docstring
    
    cls = next((s for s in symbols if s.name == "Calculator"), None)
    assert cls is not None
    assert cls.kind == "class"
    
    method = next((s for s in symbols if s.name == "add"), None)
    assert method is not None
    assert method.kind == "method"


TYPESCRIPT_SOURCE = '''
interface User {
    name: string;
}

/** Get user by ID. */
function getUser(id: number): User {
    return { name: "Test" };
}

class UserService {
    private users: User[] = [];
    
    @cache()
    findById(id: number): User | undefined {
        return this.users.find(u => u.id === id);
    }
}

type ID = string | number;
'''


def test_parse_typescript():
    """Test TypeScript parsing."""
    symbols = parse_file(TYPESCRIPT_SOURCE, "service.ts", "typescript")
    
    # Should have interface, function, class, method, type alias
    func = next((s for s in symbols if s.name == "getUser"), None)
    assert func is not None
    assert func.kind == "function"
    
    interface = next((s for s in symbols if s.name == "User"), None)
    assert interface is not None
    assert interface.kind == "type"


GO_SOURCE = '''
package main

import "fmt"

// Person represents a person.
type Person struct {
    Name string
}

// Greet prints a greeting.
func (p *Person) Greet() {
    fmt.Println("Hello, " + p.Name)
}

// Add adds two numbers.
func Add(a, b int) int {
    return a + b
}

const MaxCount = 100
'''


def test_parse_go():
    """Test Go parsing."""
    symbols = parse_file(GO_SOURCE, "main.go", "go")
    
    # Should have type, method, function, constant
    person = next((s for s in symbols if s.name == "Person"), None)
    assert person is not None
    assert person.kind == "type"
    
    greet = next((s for s in symbols if s.name == "Greet"), None)
    assert greet is not None
    assert greet.kind == "method"


RUST_SOURCE = '''
/// A user in the system.
pub struct User {
    name: String,
}

impl User {
    /// Create a new user.
    pub fn new(name: &str) -> Self {
        Self { name: name.to_string() }
    }
    
    /// Get the user's name.
    pub fn name(&self) -> &str {
        &self.name
    }
}

pub const MAX_USERS: usize = 1000;
'''


def test_parse_rust():
    """Test Rust parsing."""
    symbols = parse_file(RUST_SOURCE, "user.rs", "rust")
    
    # Should have struct, impl, methods, constant
    user = next((s for s in symbols if s.name == "User"), None)
    assert user is not None
    assert user.kind == "type"


JAVA_SOURCE = '''
/**
 * A simple calculator.
 */
public class Calculator {
    public static final int MAX_VALUE = 100;
    
    /**
     * Add two numbers.
     */
    public int add(int a, int b) {
        return a + b;
    }
}

interface Operable {
    int operate(int a, int b);
}
'''


def test_parse_java():
    """Test Java parsing."""
    symbols = parse_file(JAVA_SOURCE, "Calculator.java", "java")

    # Should have class, method, interface
    calc = next((s for s in symbols if s.name == "Calculator"), None)
    assert calc is not None
    assert calc.kind == "class"

    add = next((s for s in symbols if s.name == "add"), None)
    assert add is not None
    assert add.kind == "method"


PHP_SOURCE = '''<?php

const MAX_RETRIES = 3;

/**
 * Authenticate a user token.
 */
function authenticate(string $token): bool
{
    return strlen($token) > 0;
}

/**
 * Manages user operations.
 */
class UserService
{
    /**
     * Get a user by ID.
     */
    public function getUser(int $userId): array
    {
        return ['id' => $userId];
    }
}

interface Authenticatable
{
    public function authenticate(string $token): bool;
}

trait Timestampable
{
    public function getCreatedAt(): string
    {
        return date(\'Y-m-d\');
    }
}

enum Status
{
    case Active;
    case Inactive;
}
'''


def test_parse_php():
    """Test PHP parsing."""
    symbols = parse_file(PHP_SOURCE, "service.php", "php")

    func = next((s for s in symbols if s.name == "authenticate"), None)
    assert func is not None
    assert func.kind == "function"
    assert "Authenticate a user token" in func.docstring

    cls = next((s for s in symbols if s.name == "UserService"), None)
    assert cls is not None
    assert cls.kind == "class"

    method = next((s for s in symbols if s.name == "getUser"), None)
    assert method is not None
    assert method.kind == "method"
    assert "Get a user by ID" in method.docstring

    interface = next((s for s in symbols if s.name == "Authenticatable"), None)
    assert interface is not None
    assert interface.kind == "type"

    trait = next((s for s in symbols if s.name == "Timestampable"), None)
    assert trait is not None
    assert trait.kind == "type"

    enum = next((s for s in symbols if s.name == "Status"), None)
    assert enum is not None
    assert enum.kind == "type"


# ---- Assigned function extraction tests ----

JS_ASSIGNED_FUNCTIONS = '''
/** Set status code. */
res.status = function status(code) {
  this.statusCode = code;
  return this;
};

const handler = (req, res) => {
  res.json({ ok: true });
};

var render = function render(view, opts) {
  return view;
};

exports.createApp = function createApp() {
  return {};
};

module.exports.init = function init(config) {
  return config;
};

Foo.prototype.bar = function bar(x) {
  return x;
};

/** Both names point to the same function. */
res.contentType =
res.type = function contentType(type) {
  return type;
};
'''


def test_parse_js_property_assignments():
    """Test that property assignment functions are captured."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    status = next((s for s in symbols if s.name == "status"), None)
    assert status is not None
    assert status.kind == "method"
    assert status.qualified_name == "res.status"
    assert "Set status code" in status.docstring


def test_parse_js_variable_arrow_function():
    """Test that arrow functions in variable declarations are captured."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    handler = next((s for s in symbols if s.name == "handler"), None)
    assert handler is not None
    assert handler.kind == "function"
    assert "handler" in handler.signature


def test_parse_js_variable_function_expression():
    """Test that function expressions in variable declarations are captured."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    render = next((s for s in symbols if s.name == "render"), None)
    assert render is not None
    assert render.kind == "function"


def test_parse_js_commonjs_exports():
    """Test that CommonJS export functions are captured."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    create = next((s for s in symbols if s.name == "createApp"), None)
    assert create is not None
    assert create.kind == "function"

    init = next((s for s in symbols if s.name == "init"), None)
    assert init is not None
    assert init.kind == "function"


def test_parse_js_prototype_assignment():
    """Test that prototype method assignments are captured."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    bar = next((s for s in symbols if s.name == "bar"), None)
    assert bar is not None
    assert bar.kind == "method"
    assert bar.qualified_name == "Foo.bar"


def test_parse_js_chained_alias_assignment():
    """Test chained assignments like res.contentType = res.type = function()."""
    symbols = parse_file(JS_ASSIGNED_FUNCTIONS, "test.js", "javascript")

    # Primary name (innermost assignment target)
    typ = next((s for s in symbols if s.name == "type"), None)
    assert typ is not None
    assert typ.kind == "method"
    assert "Both names" in typ.docstring

    # Alias (outer assignment target)
    ct = next((s for s in symbols if s.name == "contentType"), None)
    assert ct is not None
    assert "Alias for" in ct.docstring


# ---- File preamble tests ----

PYTHON_WITH_PREAMBLE = '''
"""Module for user management."""

import os
from typing import Optional

MAX_USERS = 100

def get_user(user_id: int) -> Optional[dict]:
    """Get a user by ID."""
    return None
'''


def test_file_preamble_captured():
    """Test that file preamble (imports, module docstring) is captured."""
    symbols = parse_file(PYTHON_WITH_PREAMBLE, "users.py", "python")

    preamble = next((s for s in symbols if s.kind == "module"), None)
    assert preamble is not None
    assert preamble.name == "__preamble__"
    assert preamble.line == 1
    assert preamble.byte_offset == 0
    assert preamble.byte_length > 0
    assert preamble.signature == "# users.py"


def test_no_preamble_when_symbol_at_start():
    """Test that no preamble is created when the first symbol starts at byte 0."""
    source = 'def foo(): pass'
    symbols = parse_file(source, "no_preamble.py", "python")

    preamble = next((s for s in symbols if s.kind == "module"), None)
    assert preamble is None


def test_no_preamble_for_whitespace_only():
    """Test that no preamble is created when only whitespace precedes first symbol."""
    source = '\n\n\ndef foo(): pass'
    symbols = parse_file(source, "ws_only.py", "python")

    preamble = next((s for s in symbols if s.kind == "module"), None)
    assert preamble is None


# ---- Module-level variable extraction tests ----

PYTHON_VARIABLES = '''
app = Flask(__name__)
config = {"debug": True}
MAX_SIZE = 100

def main():
    pass
'''


def test_parse_python_module_variables():
    """Test that module-level variables are captured."""
    symbols = parse_file(PYTHON_VARIABLES, "app.py", "python")

    app = next((s for s in symbols if s.name == "app"), None)
    assert app is not None
    assert app.kind == "variable"

    config = next((s for s in symbols if s.name == "config"), None)
    assert config is not None
    assert config.kind == "variable"

    max_size = next((s for s in symbols if s.name == "MAX_SIZE"), None)
    assert max_size is not None
    assert max_size.kind == "constant"


PYTHON_DUNDER_SKIP = '''
__all__ = ["foo", "bar"]
__version__ = "1.0.0"

def foo():
    pass
'''


def test_python_dunder_variables_skipped():
    """Test that dunder variables like __all__ and __version__ are not captured."""
    symbols = parse_file(PYTHON_DUNDER_SKIP, "mod.py", "python")

    dunder = [s for s in symbols if s.name.startswith("__") and s.name.endswith("__") and s.kind in ("variable", "constant")]
    assert len(dunder) == 0


PYTHON_INNER_ASSIGNMENT = '''
def setup():
    db = connect()
    return db
'''


def test_python_inner_assignments_not_captured():
    """Test that assignments inside functions are NOT captured as variables."""
    symbols = parse_file(PYTHON_INNER_ASSIGNMENT, "inner.py", "python")

    db = next((s for s in symbols if s.name == "db"), None)
    assert db is None


JS_VARIABLES = '''
const API_URL = "https://api.example.com";
const config = { debug: true };
let counter = 0;

function main() {}
'''


def test_parse_js_module_variables():
    """Test that JS module-level variables are captured (non-trivial ones)."""
    symbols = parse_file(JS_VARIABLES, "config.js", "javascript")

    api_url = next((s for s in symbols if s.name == "API_URL"), None)
    assert api_url is not None
    assert api_url.kind == "constant"

    config = next((s for s in symbols if s.name == "config"), None)
    assert config is not None
    assert config.kind == "variable"

    # Simple literal with lowercase name is now filtered as trivial
    counter = next((s for s in symbols if s.name == "counter"), None)
    assert counter is None


# ---- C parsing tests ----

C_SOURCE = '''
// Add two numbers.
int add(int a, int b) {
    return a + b;
}

struct Point {
    int x;
    int y;
};

enum Color { RED, GREEN, BLUE };

typedef unsigned long ulong;
'''


def test_parse_c():
    """Test C parsing."""
    symbols = parse_file(C_SOURCE, "math.c", "c")

    add = next((s for s in symbols if s.name == "add"), None)
    assert add is not None
    assert add.kind == "function"
    assert "Add two numbers" in add.docstring

    point = next((s for s in symbols if s.name == "Point"), None)
    assert point is not None
    assert point.kind == "type"

    color = next((s for s in symbols if s.name == "Color"), None)
    assert color is not None
    assert color.kind == "type"

    ulong = next((s for s in symbols if s.name == "ulong"), None)
    assert ulong is not None
    assert ulong.kind == "type"


# ---- C# parsing tests ----

CSHARP_SOURCE = '''
using System;

/// A simple calculator.
public class Calculator {
    /// Add two numbers.
    public int Add(int a, int b) {
        return a + b;
    }
}

public interface ICalculator {
    int Calculate(int a, int b);
}

public struct Point {
    public int X;
    public int Y;
}

public enum Color {
    Red,
    Green,
    Blue
}
'''


def test_parse_csharp():
    """Test C# parsing."""
    symbols = parse_file(CSHARP_SOURCE, "Calc.cs", "csharp")

    calc = next((s for s in symbols if s.name == "Calculator"), None)
    assert calc is not None
    assert calc.kind == "class"
    assert "A simple calculator" in calc.docstring

    add = next((s for s in symbols if s.name == "Add"), None)
    assert add is not None
    assert add.kind == "method"
    assert "Add two numbers" in add.docstring

    icalc = next((s for s in symbols if s.name == "ICalculator"), None)
    assert icalc is not None
    assert icalc.kind == "type"

    point = next((s for s in symbols if s.name == "Point"), None)
    assert point is not None
    assert point.kind == "type"

    color = next((s for s in symbols if s.name == "Color"), None)
    assert color is not None
    assert color.kind == "type"


# ---- Ruby parsing tests ----

RUBY_SOURCE = '''
# Add two numbers.
def add(a, b)
  a + b
end

class Calculator
  # Multiply two numbers.
  def multiply(a, b)
    a * b
  end
end

module MathUtils
  def self.subtract(a, b)
    a - b
  end
end
'''


def test_parse_ruby():
    """Test Ruby parsing."""
    symbols = parse_file(RUBY_SOURCE, "math.rb", "ruby")

    add = next((s for s in symbols if s.name == "add"), None)
    assert add is not None
    assert add.kind == "function"
    assert "Add two numbers" in add.docstring

    calc = next((s for s in symbols if s.name == "Calculator"), None)
    assert calc is not None
    assert calc.kind == "class"

    multiply = next((s for s in symbols if s.name == "multiply"), None)
    assert multiply is not None
    assert multiply.kind == "method"
    assert "Multiply two numbers" in multiply.docstring

    math_utils = next((s for s in symbols if s.name == "MathUtils"), None)
    assert math_utils is not None
    assert math_utils.kind == "class"


# ---- Kotlin parsing tests ----

KOTLIN_SOURCE = '''
// Add two numbers.
fun add(a: Int, b: Int): Int {
    return a + b
}

class Calculator {
    // Multiply two numbers.
    fun multiply(a: Int, b: Int): Int {
        return a * b
    }
}

interface Operable {
    fun operate(a: Int, b: Int): Int
}

data class Point(val x: Int, val y: Int)

object Singleton {
    fun getInstance(): Singleton = this
}

enum class Color { RED, GREEN, BLUE }
'''


def test_parse_kotlin():
    """Test Kotlin parsing."""
    symbols = parse_file(KOTLIN_SOURCE, "math.kt", "kotlin")

    add = next((s for s in symbols if s.name == "add"), None)
    assert add is not None
    assert add.kind == "function"
    assert "Add two numbers" in add.docstring

    calc = next((s for s in symbols if s.name == "Calculator"), None)
    assert calc is not None
    assert calc.kind == "class"

    multiply = next((s for s in symbols if s.name == "multiply"), None)
    assert multiply is not None
    assert multiply.kind == "method"

    operable = next((s for s in symbols if s.name == "Operable"), None)
    assert operable is not None
    assert operable.kind == "type"

    point = next((s for s in symbols if s.name == "Point"), None)
    assert point is not None
    assert point.kind == "class"

    singleton = next((s for s in symbols if s.name == "Singleton"), None)
    assert singleton is not None
    assert singleton.kind == "class"

    color = next((s for s in symbols if s.name == "Color"), None)
    assert color is not None
    assert color.kind == "type"


# ---- Swift parsing tests ----

SWIFT_SOURCE = '''
/// Add two numbers.
func add(a: Int, b: Int) -> Int {
    return a + b
}

class Calculator {
    /// Multiply two numbers.
    func multiply(a: Int, b: Int) -> Int {
        return a * b
    }
}

struct Point {
    var x: Int
    var y: Int
}

protocol Operable {
    func operate(a: Int, b: Int) -> Int
}

enum Color {
    case red, green, blue
}
'''


def test_parse_swift():
    """Test Swift parsing."""
    symbols = parse_file(SWIFT_SOURCE, "math.swift", "swift")

    add = next((s for s in symbols if s.name == "add"), None)
    assert add is not None
    assert add.kind == "function"
    assert "Add two numbers" in add.docstring

    calc = next((s for s in symbols if s.name == "Calculator"), None)
    assert calc is not None
    assert calc.kind == "class"

    multiply = next((s for s in symbols if s.name == "multiply"), None)
    assert multiply is not None
    assert multiply.kind == "method"
    assert "Multiply two numbers" in multiply.docstring

    point = next((s for s in symbols if s.name == "Point"), None)
    assert point is not None
    assert point.kind == "type"

    operable = next((s for s in symbols if s.name == "Operable"), None)
    assert operable is not None
    assert operable.kind == "type"

    color = next((s for s in symbols if s.name == "Color"), None)
    assert color is not None
    assert color.kind == "type"


# ---- Route registration extraction tests ----

JS_ROUTES = '''
var express = require('express');
var app = express();

app.get('/users', function(req, res) {
    res.json([]);
});

app.post('/login', authenticate);

app.use(cors());

app.listen(3000);
'''


def test_parse_js_route_registrations():
    """Test that route registrations are captured."""
    symbols = parse_file(JS_ROUTES, "app.js", "javascript")

    routes = [s for s in symbols if s.kind == "route"]
    assert len(routes) >= 3  # get, post, use

    get_route = next((s for s in routes if "/users" in s.name), None)
    assert get_route is not None
    assert "GET" in get_route.name

    post_route = next((s for s in routes if "/login" in s.name), None)
    assert post_route is not None
    assert "POST" in post_route.name


def test_trivial_variables_filtered():
    """Test that trivial variables (require results, simple literals) are filtered."""
    source = '''
var path = require('path');
var express = require('express');
var count = 0;
var name = "hello";
var config = { debug: true, port: 3000 };
const API_URL = "https://api.example.com";
'''
    symbols = parse_file(source, "app.js", "javascript")

    names = {s.name for s in symbols}
    # require results should be filtered
    assert "path" not in names
    assert "express" not in names
    # Simple literals should be filtered
    assert "count" not in names
    assert "name" not in names
    # Object literals should be kept (they're meaningful config)
    assert "config" in names
    # Constants should always be kept
    assert "API_URL" in names


def test_python_trivial_variables_filtered():
    """Test that trivial Python variables (simple literals) are filtered."""
    source = '''
count = 0
name = "hello"
flag = True
config = {"debug": True}
MAX_SIZE = 100
'''
    symbols = parse_file(source, "app.py", "python")

    names = {s.name for s in symbols}
    # Simple literals with lowercase names should be filtered
    assert "count" not in names
    assert "name" not in names
    assert "flag" not in names
    # Dict literals should be kept
    assert "config" in names
    # Constants should always be kept
    assert "MAX_SIZE" in names

