"""Atomic single-connection config writes shared by join and connect."""

from __future__ import annotations

import configparser
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ConfigWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteResult:
    path: Path
    changes: tuple[str, ...]


def _preserved_text(raw: str) -> str:
    """Remove only [identity]/[server], preserving [dist] and extensions byte-for-byte."""
    headings = list(re.finditer(r"(?m)^\[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)", raw))
    if not headings:
        return raw
    pieces = [raw[:headings[0].start()]]
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(raw)
        if match.group(1).strip().lower() not in {"identity", "server"}:
            pieces.append(raw[match.start():end])
    return "".join(pieces).rstrip()


def upsert_server(
    path: Path,
    *,
    agent_id: str,
    server: dict[str, str],
    force: bool = False,
) -> WriteResult:
    path = Path(path).expanduser().resolve()
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    current = configparser.ConfigParser(interpolation=None)
    if raw:
        try:
            current.read_string(raw)
        except configparser.Error as exc:
            raise ConfigWriteError(f"cannot update invalid config {path}: {exc}") from exc
    old_kind = current.get("server", "kind", fallback="").strip().lower()
    new_kind = server.get("kind", "").strip().lower()
    if old_kind and new_kind and old_kind != new_kind and not force:
        raise ConfigWriteError(
            f"[server] is currently kind={old_kind} and this code is kind={new_kind}. "
            "Refusing to repoint this machine at a different server shape — re-run "
            "with --force if that is what you want."
        )

    changes: list[str] = []
    old_agent = current.get("identity", "agent_id", fallback=None)
    if old_agent != agent_id:
        changes.append(f"agent_id {old_agent or '<unset>'} -> {agent_id}")
    for key, value in server.items():
        old = current.get("server", key, fallback=None)
        if old != value:
            shown_old = "<set>" if key == "api_key" and old else (old or "<unset>")
            shown_new = "<replaced>" if key == "api_key" else value
            changes.append(f"{key} {shown_old} -> {shown_new}")

    rendered = configparser.ConfigParser(interpolation=None)
    rendered.optionxform = str
    rendered["identity"] = {"agent_id": agent_id}
    rendered["server"] = server
    buffer = io.StringIO()
    rendered.write(buffer)
    connection = buffer.getvalue().rstrip()
    preserved = _preserved_text(raw)
    output = f"{preserved}\n\n{connection}\n" if preserved else f"{connection}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return WriteResult(path=path, changes=tuple(changes))
