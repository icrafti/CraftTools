"""Save immutable build snapshots and compare their output and cue metadata."""

import hashlib
import heapq
import json
import math
import os
import re
import tempfile
from pathlib import Path

from checkpoints import file_digest


SCHEMA_VERSION = 1
MAX_BYTES = 32 * 1024 * 1024
MAX_LISTED = 60
IDENTIFIER = re.compile(r"[a-f0-9]{64}")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _identity(value, digests):
    if isinstance(value, Path):
        if value not in digests:
            digests[value] = file_digest(value)
        return digests[value]
    if value is not None and not isinstance(value, str):
        raise ValueError("Snapshot media identity must be a file, string or null")
    return value


def _record(value, digests):
    cue = value["cue"]
    start, end = value["start"], value["end"]
    if not isinstance(cue, str) or not cue:
        raise ValueError("Snapshot record needs a cue")
    if not all(type(item) in (int, float) and math.isfinite(item) for item in (start, end)) or not 0 <= start < end:
        raise ValueError(f"Invalid snapshot timing for {cue}")
    return {"cue": cue, "start": start, "end": end, "duration": round(end - start, 6),
            "image": _identity(value.get("image"), digests),
            "text": value.get("text"), "labels": value.get("labels", []),
            "audio": _identity(value.get("audio"), digests)}


def capture(directory, settings, records, outputs):
    """Save a snapshot from language records and a mapping of artifact names to Paths."""
    digests = {}
    data = {
        "version": SCHEMA_VERSION,
        "settings": settings,
        "records": {language: [_record(item, digests) for item in rows]
                    for language, rows in records.items()},
        "outputs": {name: {"sha256": _identity(path, digests), "bytes": path.stat().st_size}
                    for name, path in outputs.items()},
    }
    payload = _canonical(data)
    if len(payload) > MAX_BYTES:
        raise ValueError("Build snapshot is too large")
    identifier = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{identifier}.json"
    with tempfile.TemporaryDirectory(dir=directory) as work:
        pending = Path(work) / "snapshot.json"
        pending.write_bytes(payload)
        try:
            os.link(pending, target)
        except FileExistsError:
            load(directory, identifier)
    return {"id": identifier, **data}


def load(directory, identifier):
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise FileNotFoundError("Build snapshot not found")
    path = directory / f"{identifier}.json"
    if path.is_symlink():
        raise FileNotFoundError("Build snapshot not found")
    with path.open("rb") as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != identifier:
        raise ValueError("Build snapshot failed its integrity check")
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        raise ValueError("Unsupported build snapshot format")
    return {"id": identifier, **data}


def list_builds(directory):
    if not directory.is_dir():
        return []

    def candidates():
        for path in directory.glob("*.json"):
            if not IDENTIFIER.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                yield path.stat().st_mtime_ns, path.stem
            except OSError:
                continue

    result = []
    for _, identifier in heapq.nlargest(MAX_LISTED, candidates()):
        try:
            data = load(directory, identifier)
        except (OSError, ValueError):
            continue
        result.append({"id": identifier, "settings": data["settings"],
                       "languages": list(data["records"]), "outputs": list(data["outputs"])})
    return result


def _occurrences(records):
    seen = {}
    indexed = {}
    for record in records:
        cue = record["cue"]
        occurrence = seen.get(cue, 0) + 1
        seen[cue] = occurrence
        indexed[(cue, occurrence)] = record
    return indexed


def compare(before, after):
    changes = []
    for language in sorted(before["records"].keys() | after["records"].keys()):
        previous = _occurrences(before["records"].get(language, []))
        updated = _occurrences(after["records"].get(language, []))
        for key in dict.fromkeys([*updated, *previous]):
            first, second = previous.get(key), updated.get(key)
            if first == second:
                continue
            if first is None or second is None:
                kind = "added" if first is None else "removed"
                fields = []
            else:
                kind = "modified"
                fields = [name for name in ("image", "text", "labels", "audio", "duration")
                          if first[name] != second[name]]
                if (first["start"], first["end"]) != (second["start"], second["end"]):
                    fields.append("timing")
            changes.append({"language": language, "cue": key[0], "occurrence": key[1],
                            "kind": kind, "fields": fields, "before": first, "after": second})
    previous, updated = before["outputs"], after["outputs"]
    outputs = [{"name": name, "before": previous.get(name), "after": updated.get(name)}
               for name in sorted(previous.keys() | updated.keys()) if previous.get(name) != updated.get(name)]
    return {"before": before["id"], "after": after["id"], "changes": changes,
            "settings_changed": before["settings"] != after["settings"], "outputs": outputs}
