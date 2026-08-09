"""Knowledge Autopilot — round 1: it proposes and reports, it never mutates.

The knowledge base already produces review work in five scattered places (draft
skills, stale skills, source-changed skills, Living Procedures proposals, the
eval DLQ) plus GC actions nobody sees at all. Each of those lives behind a
different tab, a different endpoint, and in one case behind no reader at all —
so "is there anything for me to do?" was a question only someone who already
knew all five places could answer, and the honest answer for most operators was
"I don't know". This package aggregates them into one surface (the exception
inbox) and adds a digest that answers "what changed this week", so nobody has to
manage inventories.

ROUND 1 IS READ-ONLY, deliberately and testably. Every action an inbox row
suggests is handed back to the surface that already owns it (the Skills draft
queue, the procedures panel) rather than re-implemented here. An aggregator that
also mutates becomes a second write path for five subsystems' invariants, and
the first bug in it is silent: it would act on a stale read of somebody else's
state. `tests/test_dashboard_autopilot.py` pins the absence of any mutation call
in the panel as an invariant, not as an accident of it not being written yet.
"""

from app.autopilot.api import create_autopilot_router

__all__ = ["create_autopilot_router"]
