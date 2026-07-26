"""Generate code scaffold matching codebase conventions."""

import os
import re
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo
from .extract_conventions import extract_conventions


def _detect_ai_provider():
    """Detect available AI provider using the same logic as the summarizer module.

    Priority: Anthropic > Google Gemini > OpenAI/Local.
    Returns (provider_name, client) or (None, None).
    """
    # 1. Anthropic
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            return "anthropic", client
        except ImportError:
            pass

    # 2. Google Gemini
    if os.environ.get("GOOGLE_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
            client = genai.GenerativeModel("gemini-1.5-flash")
            return "gemini", client
        except ImportError:
            pass

    # 3. OpenAI-compatible (local LLM)
    if os.environ.get("OPENAI_API_BASE"):
        try:
            import httpx
            api_base = os.environ["OPENAI_API_BASE"].rstrip("/")
            api_key = os.environ.get("OPENAI_API_KEY", "local-llm")
            client = httpx.Client(
                timeout=float(os.environ.get("OPENAI_TIMEOUT", "60.0")),
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return "openai", (client, api_base, os.environ.get("OPENAI_MODEL", "qwen3-coder"))
        except ImportError:
            pass

    return None, None


def _generate_with_anthropic(client, prompt: str) -> str:
    """Generate scaffold using Anthropic API."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _generate_with_gemini(client, prompt: str) -> str:
    """Generate scaffold using Gemini API."""
    response = client.generate_content(prompt)
    return response.text


def _generate_with_openai(client_tuple, prompt: str) -> str:
    """Generate scaffold using OpenAI-compatible API."""
    client, api_base, model = client_tuple
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.0,
    }
    response = client.post(f"{api_base}/chat/completions", json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _build_scaffold_prompt(intent: str, conventions: dict, template_source: str, target_kind: str) -> str:
    """Build the prompt for AI-assisted scaffolding."""
    parts = [
        "Generate a code scaffold for the following intent. Match the codebase conventions exactly.",
        "",
        f"Intent: {intent}",
        f"Symbol kind: {target_kind}",
        "",
    ]

    if conventions.get("naming"):
        parts.append(f"Naming conventions: {conventions['naming']}")
    if conventions.get("framework", {}).get("detected", "none detected") != "none detected":
        parts.append(f"Framework: {conventions['framework']['detected']}")

    if template_source:
        parts.extend([
            "",
            "Template (match this style):",
            template_source[:1000],
        ])

    parts.extend([
        "",
        "Output ONLY the code scaffold, no explanation. Include TODO comments for parts that need implementation.",
    ])

    return "\n".join(parts)


def _template_fallback(intent: str, template_sym: Optional[dict], conventions: dict, target_kind: str) -> str:
    """Generate scaffold without AI by copying template structure."""
    # Extract keywords from intent for naming
    words = re.findall(r"[a-z]+", intent.lower())
    words = [w for w in words if w not in ("a", "an", "the", "to", "for", "and", "or", "in", "of", "with")]

    # Determine naming convention
    naming = conventions.get("naming", {})
    func_convention = naming.get("functions", "snake_case")

    if "snake_case" in func_convention:
        func_name = "_".join(words[:3]) if words else "new_function"
    elif "camelCase" in func_convention:
        func_name = words[0] + "".join(w.capitalize() for w in words[1:3]) if words else "newFunction"
    else:
        func_name = "_".join(words[:3]) if words else "new_function"

    if template_sym:
        sig = template_sym.get("signature", "")
        decorators = template_sym.get("decorators", [])

        # Replace name in signature
        old_name = template_sym.get("name", "")
        if old_name and old_name in sig:
            new_sig = sig.replace(old_name, func_name, 1)
        else:
            new_sig = sig

        lines = []
        for dec in decorators:
            lines.append(f"@{dec}")
        lines.append(new_sig)
        lines.append(f"    # TODO: Implement {intent}")
        lines.append("    pass")
        return "\n".join(lines)

    # No template - generate from scratch
    if target_kind == "class":
        class_name = "".join(w.capitalize() for w in words[:3]) if words else "NewClass"
        return f"class {class_name}:\n    # TODO: Implement {intent}\n    pass"
    else:
        return f"def {func_name}():\n    # TODO: Implement {intent}\n    pass"


def scaffold_symbol(
    repo: str,
    intent: str,
    kind: Optional[str] = None,
    target_file: Optional[str] = None,
    like: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Generate a code scaffold matching codebase conventions.

    Uses AI when available (same provider detection as summarizer),
    with a template-based fallback when no AI is configured.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        intent: What the new symbol should do (e.g., "API endpoint for user deletion").
        kind: Symbol kind to generate (function, class, method). Default: function.
        target_file: Where it should go (helps match conventions).
        like: Symbol ID to use as template.
        storage_path: Custom storage path.

    Returns:
        Dict with generated scaffold and _meta envelope.
    """
    start = time.perf_counter()
    target_kind = kind or "function"

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Find template symbol
    template_sym = None
    template_source = ""

    if like:
        template_sym = index.get_symbol(like)
        if not template_sym:
            return {"error": f"Template symbol not found: {like}"}
        template_source = store.get_symbol_content(owner, name, like) or ""
    else:
        # Find best match using suggest_symbols-style keyword matching
        from .suggest_symbols import _tokenize_task
        from ..storage import score_symbol

        keywords = _tokenize_task(intent)
        keyword_set = set(keywords)
        query_str = " ".join(keywords)

        best_score = 0
        for sym in index.symbols:
            sym_kind = sym.get("kind", "")
            if kind and sym_kind != kind:
                continue
            score = score_symbol(sym, query_str, keyword_set)
            if score > best_score:
                best_score = score
                template_sym = sym

        if template_sym:
            template_source = store.get_symbol_content(owner, name, template_sym["id"]) or ""

    # Extract conventions
    conventions = extract_conventions(repo, focus="all", storage_path=storage_path)
    if "error" in conventions:
        conventions = {}

    # Try AI generation
    ai_generated = False
    scaffold = ""

    provider_name, client = _detect_ai_provider()

    if provider_name and client:
        prompt = _build_scaffold_prompt(intent, conventions, template_source, target_kind)
        try:
            if provider_name == "anthropic":
                scaffold = _generate_with_anthropic(client, prompt)
            elif provider_name == "gemini":
                scaffold = _generate_with_gemini(client, prompt)
            elif provider_name == "openai":
                scaffold = _generate_with_openai(client, prompt)
            ai_generated = bool(scaffold.strip())
        except Exception:
            scaffold = ""

    # Fallback to template-based generation
    if not scaffold.strip():
        scaffold = _template_fallback(intent, template_sym, conventions, target_kind)
        ai_generated = False

    # Build result
    result = {
        "scaffold": scaffold,
        "ai_generated": ai_generated,
        "conventions_applied": [],
    }

    if target_file:
        result["target_file"] = target_file

    if template_sym:
        result["based_on"] = {
            "symbol_id": template_sym["id"],
            "name": template_sym.get("name", ""),
        }

    # Note which conventions were applied
    naming = conventions.get("naming", {})
    if naming.get("functions"):
        result["conventions_applied"].append(f"function naming: {naming['functions']}")
    if naming.get("classes"):
        result["conventions_applied"].append(f"class naming: {naming['classes']}")
    framework = conventions.get("framework", {})
    if framework.get("detected", "none detected") != "none detected":
        result["conventions_applied"].append(f"framework: {framework['detected']}")

    elapsed = (time.perf_counter() - start) * 1000
    result["_meta"] = {"timing_ms": round(elapsed, 1)}

    return result


TOOL_DEF = {
    "name": "scaffold_symbol",
    "description": "Generate a code scaffold for a new symbol that matches existing codebase conventions. Uses AI when available (same providers as summarizer), with template-based fallback.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "intent": {
                            "type": "string",
                            "description": "What the new symbol should do (e.g. 'API endpoint for user deletion')"
                    },
                    "kind": {
                            "type": "string",
                            "description": "Symbol kind to generate (function, class, method)"
                    },
                    "target_file": {
                            "type": "string",
                            "description": "Where the scaffold should go (helps match conventions)"
                    },
                    "like": {
                            "type": "string",
                            "description": "Symbol ID to use as template"
                    }
            },
            "required": [
                    "repo",
                    "intent"
            ]
    },
    "handler": scaffold_symbol,
}
