"""Persistent export receipts and artifact version checks."""

import json
import os
import re
import tempfile
from urllib.parse import urlencode


def version(path):
    try:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_size, stat.st_ino] if path.is_file() else None
    except OSError:
        return None


class ExportRegistry:
    def __init__(self, directory, resolve):
        self.directory = directory
        self.resolve = resolve

    def save(self, job):
        identifier = job["id"]
        outputs = [
            {**item, "url": "/api/files?" + urlencode({"path": item["path"], "export": identifier})}
            for item in job["outputs"]
        ]
        versions = {item["path"]: version(self.resolve(item["path"])) for item in outputs}
        if None in versions.values():
            raise ValueError("An export artifact disappeared before it could be registered")
        record = {"job": {**job, "outputs": outputs}, "versions": versions}
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.directory) as work:
            pending = self.directory / os.path.basename(work) / "receipt.json"
            pending.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.replace(pending, self.directory / f"{identifier}.json")
        return record

    def load(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            return None
        try:
            record = json.loads((self.directory / f"{identifier}.json").read_text(encoding="utf-8"))
            if record["job"]["id"] == identifier and isinstance(record["versions"], dict):
                return record
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def recent(self, limit):
        dated = []
        for path in self.directory.glob("*.json"):
            try:
                dated.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(dated, reverse=True)]
        return [record for path in paths[:limit] if (record := self.load(path.stem))]

    def matches(self, record, names):
        for name in names:
            expected = record["versions"].get(name)
            if expected is None or version(self.resolve(name)) != expected:
                return False
        return True
