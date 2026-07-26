ARG REGISTRY=docker.io/library
FROM ${REGISTRY}/python:3.11-slim

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
