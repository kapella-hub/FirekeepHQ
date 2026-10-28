# Glama listing: tool catalog build

Glama scores an MCP listing from the tool definitions the server returns, which
it gets by building the repository and calling `tools/list`. Firekeep's tools
live behind a running Keep, so a plain build reports none and the listing has no
score. `catalog_server.py` serves the **real** definitions instead, read from
each service's FastMCP app at startup, and answers every tool call with
install instructions. It stores nothing and connects to nothing.

## Values for Glama's admin page

On the listing's **Admin → Dockerfile** page
(<https://glama.ai/mcp/servers/kapella-hub/FirekeepHQ>), signed in as the
owner:

**Build steps**

```json
[
  "uv venv /app/.venv",
  "uv pip install --python /app/.venv/bin/python -r deploy/glama/requirements.txt"
]
```

**CMD arguments**

```json
["mcp-proxy", "/app/.venv/bin/python", "deploy/glama/catalog_server.py"]
```

No environment variables are needed. Then press **Deploy**. Pass the command
straight after `mcp-proxy`; mcp-proxy 5.x rejects a `--` separator ("Not
enough non-option arguments").

## Verify before pasting

`Dockerfile` here reproduces Glama's generated image (Debian bookworm, uv
Python 3.13, Node + mcp-proxy) with the same build steps and CMD:

```bash
docker build -f deploy/glama/Dockerfile -t firekeep-glama .
docker run -i --rm firekeep-glama /app/.venv/bin/python deploy/glama/catalog_server.py
```

Then type an `initialize` request followed by `tools/list`.
`tests/test_glama_catalog.py` runs the same exchange in CI with only
`requirements.txt` installed. If a service adds an import-time dependency, that
test fails here before Glama's build breaks.
