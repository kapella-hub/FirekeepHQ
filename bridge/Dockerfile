# Base pulled via the registry mirror (not Docker Hub directly)
# — the bare pull hit a Docker Hub 502 in the v0.2.0 build. REGISTRY overridable.
#
# Base image pinned by tag AND digest. `3.11-slim` is a FLOATING tag: it moved
# to 3.11.15 with no commit here, so two builds of the same git SHA were two
# different images — the same reproducibility hole requirements.lock (below)
# closes for Python packages, one layer further down. The digest is what makes
# it immutable; the tag is what lets a human read this line and know what they
# are running. Both, never a bare digest.
#
# It is the multi-arch MANIFEST LIST (OCI index) digest, NOT a per-platform one
# — pinning a platform manifest breaks every architecture except the one it was
# resolved on. Re-resolve from the TOP-LEVEL "Digest:" of:
#   docker buildx imagetools inspect python:<tag>
# All five service Dockerfiles (root, cortex, bridge, sentinel, relay) must
# carry the SAME value — a stack whose services sit on different Python patch
# releases is a debugging trap nobody thinks to suspect.
#
# ${REGISTRY} is deliberately preserved: a digest is content-addressed, so a
# pull-through mirror serves the identical manifest. A mirror that RE-PUSHES
# rather than proxies may not carry this digest — that fails loudly at build,
# which is the right outcome and the reason not to hard-code docker.io here.
ARG REGISTRY=docker.io/library
FROM ${REGISTRY}/python:3.11.15-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Dependencies come from the LOCK, not requirements.txt. The .txt file holds
# loose ranges (`fastapi>=0.115,<1`) and is the INPUT to resolution; the .lock is
# the resolved, hash-pinned output and is what gets installed. Without this, two
# builds of the same commit produce different images -- a customer reporting a
# bug against a git SHA would not be describing a knowable artifact. It also
# makes the CI CVE gate and the SBOM mean something: both now describe what
# ships rather than whatever resolves on the day they run.
#
# Hashes make pip enter --require-hashes mode implicitly: every requirement must
# be pinned with == and carry a hash, and a tampered or re-uploaded artifact
# fails the build loudly instead of installing.
#
# Regenerate after editing requirements.txt (CI fails if they drift):
#   uv pip compile <svc>/requirements.txt --python-platform linux #     --python-version 3.11 --generate-hashes --output-file <svc>/requirements.lock
COPY bridge/requirements.txt bridge/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Service code
COPY bridge/app/ ./app/

# Shared modules (v2: replay emitter)
COPY replay/ ./replay/
# SP1a: shared auth (FirekeepKeyAuthMiddleware in auth/asgi.py)
COPY auth/ ./auth/
# Shared build-identity module (GET /version)
COPY provenance/ ./provenance/

RUN chown -R appuser:appuser /app

# Build provenance — injected at build time, read at runtime by GET /version
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ARG APP_VERSION=0.6.0
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    APP_VERSION=${APP_VERSION}

USER appuser

EXPOSE 8070

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('localhost', 8070), 5); s.close()"

STOPSIGNAL SIGTERM

CMD ["python", "-m", "app.mcp_server"]
