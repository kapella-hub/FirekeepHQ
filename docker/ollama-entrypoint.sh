#!/bin/sh
# Runtime reassembly for the chunked ollama images (see ollama-chunk.sh):
# concatenate the sorted tar parts from /layers/dNNN and extract at / —
# tar restores symlinks and permissions that a file-level copy would lose.
# The marker guards double-extract: a container restart gets a fresh writable
# layer (marker gone -> clean reassembly), a same-container re-exec skips.
set -eu

MARKER=/.payload-assembled
if [ ! -f "$MARKER" ]; then
    # --keep-directory-symlink: on merged-usr Ubuntu, /bin is a SYMLINK to
    # /usr/bin — default tar replaces it with a real dir when the archive
    # carries that path, destroying /bin/sh for every later exec (k8s
    # readiness probes use absolute /bin/sh; the v0.1.5 pod was never Ready
    # because of exactly this). The payload also avoids ./bin entirely
    # (binary ships at ./usr/bin/ollama), this flag is defense in depth.
    find /layers -type f ! -name .keep | sort | xargs cat | tar --keep-directory-symlink -xf - -C /
    chmod +x /usr/bin/ollama 2>/dev/null || true
    touch "$MARKER"
fi

exec /usr/bin/ollama serve
