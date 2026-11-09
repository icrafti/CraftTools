"""Content signatures and atomic checkpoints for opt-in render recovery."""

import hashlib
import json
import re
import tempfile
import wave
from pathlib import Path


VERSION = 1


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode(value):
    if isinstance(value, Path):
        return ["file", file_digest(value)]
    if isinstance(value, dict):
        return ["object", [[key, _encode(item)] for key, item in sorted(value.items())]]
    if isinstance(value, (list, tuple)):
        return ["array", [_encode(item) for item in value]]
    return ["value", value]


def signature(*parts):
    data = json.dumps(_encode(parts), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _sidecar(target):
    return target.with_name(target.name + ".checkpoint.json")


def current(target, key):
    try:
        state = json.loads(_sidecar(target).read_text(encoding="utf-8"))
        return (
            isinstance(state, dict)
            and state.get("version") == VERSION
            and state.get("key") == key
            and state.get("sha256") == file_digest(target)
        )
    except (OSError, ValueError):
        return False


def commit(target, key):
    state = {"version": VERSION, "key": key, "sha256": file_digest(target)}
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            pending = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False)
            stream.write("\n")
        pending.replace(_sidecar(target))
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def cached_stage(directory, stage, key, produce):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", stage) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid checkpoint stage or signature")
    target = directory / f"{stage}-{key}.wav"
    if current(target, key):
        print(f"Resume: reused {stage}")
        return target
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as work:
        pending = Path(work) / "stage.wav"
        produce(pending)
        try:
            with wave.open(str(pending), "rb") as audio:
                frames = audio.getnframes()
                if not frames:
                    raise ValueError("Checkpoint audio has no frames")
                audio.setpos(frames - 1)
                if len(audio.readframes(1)) != audio.getnchannels() * audio.getsampwidth():
                    raise ValueError("Checkpoint audio contains truncated samples")
        except (wave.Error, EOFError) as error:
            raise ValueError("Checkpoint stage must produce a PCM WAV") from error
        pending.replace(target)
        commit(target, key)
    return target
