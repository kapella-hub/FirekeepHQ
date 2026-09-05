// The Hands DOM probe: a single self-contained IIFE, evaluated in the page
// via `Runtime.evaluate` with `returnByValue: true`. `browser.py` prepends a
// `const __hands = {...};` line ahead of this file's source before sending
// it, so every entry point reads its arguments off that one object rather
// than through separate `Runtime.evaluate` parameters — CDP's evaluate takes
// a bare expression string, not a function plus arguments.
//
// Three operations:
//   "scan"  -> tag every visible interactive element with a fresh
//              `data-hands-ref`, return {controls, truncated}.
//   "find"  -> the same scan, filtered by a case-insensitive substring over
//              name/value/href, capped at `__hands.limit`.
//   "focus" -> resolve a ref minted by an earlier scan/find, call
//              `.focus()` on it, and return its CURRENT rect. `browser.py`
//              uses this for both `fill` (it needs the DOM focus) and
//              `click` (it needs a rect that cannot go stale between the
//              probe running and the mouse event being dispatched) — a
//              harmless side effect for the click case, since every element
//              this probe tags is already natively focusable.
//
// Refs are re-minted on every scan/find call ("d" + the element's 1-based
// position in the interactive-element list); a ref from a previous call is
// only safe to reuse — via "focus" — as long as the page has not been
// re-scanned since. That is the same staleness contract `browser.py`
// documents for `click`/`fill`.
(function () {
    var SELECTOR = 'a[href], button, input, select, textarea, ' +
        '[role="button"], [role="link"], [contenteditable], [onclick]';
    var NAME_LIMIT = 80;

    function isVisible(el) {
        var rect = el.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) {
            return false;
        }
        if (typeof window !== "undefined" && window.getComputedStyle) {
            var style = window.getComputedStyle(el);
            if (style && style.visibility === "hidden") {
                return false;
            }
        }
        return true;
    }

    function truncateText(value) {
        var text = String(value == null ? "" : value).trim();
        return text.length > NAME_LIMIT ? text.slice(0, NAME_LIMIT) : text;
    }

    function accessibleName(el) {
        var getAttr = el.getAttribute ? el.getAttribute.bind(el) : function () { return null; };
        var ariaLabel = getAttr("aria-label");
        if (ariaLabel) {
            return truncateText(ariaLabel);
        }
        var text = el.innerText !== undefined ? el.innerText : el.textContent;
        if (text && String(text).trim()) {
            return truncateText(text);
        }
        var placeholder = getAttr("placeholder");
        if (placeholder) {
            return truncateText(placeholder);
        }
        var alt = getAttr("alt");
        if (alt) {
            return truncateText(alt);
        }
        var title = getAttr("title");
        if (title) {
            return truncateText(title);
        }
        if ("value" in el && el.value) {
            return truncateText(el.value);
        }
        return "";
    }

    function roleOf(el) {
        var explicit = el.getAttribute ? el.getAttribute("role") : null;
        if (explicit) {
            return explicit;
        }
        return String(el.tagName || "").toLowerCase();
    }

    function rectOf(el) {
        var r = el.getBoundingClientRect();
        return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    }

    function describe(el, ref) {
        return {
            ref: ref,
            role: roleOf(el),
            name: accessibleName(el),
            value: "value" in el ? String(el.value == null ? "" : el.value) : "",
            rect: rectOf(el),
            href: el.getAttribute ? String(el.getAttribute("href") || "") : "",
        };
    }

    function scan(maxNodes) {
        var nodes = document.querySelectorAll(SELECTOR);
        var controls = [];
        var truncated = false;
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el)) {
                continue;
            }
            if (controls.length >= maxNodes) {
                truncated = true;
                break;
            }
            var ref = "d" + (i + 1);
            el.setAttribute("data-hands-ref", ref);
            controls.push(describe(el, ref));
        }
        return { controls: controls, truncated: truncated };
    }

    function find(query, maxNodes, limit) {
        var needle = String(query == null ? "" : query).toLowerCase();
        var scanned = scan(maxNodes);
        var matches = scanned.controls.filter(function (control) {
            return control.name.toLowerCase().indexOf(needle) !== -1
                || control.value.toLowerCase().indexOf(needle) !== -1
                || control.href.toLowerCase().indexOf(needle) !== -1;
        });
        return {
            controls: matches.slice(0, limit),
            truncated: scanned.truncated || matches.length > limit,
        };
    }

    function focus(ref) {
        var el = document.querySelector('[data-hands-ref="' + ref + '"]');
        if (!el) {
            return { ok: false };
        }
        try {
            el.focus();
        } catch (err) {
            // Not every tagged element is focusable (e.g. a plain
            // [onclick] div) — that must not fail the whole op, since the
            // caller (click) only needs the rect below.
        }
        return { ok: true, rect: rectOf(el) };
    }

    var settings = typeof __hands !== "undefined" ? __hands : {};
    var op = settings.op || "scan";
    var maxNodes = settings.max_nodes || 200;

    if (op === "scan") {
        return scan(maxNodes);
    }
    if (op === "find") {
        return find(settings.query, maxNodes, settings.limit || 10);
    }
    if (op === "focus") {
        return focus(settings.ref);
    }
    return { controls: [], truncated: false, error: "unknown probe op: " + op };
})()
