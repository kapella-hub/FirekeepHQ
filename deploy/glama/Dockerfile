# Local reproduction of the image Glama builds for the listing, so the build
# steps and CMD in README.md can be verified before they are pasted into
# Glama's admin page. Glama's generated template: Debian bookworm, uv-managed
# Python 3.13 as `python`, Node + mcp-proxy, the repo at /app.
#
#   docker build -f deploy/glama/Dockerfile -t firekeep-glama .
#   docker run -i --rm firekeep-glama /app/.venv/bin/python deploy/glama/catalog_server.py
FROM debian:bookworm@sha256:854dd8ab51d62866dbd8f5431d15ce77b12983a2a4a1e1fe43a2395dc7ef9f61

ENV DEBIAN_FRONTEND=noninteractive

RUN (apt-get update) && (apt-get install -y --no-install-recommends ca-certificates curl git) \
    && (curl -fsSL https://deb.nodesource.com/setup_24.x | bash -) && (apt-get install -y nodejs) \
    && (npm install -g mcp-proxy@^5.3) \
    && (curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR="/usr/local/bin" sh) \
    && (uv python install 3.13 --default --preview) && (ln -s $(uv python find) /usr/local/bin/python) \
    && (apt-get clean) && (rm -rf /var/lib/apt/lists/*)

WORKDIR /app

COPY . .

# The Glama build steps (README.md), verbatim.
RUN (uv venv /app/.venv) && (uv pip install --python /app/.venv/bin/python -r deploy/glama/requirements.txt)

CMD ["mcp-proxy", "/app/.venv/bin/python", "deploy/glama/catalog_server.py"]
