// The Hands DOM probe: a single self-contained IIFE, evaluated in the page
// via `Runtime.evaluate` with `returnByValue: true`. `browser.py` prepends a
// `window.__hands = {...};` assignment ahead of this file's source before
// sending it (an assignment, not a `const`, because a second evaluate in the
// same page would otherwise throw on the redeclaration), so every entry point
// reads its arguments off that one object rather
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
// Refs are re-minted on every scan/find call (`"g" + generation + "-d" +
// the element's 1-based position`), where `generation` is a counter kept on
// `window.__hands_gen` and bumped once per scan/find — so it survives
// across separate `Runtime.evaluate` calls the way any other page-global
// state does, but resets if the page itself navigates or reloads (a fresh
// page has no `window.__hands_gen` yet, same as a fresh generation 0).
// "focus" checks a ref's baked-in generation against the CURRENT one before
// even looking at the DOM: a ref from any scan/find but the most recent one
// is rejected as stale, regardless of whether its `data-hands-ref` attribute
// happens to still be sitting on some element (an earlier scan tags visible
// elements; nothing goes back and strips that attribute from an element a
// later scan didn't revisit, so the literal string could otherwise still
// resolve to the wrong, no-longer-current node).
//
// TOP-LEVEL DOCUMENT ONLY: `document.querySelectorAll` does not cross into
// `<iframe>` content documents or pierce shadow DOM (`shadowRoot`), so a
// control that lives inside either is invisible to `scan`/`find` and cannot
// be `click`ed or `fill`ed. No workaround here — see `browser.py`'s module
// docstring for the same limitation stated from the Python side.
(function () {
    var SELECTOR = 'a[href], button, input, select, textarea, ' +
        '[role="button"], [role="link"], [contenteditable], [onclick]';
    var NAME_LIMIT = 80;
    var REF_PATTERN = /^g(\d+)-d\d+$/;

    function currentGeneration() {
        return typeof window.__hands_gen === "number" ? window.__hands_gen : 0;
    }

    function nextGeneration() {
        window.__hands_gen = currentGeneration() + 1;
        return window.__hands_gen;
    }

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

    function accessibleName(el, role) {
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
        // Never the value of a password field: that fallback would put the
        // secret already sitting in the box into the control's NAME, which
        // travels to the model and into the evidence ledger.
        if (role !== "password" && "value" in el && el.value) {
            return truncateText(el.value);
        }
        return "";
    }

    function roleOf(el) {
        // The TYPE decides before any author-supplied `role` attribute: a
        // password box is a password box even if the page labels it
        // `role="textbox"`, and the session maps this onto the PasswordBox
        // role the classifier treats as a credential target. Checking the
        // explicit role first would let a page opt its own password field
        // out of that protection.
        var tag = String(el.tagName || "").toLowerCase();
        if (tag === "input") {
            var type = el.getAttribute ? el.getAttribute("type") : null;
            if (String(type == null ? "" : type).toLowerCase() === "password") {
                return "password";
            }
        }
        var explicit = el.getAttribute ? el.getAttribute("role") : null;
        if (explicit) {
            return explicit;
        }
        return tag;
    }

    function rectOf(el) {
        var r = el.getBoundingClientRect();
        return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    }

    function viewportSize() {
        // `[w, h]`, or `null` when the page cannot say. Null means "no
        // verdict" on the Python side rather than "outside" — a viewport this
        // could not read must never turn every click into a refusal.
        var w = typeof window.innerWidth === "number" ? window.innerWidth : 0;
        var h = typeof window.innerHeight === "number" ? window.innerHeight : 0;
        return w > 0 && h > 0 ? [Math.round(w), Math.round(h)] : null;
    }

    function describe(el, ref) {
        var role = roleOf(el);
        return {
            ref: ref,
            role: role,
            // A password field's contents never leave the page — same rule
            // the Windows backend applies to a PasswordBox.
            value: role !== "password" && "value" in el
                ? String(el.value == null ? "" : el.value) : "",
            name: accessibleName(el, role),
            rect: rectOf(el),
            href: el.getAttribute ? String(el.getAttribute("href") || "") : "",
        };
    }

    function scan(maxNodes) {
        var gen = nextGeneration();
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
            var ref = "g" + gen + "-d" + (i + 1);
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
        var match = REF_PATTERN.exec(String(ref == null ? "" : ref));
        if (!match) {
            return { ok: false, reason: "malformed" };
        }
        if (parseInt(match[1], 10) !== currentGeneration()) {
            // Minted by an earlier scan/find, superseded by a later one —
            // reject on the generation mismatch alone, without even
            // querying the DOM (see the module comment above for why the
            // DOM lookup below is not, by itself, a sufficient check).
            return { ok: false, reason: "stale" };
        }
        var el = document.querySelector('[data-hands-ref="' + ref + '"]');
        if (!el) {
            return { ok: false, reason: "not_found" };
        }
        try {
            el.focus();
        } catch (err) {
            // Not every tagged element is focusable (e.g. a plain
            // [onclick] div) — that must not fail the whole op, since the
            // caller (click) only needs the rect below.
        }
        // Focusing scrolls a focusable element into view; this makes that
        // true for the ones focus() cannot help (a plain [onclick] div below
        // the fold) and deterministic for the ones it does. It matters
        // because `click` dispatches at the centre of the rect returned
        // below, in VIEWPORT coordinates, with no hit test — so an
        // off-viewport rect means the mouse event lands wherever those
        // coordinates happen to be, which may be a different element than the
        // one that was classified and approved. `behavior: "instant"`
        // overrides a page's `scroll-behavior: smooth`, which would otherwise
        // leave the rect below reading a position the page is still animating
        // away from; it throws as an invalid enum on older Chrome, hence the
        // plain retry.
        try {
            el.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
        } catch (err) {
            try {
                el.scrollIntoView({ block: "center", inline: "nearest" });
            } catch (err2) {
                // Not scrollable (or a stub without the method) — the
                // viewport check on the Python side is what catches the
                // consequence.
            }
        }
        return {
            ok: true,
            rect: rectOf(el),
            // So the caller can tell an on-screen rect from one that could
            // not be scrolled to. Sent as data rather than judged here: the
            // refusal belongs with the other HandsErrors.
            viewport: viewportSize(),
        };
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
