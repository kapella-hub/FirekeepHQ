# Base image pinned by tag AND digest. `3.11-slim` is a FLOATING tag: it moved
# to 3.11.15 with no commit here, so two builds of the same git SHA were two
# different images — the same reproducibility hole requirements.lock closes for
# Python packages, one layer further down. The digest is what makes it
# immutable; the tag is what lets a human read this line and know what they are
# running. Both, never a bare digest.
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

# Optional corporate CA certs (drop .crt files in docker/ca-certs/)
COPY docker/ca-certs/ /usr/local/share/ca-certificates/
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null 2>&1 && update-ca-certificates && rm -rf /var/lib/apt/lists/*
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY cortex/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Service code
COPY cortex/app/ ./app/

# Shared modules (v2: replay, auth, vault, corpus, evals; provenance: build identity)
COPY replay/ ./replay/
COPY auth/ ./auth/
COPY vault/ ./vault/
COPY corpus/ ./corpus/
COPY provenance/ ./provenance/

# Third-party attribution -- must actually land in the shipped image, not
# just exist at the repo root (see docs/LICENSING.md).
COPY NOTICE .

RUN chown -R appuser:appuser /app

# Build provenance — injected at build time, read at runtime by app/version.py
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ARG APP_VERSION=0.6.0
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    APP_VERSION=${APP_VERSION}

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
