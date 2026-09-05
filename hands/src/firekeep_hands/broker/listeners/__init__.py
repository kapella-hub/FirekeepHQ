"""Platform input listeners — the only components that can turn a human
being into an approved permit.

Neither module may be imported for its side effects: `win` and `mac` are
both importable everywhere, on purpose. Their pure halves (`kb_event_is_real`,
`event_is_real`, `ChordTracker`, the key tables) carry the security logic, so
they are unit-tested on Linux CI as well as on the platform they belong to;
everything that needs a real OS API is resolved inside `run_listener`.
"""
