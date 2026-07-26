"""Pre-flight briefing aggregator (SP1b-server).

GET /briefing composes 11 sections (7 in-process Cortex reads + 4 outbound
HTTP fan-ins to Sentinel/Relay/Bridge) into a single fail-loud response.
Every section is ALWAYS present with an explicit status; the endpoint returns
HTTP 200 whenever the briefing host itself is up.
"""
