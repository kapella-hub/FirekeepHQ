# Firekeep Hands

Hands is a local MCP server plus a local approval broker: it lets any runtime connected to
your Keep perceive the active window, accessibility tree, and screen, and act on your
computer — focusing and launching apps, invoking controls, typing, driving the browser — with
every consequential step gated behind your explicit, in-the-moment approval.

Hands is turned on with `firekeep hands enable`, the only supported install path. The wheel
imports `firekeep_client.resolver`, `firekeep_client.state`, and `firekeep_client.hooklog`
from the Client Kit's own venv at runtime, and deliberately does **not** declare
`firekeep-client` as a dependency in `pyproject.toml` — that PyPI name is owned by a third
party, so resolving it there would pull in unrelated code (see
`client/firekeep_client/cli.py`). A bare `pip install firekeep-hands` outside the kit venv is
therefore unsupported; it will import cleanly only where a Client Kit install already put
`firekeep_client` on the path.

See [`docs/guides/hands.md`](../docs/guides/hands.md) for the full design: what Hands can
perceive and do, the approval broker, protected-class actions, and the platform backends.
