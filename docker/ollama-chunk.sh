#!/bin/sh
# Builder-stage chunker (runs inside the image build): tar the assembled
# payload tree at /payload (preserving SYMLINKS and permissions — ollama's
# /usr/lib/ollama is full of .so version symlinks that a file-level copy
# drops, leaving llama-server unable to load libllama-common.so.0), split the
# tarball into <= CHUNK_BYTES parts, one /chunks/dNNN dir per part, one
# COPY-layer per dir. The runtime entrypoint concatenates the sorted parts
# and untars at /.
#
# Why chunk at all: the office registry promote/the deployment automation/replication chain has
# never moved a blob larger than ~168MB (measured 2026-07-14 against every
# image that fully transferred); the ollama runtime (~1.1GB) and model
# (~3.3GB) layers strand every time.
set -eu

PAYLOAD=/payload
OUT=/chunks
CHUNK_BYTES=$((95 * 1024 * 1024))
# Overridable: the embed image (Dockerfile.embed) packs a much smaller payload
# and declares a shorter fixed COPY list.
MAX_DIRS="${CHUNK_MAX_DIRS:-60}"

mkdir -p "$OUT"
echo "chunker: payload is $(du -sm "$PAYLOAD" | cut -f1)MB"
du -sm "$PAYLOAD"/usr/lib/ollama/* 2>/dev/null | sort -rn | head -5 || true
# STREAM tar into split — never materialize the whole tarball. The runner
# dind disks are tight: payload + tarball + parts (3x peak) blew ENOSPC on
# the v0.1.4 builds; streaming keeps the peak at payload + parts (2x).
tar -cf - -C "$PAYLOAD" . | split -b "$CHUNK_BYTES" -d -a 3 - /tmp/payload.tar.

i=0
for p in /tmp/payload.tar.*; do
    [ -e "$p" ] || continue
    [ "$i" -lt "$MAX_DIRS" ] || { echo "chunker: exceeded $MAX_DIRS dirs — payload too large" >&2; exit 1; }
    d=$(printf 'd%03d' "$i")
    mkdir -p "$OUT/$d"
    mv "$p" "$OUT/$d/"
    i=$((i + 1))
done

# Every dir up to MAX_DIRS must exist (the Dockerfile has a fixed COPY list).
k=0
while [ "$k" -lt "$MAX_DIRS" ]; do
    d=$(printf 'd%03d' "$k")
    mkdir -p "$OUT/$d"
    touch "$OUT/$d/.keep"
    k=$((k + 1))
done

echo "chunker: packed $i tar parts of <= $CHUNK_BYTES bytes"
