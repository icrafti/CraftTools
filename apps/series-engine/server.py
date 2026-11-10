"""Local browser interface and HTTP API for the series engine."""

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
import webbrowser
from collections import deque
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import series
import build_history
import tts_bridge
from export_registry import ExportRegistry
import quality


ENGINE = Path(__file__).resolve().parent
WORKSPACE = ENGINE.parent
LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
MIME_TYPES = {
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
IMAGE_TYPES = {ext for ext, mime in MIME_TYPES.items() if mime.startswith("image/")}
MAX_LOG = 64000
MAX_JOBS = 60
MAX_PREVIEWS = 8
REPEAT_HOLD_SECONDS = 9
REPEAT_WINDOW_SECONDS = 60
REPEAT_MIN_APPEARANCES = 3


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def inside(path, boundary=None):
    if not path.resolve().is_relative_to(boundary or WORKSPACE):
        raise ValueError("A project path points outside the workspace")
    return path


def relative(path):
    return path.relative_to(WORKSPACE).as_posix()


def project_root(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Specify the project folder")
    root = inside(WORKSPACE / value).resolve()
    if not inside(root / "series.json").is_file():
        raise ApiError(404, "No series.json in the selected folder")
    return root


def project_config(root):
    config = series.read(inside(root / "series.json"))
    if not isinstance(config, dict) or not isinstance(config.get("languages"), dict):
        raise ValueError("series.json must define languages")
    if not config["languages"] or not all(
        LANGUAGE.fullmatch(code) and isinstance(settings, dict)
        for code, settings in config["languages"].items()
    ):
        raise ValueError("Invalid language settings in series.json")
    return config


def project_info(root, config):
    return {"path": relative(root), "name": config.get("name") or root.name}

def pronunciation_path(root, language):
    if language not in project_config(root)["languages"]:
        raise ValueError("Choose a configured pronunciation language")
    return inside(root / language / "pronunciation.json", root)


def read_pronunciation(path):
    return series.pronunciation_rules(series.read(path)) if path.is_file() else []


def save_pronunciation(payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = project_root(payload.get("path"))
    language = payload.get("language")
    if not isinstance(language, str):
        raise ValueError("Choose a configured pronunciation language")
    path = pronunciation_path(root, language)
    rules = series.pronunciation_rules(payload.get("rules"))
    sample = payload.get("sample", "")
    dry_run = payload.get("dry_run", False)
    if not isinstance(sample, str) or len(sample) > 4000 or type(dry_run) is not bool:
        raise ValueError("sample must be text and dry_run must be a boolean")
    if not dry_run:
        with jobs.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            pending = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 suffix=".tmp", delete=False) as stream:
                    pending = Path(stream.name)
                    json.dump(rules, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                pending.replace(path)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
    return {"language": language, "rules": rules, "dry_run": dry_run,
            "text": series.pronunciation_transform(rules)(sample)}


def narration_for_tts(root, language):
    path = pronunciation_path(root, language)
    source = inside(path.parent / "narration.jsonl", root)
    if not source.is_file():
        raise ApiError(404, "No narration.jsonl for this language")
    transform = series.pronunciation_transform(read_pronunciation(path))
    output = []
    with source.open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                raise ValueError(f"narration.jsonl line {index} requires a text field")
            output.append(json.dumps({**row, "text": transform(row["text"])}, ensure_ascii=False))
    return ("\n".join(output) + "\n").encode("utf-8") if output else b""


def tts_settings(root, query):
    config = project_config(root)
    language = query.get("language", [config.get("source_language")])[0]
    if language not in config["languages"]:
        raise ValueError("Choose a configured narration language")
    settings = config.get("tts", {})
    if not isinstance(settings, dict):
        raise ValueError("tts must be an object in series.json")
    provider = query.get("provider", [settings.get("provider", "microsoft")])[0]
    if provider not in ("microsoft", "qwen", "omnivoice"):
        raise ValueError("Choose microsoft, qwen or omnivoice")
    profile = settings.get(provider, {})
    if not isinstance(profile, dict):
        raise ValueError("TTS provider settings must be an object")
    endpoint = profile.get("endpoint", {"qwen": "http://127.0.0.1:9881",
                                        "omnivoice": "http://127.0.0.1:9882"}.get(provider))
    model = profile.get("model")
    timeout = profile.get("timeout_seconds", 120)
    if endpoint is not None:
        if not isinstance(endpoint, str):
            raise ValueError("TTS endpoint must be an HTTP or HTTPS URL")
        address = urlsplit(endpoint)
        if address.scheme not in ("http", "https") or not address.hostname or address.username or address.password:
            raise ValueError("TTS endpoint must be an HTTP or HTTPS URL without credentials")
        _ = address.port  # Validate an explicitly configured port.
    if (model is not None and not isinstance(model, str)) or type(timeout) is not int or timeout <= 0:
        raise ValueError("TTS model must be a string and timeout_seconds a positive integer")
    voices = profile.get("voices", {})
    if not isinstance(voices, dict) or not isinstance(overrides := voices.get(language, {}), dict):
        raise ValueError("TTS voices must map languages to voice overrides")
    if not all(isinstance(voice, str) and voice.strip() for voice in overrides.values()):
        raise ValueError("TTS voice overrides must be nonempty strings")
    voice_file = inside(root / language / "voices.json", root)
    return {"provider": provider, "language": language, "endpoint": endpoint, "model": model,
            "timeout_seconds": timeout, "voice_overrides": overrides,
            "voices": series.read(voice_file) if voice_file.is_file() else None,
            "narration_url": "/api/narration/tts?" + urlencode({"path": relative(root), "language": language})}


def call_tts_bridge(profile, operation, speech=None):
    try:
        return tts_bridge.request(profile, operation, speech)
    except RuntimeError as error:
        raise ApiError(502, str(error)) from error


def project_image(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError("Specify an image path inside the project")
    path = inside(root / value, root).resolve()
    if path.suffix.lower() not in IMAGE_TYPES or not path.is_file():
        raise ApiError(404, "Project image not found")
    return path


def project_image_url(root, path):
    query = urlencode({"path": relative(root), "image": path.relative_to(root).as_posix()})
    return f"/api/project/image?{query}"


def region_frame(root, config, cue):
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    rows = series.read(storyboard) if storyboard.is_file() else []
    shot = next((row for row in rows if series.cue(row) == cue), None)
    if shot is None:
        raise ApiError(404, "Cue not found in the storyboard")
    name = shot["image"]
    image = inside(storyboard.parent / name, root).resolve()
    image = project_image(root, image.relative_to(root).as_posix())
    marked = name in config.get("ocr_images", []) or any(
        row["image"] == name and row.get("ocr") is True for row in rows)
    return name, image, marked


def region_state(root, config, cue, frame):
    name, image, marked = frame
    return {"cue": cue, "image": project_image_url(root, image), "ocr_marked": marked,
            "regions": config.get("regions", {}).get(name)}


def normalized_box(box):
    if not isinstance(box, list) or len(box) != 4 or any(
        type(number) not in (int, float) or not 0 <= number <= 1 for number in box
    ):
        raise ValueError("Box must contain four normalized coordinates")
    x, y, w, h = box
    if not (x < 1 and y < 1 and w > 0 and h > 0 and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9):
        raise ValueError("Draw a nonempty box inside the image")
    return [x, y, min(w, 1 - x), min(h, 1 - y)]


def region_rules(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Expected up to 32 text regions")
    regions = []
    for region in value:
        if not isinstance(region, dict):
            raise ValueError("Each region must be an object")
        text, box, angle = region.get("text"), region.get("box"), region.get("angle", 0)
        if not isinstance(text, str) or not re.fullmatch(r"[ -~]{1,200}", text) or not text.strip():
            raise ValueError("Enter the English text or digits shown on the image (up to 200 characters)")
        if type(angle) not in (int, float) or not -math.pi <= angle <= math.pi:
            raise ValueError("Angle must be within ±π radians")
        rule = {"text": text.strip(), "box": normalized_box(box)}
        if angle:
            rule["angle"] = angle
        regions.append(rule)
    return regions


def save_regions(root, payload, jobs):
    if "regions" not in payload or type(payload.get("dry_run", False)) is not bool:
        raise ValueError("Supply regions and an optional boolean dry_run")
    value = payload["regions"]
    rules = region_rules(value) if value is not None else None
    dry_run = payload.get("dry_run", False)
    cue = payload.get("cue")
    with jobs.lock:
        if jobs.busy and not dry_run:
            raise ApiError(409, "Wait for the active job before changing text regions")
        config = project_config(root)
        frame = region_frame(root, config, cue)
        overrides = config.setdefault("regions", {})
        changed = overrides.get(frame[0]) != rules
        if rules is None:
            overrides.pop(frame[0], None)
        else:
            overrides[frame[0]] = rules
        if changed and not dry_run:
            target = inside(root / "series.json", root)
            pending = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
                    pending = Path(stream.name)
                    json.dump(config, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                pending.replace(target)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
    return {**region_state(root, config, cue, frame), "dry_run": dry_run}


def preview_regions(root, payload, work):
    config = project_config(root)
    language = payload.get("language")
    if not isinstance(language, str) or language not in config["languages"]:
        raise ValueError("Choose a configured preview language")
    _, image, _ = region_frame(root, config, payload.get("cue"))
    rules = region_rules(payload.get("regions"))
    glossary = {series.key(text): values for text, values in config.get("glossary", {}).items()}
    labels = []
    if not series.original_art_language(config, language):
        aliases = {series.key(text): value for text, value in config.get("ocr_aliases", {}).items()}
        for rule in rules:
            text = rule["text"]
            lookup = series.key(aliases.get(series.key(text), text))
            if any(char.isalpha() for char in text) and language not in glossary.get(lookup, {}):
                raise ValueError(f"{language}: add a glossary translation for {text!r}")
        labels = series.localized_labels(rules, language, config, glossary)
    font = config.get("fonts", {}).get(language, "C:/Windows/Fonts/arial.ttf")
    return series.localize(image, labels, font, work)


def project_library(root, kind, selected_cue):
    config = project_config(root)
    manifest = inside(root / config.get("library", "library.json"), root)
    records = series.read(manifest) if manifest.is_file() else []
    if not isinstance(records, list):
        raise ValueError("library.json must contain a list")
    kinds = {"location", "prop"}
    if kind is not None and kind not in kinds:
        raise ValueError("kind must be location or prop")
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    cues = list(dict.fromkeys(series.cue(shot) for shot in series.read(storyboard))) if storyboard.is_file() else []
    known_cues = set(cues)
    if selected_cue is not None and selected_cue not in known_cues:
        raise ApiError(404, "Cue not found in the storyboard")
    items = []
    identifiers = set()
    for entry in records:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(field), str) and entry[field].strip() for field in ("id", "kind", "name", "description")
        ):
            raise ValueError("Library items need id, kind, name and description")
        identifier = entry["id"]
        if identifier in identifiers or entry["kind"] not in kinds:
            raise ValueError(f"Duplicate id or invalid kind for library item {identifier}")
        identifiers.add(identifier)
        links = entry.get("cues", [])
        refs = entry.get("references", [])
        if not isinstance(links, list) or any(not isinstance(cue, str) for cue in links):
            raise ValueError(f"Expected a list of cue strings for {identifier}")
        if not isinstance(refs, list):
            raise ValueError(f"Expected a list of references for {identifier}")
        if kind is not None and entry["kind"] != kind or selected_cue is not None and selected_cue not in links:
            continue
        issues = [{"code": "unknown_cue", "cue": cue} for cue in links if cue not in known_cues]
        references = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("label"), str):
                raise ValueError(f"Invalid reference for {identifier}")
            try:
                image = project_image(root, ref.get("image"))
            except ApiError as error:
                if error.status != 404:
                    raise
                issues.append({"code": "missing_image", "image": ref["image"]})
            else:
                references.append({**ref, "url": project_image_url(root, image)})
        items.append({**entry, "references": references, "issues": issues})
    return {"items": items, "cues": cues}


def continuity_report(root):
    config = project_config(root)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    shots = series.read(storyboard) if storyboard.is_file() else []
    path = inside(root / config.get("continuity", "continuity.json"), root)
    annotations = series.read(path) if path.is_file() else {}
    if not isinstance(annotations, dict):
        raise ValueError("continuity.json must be an object keyed by cue")
    by_cue = {series.cue(shot): shot for shot in shots}
    issues = [{"code": "unknown_cue", "cue": cue} for cue in sorted(annotations.keys() - by_cue.keys())]
    annotated = 0
    previous_cue, previous = None, {}
    for cue, shot in by_cue.items():
        current = annotations.get(cue, {})
        if not isinstance(current, dict):
            raise ValueError(f"{cue}: continuity annotation must be an object")
        annotated += bool(current)
        place = current.get("place")
        outfits = current.get("outfits", {})
        changes = current.get("outfit_changes", [])
        reading = current.get("reading", {})
        if not isinstance(outfits, dict) or not isinstance(changes, list) or not isinstance(reading, dict):
            raise ValueError(f"{cue}: invalid outfits, outfit_changes or reading annotation")
        if current.get("shelter") not in (None, "dry", "open", "leaking") or current.get("rain") not in (
            None, "inside", "outside", "none"
        ):
            raise ValueError(f"{cue}: invalid shelter or rain annotation")
        if any(field in reading and type(reading[field]) is not bool for field in ("can_read", "viewer_can_read")):
            raise ValueError(f"{cue}: reading visibility must be a boolean")
        found = []
        if current.get("shelter") == "dry" and current.get("rain") == "inside":
            found.append({"code": "rain_inside_dry_location", "place": place})
        if current.get("expected_place") and place and current["expected_place"] != place:
            found.append({"code": "location_mismatch", "place": place, "expected_place": current["expected_place"]})
        if place and place == previous.get("place"):
            previous_outfits = previous.get("outfits", {})
            for actor in sorted(previous_outfits.keys() & outfits.keys()):
                if outfits[actor] != previous_outfits[actor] and actor not in changes:
                    found.append({"code": "outfit_changed", "actor": actor, "previous_cue": previous_cue,
                                  "before": previous_outfits[actor], "after": outfits[actor]})
        if reading.get("can_read") is False:
            found.append({"code": "reader_cannot_see_text", "actor": reading.get("actor")})
        if reading.get("viewer_can_read") is False:
            found.append({"code": "viewer_cannot_see_text", "actor": reading.get("actor")})
        if isinstance(current.get("review"), str) and current["review"].strip():
            found.append({"code": "visual_review", "review": current["review"]})
        for issue in found:
            issue.update(cue=cue, notes=current.get("notes"))
            for field, related in (("image_url", cue), ("previous_image_url", issue.get("previous_cue"))):
                if related is None:
                    continue
                image = inside(storyboard.parent / by_cue[related]["image"], root).resolve()
                if image.is_file() and image.suffix.lower() in IMAGE_TYPES:
                    issue[field] = project_image_url(root, image)
            issues.append(issue)
        previous_cue, previous = cue, current
    return {"total_cues": len(by_cue), "annotated_cues": annotated, "issues": issues}


def project_characters(root, selected_cue):
    config = project_config(root)
    manifest = inside(root / config.get("characters", "characters.json"), root)
    profiles = series.read(manifest) if manifest.is_file() else {}
    characters = {}
    speaker_ids = {}
    for language in config["languages"]:
        path = inside(root / language / "voices.json", root)
        if not path.is_file():
            continue
        for speaker, voice in series.read(path)["speakers"].items():
            identifier = voice.get("voice_id") or speaker
            speaker_ids.setdefault(speaker, identifier)
            entry = characters.setdefault(identifier, {"id": identifier, "languages": {}})
            entry["languages"].setdefault(language, []).append({"speaker": speaker, **voice})
    for identifier, profile in profiles.items():
        entry = characters.setdefault(identifier, {"id": identifier, "languages": {}})
        entry.update({key: profile[key] for key in ("traits", "appearance", "continuity", "portrait_grid")
                      if key in profile})
        if profile.get("portrait"):
            portrait = project_image(root, profile["portrait"])
            entry["portrait_url"] = project_image_url(root, portrait)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    shots = series.read(storyboard) if storyboard.is_file() else []
    scenes = []
    for shot in shots:
        speaker = shot.get("speaker")
        identifiers = list(shot.get("characters", []))
        if speaker in speaker_ids:
            identifiers.append(speaker_ids[speaker])
        scenes.append({"cue": series.cue(shot), "characters": list(dict.fromkeys(
            identifier for identifier in identifiers if identifier in characters
        ))})
    scene = next((row for row in scenes if row["cue"] == selected_cue), None)
    if selected_cue is not None and scene is None:
        raise ApiError(404, "Cue not found in the storyboard")
    return {"characters": list(characters.values()), "scenes": scenes, "scene": scene}


def discover_projects():
    projects = []
    ignored = {"node_modules", "yt-dlp", "__pycache__"}
    for folder, dirs, names in os.walk(WORKSPACE):
        root = Path(folder)
        dirs[:] = [name for name in dirs if name not in ignored and not name.startswith(".")
                   and (root / name).resolve() == root / name]
        if "series.json" in names:
            try:
                projects.append(project_info(root, project_config(root)))
            except (OSError, ValueError, TypeError):
                continue
            dirs.clear()
    return sorted(projects, key=lambda item: item["path"].casefold())


def create_project(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    name = payload.get("name")
    slug = payload.get("slug")
    codes = payload.get("languages")
    source = payload.get("source_language", "en")
    dry_run = payload.get("dry_run", False)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError("Enter a project title of 1 to 120 characters")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("Use 1 to 64 lowercase letters, digits or hyphens for the folder")
    if re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", slug):
        raise ValueError("The folder name is reserved")
    if not isinstance(codes, list) or not 1 <= len(codes) <= 30 or not all(
        isinstance(code, str) and len(code) <= 35 and LANGUAGE.fullmatch(code)
        and code.casefold() not in {"con", "prn", "aux", "nul"} for code in codes
    ):
        raise ValueError("Supply 1 to 30 valid language codes")
    if len({code.casefold() for code in codes}) != len(codes) or source not in codes:
        raise ValueError("Language codes must be unique and include the source language")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    root = inside(WORKSPACE / "output" / slug)
    if root.exists() or root.is_symlink():
        raise ApiError(409, "That project folder already exists")
    config = {"name": name.strip(), "source_language": source, "languages": {code: {} for code in codes}}
    shot = {"cue": "G01-001:1", "image": "frames/001.jpg"}
    documents = {"shots.json": [shot]}
    folders = ["frames"]
    for code in codes:
        folders.append(f"{code}/clips")
        documents[f"{code}/utterances.json"] = [{"cue": shot["cue"], "audio": "clips/001.mp3"}]
    documents["series.json"] = config
    result = {**project_info(root, config), "source_language": source, "languages": codes,
              "files": list(documents), "folders": folders, "dry_run": dry_run}
    if dry_run:
        return result
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise ApiError(409, "That project folder already exists") from None
    try:
        for folder in folders:
            (root / folder).mkdir(parents=True)
        for filename, data in documents.items():
            (root / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        shutil.rmtree(root)
        raise
    return result


def narration_ready(root, code, settings):
    manifest = inside(root / settings.get("utterances", f"{code}/utterances.json"))
    if "utterances" in settings or manifest.is_file():
        if not manifest.is_file():
            return False
        try:
            rows = series.read(manifest)
            return isinstance(rows, list) and bool(rows) and all(
                isinstance(row, dict) and isinstance(row.get("audio"), str)
                and isinstance(row.get("cue"), str) and bool(row["cue"].strip())
                and inside(manifest.parent / row["audio"]).is_file()
                for row in rows
            )
        except (OSError, ValueError):
            return False
    return all(inside(root / settings.get(key, fallback)).is_file() for key, fallback in (
        ("timeline", f"{code}/timeline.json"), ("audio", f"{code}/narration.wav"),
    ))


def sync_timeline(root, language, settings, utterance_output=None):
    manifest = inside(root / settings.get("utterances", f"{language}/utterances.json"), root)
    if "utterances" in settings or manifest.is_file():
        rows = series.read(manifest)
        if not isinstance(rows, list):
            raise ValueError("Expected a list of utterances")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("audio"), str):
                raise ValueError("Every utterance needs an audio path")
            inside(manifest.parent / row["audio"], root)
        return series.time_utterances(manifest, utterance_output, rows=rows, verify_pcm=True,
                                     profile=quality.profile(project_config(root)))
    timeline = inside(root / settings.get("timeline", f"{language}/timeline.json"), root)
    audio = inside(root / settings.get("audio", f"{language}/narration.wav"), root)
    if not timeline.is_file():
        raise series.BuildIssue("Narration timeline is missing", "missing_timeline", "prepare_timing", file=timeline)
    entries = series.read(timeline)
    if not isinstance(entries, list) or not entries:
        raise ValueError("Expected a nonempty narration timeline")
    if not audio.is_file():
        raise series.BuildIssue("Narration audio file is missing", "missing_audio", "import_audio", file=audio)
    if abs(series.audio_duration(audio) - round(entries[-1]["end"] * series.FPS) / series.FPS) > 1 / series.FPS:
        raise ValueError("Narration audio duration differs from its timeline")
    return entries


def readability_targets(root, config, images):
    path = inside(root / "readability.json", root)
    data = series.read(path) if path.is_file() else {}
    if not isinstance(data, dict) or data.keys() - images.keys():
        raise ValueError("readability.json must map known storyboard cues to target lists")
    result = {}
    for cue, image in images.items():
        custom = data.get(cue, [])
        if not isinstance(custom, list):
            raise ValueError(f"{cue}: expected a readability target list")
        targets = [{"label": region["text"], "kind": "text", "box": region["box"],
                    "angle": region.get("angle", 0)}
                   for region in config.get("regions", {}).get(image, [])]
        for target in custom:
            if (not isinstance(target, dict) or target.get("kind") not in ("text", "object")
                    or not isinstance(target.get("label"), str) or not target["label"].strip()):
                raise ValueError(f"{cue}: target requires label, kind and box")
            targets.append({"label": target["label"], "kind": target["kind"],
                            "box": normalized_box(target.get("box")), "angle": 0})
        if targets:
            result[cue] = targets
    return result


def readability_issue(target, window, dimensions):
    left, top, crop_width, crop_height = window
    x, y, w, h = (value * scale for value, scale in zip(target["box"], series.FRAME_SIZE * 2))
    angle = target["angle"]
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    span_x, span_y = cosine * w + sine * h, sine * w + cosine * h
    x, y = x + (w - span_x) / 2, y + (h - span_y) / 2
    visible_x = max(0, min(x + span_x, left + crop_width) - max(x, left))
    visible_y = max(0, min(y + span_y, top + crop_height) - max(y, top))
    fraction = visible_x * visible_y / (span_x * span_y)
    width_px, height_px = w * dimensions[0] / crop_width, h * dimensions[1] / crop_height
    minimum = height_px < 16 if target["kind"] == "text" else min(width_px, height_px) < 24
    if fraction == 0:
        code = "outside"
    elif max(span_x - visible_x, span_y - visible_y) > 2:
        code = "clipped"
    elif minimum:
        code = "too_small"
    else:
        return None
    return {"code": code, "label": target["label"], "kind": target["kind"],
            "visible_fraction": round(fraction, 4),
            "width_px": round(width_px, 1), "height_px": round(height_px, 1)}


def readability_report(root, query):
    config = project_config(root)
    language = query.get("language", [config.get("source_language")])[0]
    aspect = query.get("aspect", ["16:9"])[0]
    resolution = query.get("resolution", ["1080p"])[0]
    if language not in config["languages"] or aspect not in ("16:9", "9:16") or resolution not in series.RESOLUTIONS:
        raise ValueError("Choose a configured language, aspect and resolution")
    entries = sync_timeline(root, language, config["languages"][language])
    series.blocks(entries)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    images = {series.cue(row): row["image"] for row in series.read(storyboard)}
    targets = readability_targets(root, config, images)
    montage = series.read_montage(inside(root / "montage.json", root))
    vertical = aspect == "9:16"
    dimensions = series.output_dimensions(resolution, vertical)
    issues = []
    checked = 0
    for entry in entries:
        cue = series.cue(entry)
        if cue not in images:
            raise ValueError(f"Missing storyboard cue: {cue}")
        if cue not in targets:
            continue
        checked += 1
        image = project_image(root, str(storyboard.parent / images[cue]))
        rule = montage.get(cue, {})
        start, end = (round(entry[key] * series.FPS) for key in ("start", "end"))
        intervals = []
        current = start
        for first, last in series.close_intervals(start, end, rule, cue, quality.profile(config)):
            if current < first:
                intervals.append((current, first, False))
            intervals.append((first, last, True))
            current = last
        if current < end:
            intervals.append((current, end, False))
        for first, last, close in intervals:
            window = series.crop_window(rule, vertical, close)
            for target in targets[cue]:
                issue = readability_issue(target, window, dimensions)
                if issue:
                    issues.append({**issue, "cue": cue, "start": first / series.FPS,
                                   "end": last / series.FPS, "plan": "close" if close else "wide",
                                   "image_url": project_image_url(root, image)})
    return {"language": language, "aspect": aspect, "resolution": resolution, "total_shots": len(entries),
            "checked_shots": checked, "issues": issues}


def repeats_report(root, query):
    config = project_config(root)
    language = query.get("language", [config.get("source_language")])[0]
    if language not in config["languages"]:
        raise ValueError("Choose a configured language")
    entries = sync_timeline(root, language, config["languages"][language])
    series.blocks(entries)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    images = {series.cue(row): row["image"] for row in series.read(storyboard)}
    identities = {}
    runs = []
    for row in entries:
        cue = series.cue(row)
        if cue not in images:
            raise ValueError(f"Missing storyboard cue: {cue}")
        filename = images[cue]
        if filename not in identities:
            image = project_image(root, str(storyboard.parent / filename))
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            identities[filename] = digest, project_image_url(root, image)
        digest, url = identities[filename]
        start, end = (round(row[key] * series.FPS) for key in ("start", "end"))
        interval = {"cue": cue, "start": start / series.FPS, "end": end / series.FPS}
        if runs and runs[-1]["digest"] == digest:
            runs[-1]["end"] = end
            runs[-1]["occurrences"].append(interval)
        else:
            runs.append({"digest": digest, "cue": cue, "start": start, "end": end,
                         "image_url": url, "occurrences": [interval]})

    def issue(code, first, last, occurrences):
        return {"code": code, "cue": first["cue"], "start": first["start"] / series.FPS,
                "end": last["end"] / series.FPS, "image_url": first["image_url"],
                "occurrences": occurrences}

    issues = []
    by_image = {}
    for run in runs:
        by_image.setdefault(run["digest"], []).append(run)
        if run["end"] - run["start"] > REPEAT_HOLD_SECONDS * series.FPS:
            issues.append(issue("long_hold", run, run, run["occurrences"]))
    for appearances in by_image.values():
        left = 0
        best_start = best_end = 0
        for right, run in enumerate(appearances):
            while run["start"] - appearances[left]["start"] > REPEAT_WINDOW_SECONDS * series.FPS:
                left += 1
            if right - left + 1 > best_end - best_start:
                best_start, best_end = left, right + 1
        if best_end - best_start >= REPEAT_MIN_APPEARANCES:
            best = appearances[best_start:best_end]
            occurrences = [{"cue": run["cue"], "start": run["start"] / series.FPS,
                            "end": run["end"] / series.FPS} for run in best]
            issues.append(issue("frequent_return", best[0], best[-1], occurrences))
    issues.sort(key=lambda row: (row["start"], row["code"]))
    return {"language": language, "total_shots": len(entries), "distinct_images": len(by_image),
            "rules": {"hold_seconds": REPEAT_HOLD_SECONDS, "window_seconds": REPEAT_WINDOW_SECONDS,
                      "min_appearances": REPEAT_MIN_APPEARANCES},
            "issues": issues}


def sync_fragment(root, config, query):
    if any(key in query for key in ("chapter", "cue_start", "cue_end")):
        storyboard = inside(root / config.get("storyboard", "shots.json"), root)
        selection = [query.get(key, [None])[0] for key in ("chapter", "cue_start", "cue_end")]
        return series.select_fragment(series.read(storyboard), *selection)
    return None


def sound_map_report(root, query):
    config = project_config(root)
    language = query.get("language", [config.get("source_language")])[0]
    if language not in config["languages"]:
        raise ValueError("Choose a configured language")
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    cues = {series.cue(row) for row in series.read(storyboard)}
    mapping = series.read_sound_map(root, cues)
    entries = sync_timeline(root, language, config["languages"][language])
    series.blocks(entries)
    fragment = sync_fragment(root, config, query)
    scoped, window = series.slice_timeline(entries, fragment) if fragment else (entries, None)
    covered, plan = series.sound_map_plan(entries, mapping, window) if mapping is not None else (0, [])
    return {"language": language, "configured": mapping is not None, "covered_shots": covered,
            "total_shots": len(scoped), "segments": [
                {"location": location["name"], "audio": location["audio"].relative_to(root).as_posix(),
                 "start": start / series.FPS, "end": end / series.FPS}
                for location, start, end, _ in plan
            ]}


def chapter_report(root, query):
    config = project_config(root)
    master = query.get("master_language", [config.get("source_language")])[0]
    language = query.get("language", [master])[0]
    if master not in config["languages"] or language not in config["languages"]:
        raise ValueError("Choose configured languages for chapter timing and titles")
    entries = sync_timeline(root, master, config["languages"][master])
    series.blocks(entries)
    fragment = sync_fragment(root, config, query)
    if fragment:
        entries, _ = series.slice_timeline(entries, fragment)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    by_cue = {series.cue(shot): series.chapter(shot) for shot in series.read(storyboard)}
    path = inside(root / "chapter-titles.json", root)
    translations = series.read(path) if path.is_file() else {}
    if not isinstance(translations, dict) or not isinstance(translations.get(language, {}), dict):
        raise ValueError("chapter-titles.json must map languages to chapter titles")
    titles = translations.get(language, {})
    chapters = []
    seen = set()
    for row in entries:
        cue = series.cue(row)
        if cue not in by_cue:
            raise ValueError(f"Missing storyboard cue: {cue}")
        identifier = by_cue[cue]
        start, end = (round(row[field] * series.FPS) / series.FPS for field in ("start", "end"))
        if chapters and chapters[-1]["id"] == identifier:
            chapters[-1]["end"] = end
            continue
        if identifier in seen:
            raise ValueError(f"Noncontiguous chapter: {identifier}")
        seen.add(identifier)
        title = titles.get(identifier, identifier)
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"{identifier}: expected a chapter title")
        whole_seconds = int(start)
        hours, rest = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        timestamp = f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
        chapters.append({"id": identifier, "title": " ".join(title.split()),
                         "start": start, "end": end, "timestamp": timestamp})
    issues = [{"code": "too_few_chapters"}] if len(chapters) < 3 else []
    for index, chapter in enumerate(chapters):
        start, end = chapter["start"], chapter["end"]
        displayed_end = int(end) if index < len(chapters) - 1 else end
        if round((end - start) * series.FPS) < 10 * series.FPS or displayed_end - int(start) < 10:
            issues.append({"code": "chapter_too_short", "chapter": chapter["id"]})
    return {"master_language": master, "language": language, "duration": chapters[-1]["end"],
            "ready": not issues, "chapters": chapters, "issues": issues,
            "text": "\n".join(f"{item['timestamp']} {item['title']}" for item in chapters) + "\n"}


def dub_report(code, target, source, window):
    relevant_target, relevant_source = series.sync_blocks(target, source, window)
    originals = {row[0]: row for row in relevant_source}
    targets = {row[0] for row in relevant_target}
    issues = []
    if [row[0] for row in relevant_source] != [row[0] for row in relevant_target]:
        issues.append({
            "code": "block_sequence_mismatch",
            "missing": [row[0] for row in relevant_target if row[0] not in originals],
            "extra": [row[0] for row in relevant_source if row[0] not in targets],
        })
    blocks = []
    for row in relevant_target:
        identifier, start, end = row
        original = originals.get(identifier)
        block = {"id": identifier, "target_start": start / series.FPS,
                 "target_duration": (end - start) / series.FPS, "status": "missing"}
        if original:
            tempo = series.block_tempo(original, row)
            block.update(source_start=original[1] / series.FPS,
                         source_duration=(original[2] - original[1]) / series.FPS,
                         tempo=tempo, status=series.tempo_status(tempo))
        blocks.append(block)
    return {"language": code, "blocks": blocks, "issues": issues,
            "ready": not issues and all(block["status"] != "out_of_range" for block in blocks)}


def sync_report(root, query):
    config = project_config(root)
    languages = config["languages"]
    master = query.get("master_language", [config.get("source_language")])[0]
    selected = query.get("language", [code for code in languages if code != master])
    if master not in languages or any(code not in languages or code == master for code in selected):
        raise ValueError("Choose a configured master and other languages for dubbing")
    if len(selected) != len(set(selected)):
        raise ValueError("Dubbing languages must not contain duplicates")
    entries = sync_timeline(root, master, languages[master])
    target = series.blocks(entries)
    fragment = sync_fragment(root, config, query)
    window = series.slice_timeline(entries, fragment)[1] if fragment else None
    reports = []
    for code in selected:
        try:
            source = series.blocks(sync_timeline(root, code, languages[code]))
        except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
            reports.append({"language": code, "blocks": [], "ready": False,
                            "issues": [{"code": "invalid_narration", "message": str(error)}]})
        else:
            reports.append(dub_report(code, target, source, window))
    start, end = window or (0, target[-1][2])
    return {"master_language": master, "start": start / series.FPS, "duration": (end - start) / series.FPS,
            "languages": reports}


def compare_masters(root, query):
    config = project_config(root)
    languages = config["languages"]
    selected = query.get("language")
    if selected is None:
        selected = [code for code, settings in languages.items() if narration_ready(root, code, settings)]
    if len(selected) < 2 or len(set(selected)) != len(selected) or any(code not in languages for code in selected):
        raise ValueError("Choose at least two distinct configured languages")
    fragment = sync_fragment(root, config, query)
    timings = {}
    errors = {}
    for code in selected:
        try:
            entries = sync_timeline(root, code, languages[code])
            timings[code] = (entries, series.blocks(entries))
        except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
            errors[code] = str(error)
    candidates = []
    for code in selected:
        candidate = {"language": code, "ready": False, "issues": [], "duration": None,
                     "noticeable_blocks": 0, "out_of_range_blocks": 0, "mean_tempo_change": None}
        candidates.append(candidate)
        try:
            if code in errors:
                raise ValueError(errors[code])
            entries, target = timings[code]
            window = series.slice_timeline(entries, fragment)[1] if fragment else None
        except ValueError as error:
            candidate["issues"].append({"language": code, "message": str(error)})
            continue
        start, end = window or (0, target[-1][2])
        candidate["duration"] = (end - start) / series.FPS
        changes = []
        for dub in selected:
            if dub == code:
                continue
            if dub in errors:
                candidate["issues"].append({"language": dub, "message": errors[dub]})
                continue
            report = dub_report(dub, target, timings[dub][1], window)
            candidate["issues"].extend({"language": dub, **issue} for issue in report["issues"])
            for block in report["blocks"]:
                if "tempo" in block:
                    changes.append(abs(block["tempo"] - 1))
                candidate["noticeable_blocks"] += block["status"] == "noticeable"
                candidate["out_of_range_blocks"] += block["status"] == "out_of_range"
        candidate["mean_tempo_change"] = sum(changes) / len(changes) if changes else None
        candidate["ready"] = not candidate["issues"] and not candidate["out_of_range_blocks"]
    source = config.get("source_language")
    candidates.sort(key=lambda item: (not item["ready"], len(item["issues"]), item["out_of_range_blocks"],
                                      item["noticeable_blocks"], item["mean_tempo_change"] or 0,
                                      item["language"] != source, item["language"]))
    return {"recommended": candidates[0]["language"] if candidates[0]["ready"] else None,
            "candidates": candidates}


def sync_audio(root, query, work):
    config = project_config(root)
    master = query.get("master_language", [config.get("source_language")])[0]
    language = query.get("language", [None])[0]
    identifier = query.get("block", [None])[0]
    version = query.get("version", ["source"])[0]
    if master not in config["languages"] or language not in config["languages"] or master == language:
        raise ValueError("Choose two distinct configured languages")
    if version not in ("source", "adjusted") or not identifier:
        raise ValueError("Specify a block and source or adjusted version")
    settings = config["languages"][language]
    combined = work / "combined.wav"
    entries = sync_timeline(root, language, settings, combined)
    source = next((row for row in series.blocks(entries) if row[0] == identifier), None)
    if source is None:
        raise ApiError(404, "Block not found in the selected dub")
    audio = combined if combined.is_file() else inside(
        root / settings.get("audio", f"{language}/narration.wav"), root
    )
    target = source
    tempo = 1
    if version == "adjusted":
        master_entries = sync_timeline(root, master, config["languages"][master])
        target = next((row for row in series.blocks(master_entries) if row[0] == identifier), None)
        if target is None:
            raise ApiError(404, "Block not found in the master")
        tempo = series.block_tempo(source, target)
        if series.tempo_status(tempo) == "out_of_range":
            raise ApiError(422, "Tempo falls outside 0.5–2×; rerecord this block")
    rate = 48000
    samples = rate // series.FPS
    block_filter = series.dub_block_filter(source[1] * samples, source[2] * samples,
                                          (target[2] - target[1]) * samples, tempo)
    output = work / "block.mp3"
    series.call("ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-map", "0:a:0",
                "-af", f"aresample={rate},{block_filter}", "-c:a", "libmp3lame", "-b:a", "192k", str(output))
    return output


def import_narration(payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = project_root(payload.get("path"))
    config = project_config(root)
    language = payload.get("language")
    if not isinstance(language, str) or language not in config["languages"]:
        raise ValueError("Select a language configured for this project")
    source_path = payload.get("manifest")
    folder_path = payload.get("folder")
    if (source_path is None) == (folder_path is None):
        raise ValueError("Specify either a project-relative manifest or a folder of audio clips")
    selected_path = source_path if folder_path is None else folder_path
    if not isinstance(selected_path, str) or not selected_path.strip():
        raise ValueError("Specify the input path inside the project")
    dry_run = payload.get("dry_run", False)
    replace = payload.get("replace", False)
    if not isinstance(dry_run, bool) or not isinstance(replace, bool):
        raise ValueError("dry_run and replace must be booleans")
    source = inside(root / selected_path, root)
    target = inside(root / config["languages"][language].get("utterances", f"{language}/utterances.json"), root)
    if (folder_path is None and source.suffix.lower() != ".json") or target.suffix.lower() != ".json":
        raise ValueError("Expected a JSON manifest")
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    if target.resolve() in {storyboard.resolve(), (root / "series.json").resolve()}:
        raise ValueError("The narration destination conflicts with project metadata")
    ordered_cues = [series.cue(shot) for shot in series.read(storyboard)]
    cues = set(ordered_cues)
    problems = []
    if folder_path is None:
        base = source.parent
        rows = series.read(source)
    else:
        base = source
        if not base.is_dir():
            raise ValueError("Audio folder not found")
        rows, problems = match_narration_folder(base, ordered_cues)
        if problems:
            report = {"language": language, "utterances": len(rows), "issues": problems, "dry_run": dry_run}
            if dry_run:
                return report
            raise ApiError(409, "Resolve unmatched or ambiguous audio files before importing")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty list of utterances")
    normalized = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("cue"), str) or row["cue"] not in cues:
            raise ValueError(f"Utterance {index}: cue must exist in the storyboard")
        audio = row.get("audio")
        if not isinstance(audio, str) or not audio.strip():
            raise ValueError(f"Utterance {index}: missing audio path")
        if "id" in row and (not isinstance(row["id"], str) or not row["id"].strip()):
            raise ValueError(f"Utterance {index}: expected a nonempty block id")
        clip = inside(base / audio, root).resolve()
        if clip.suffix.lower() != ".mp3" or not clip.is_file():
            raise ValueError(f"Utterance {index}: expected an existing MP3 file: {audio}")
        normalized.append({**row, "audio": Path(os.path.relpath(clip, target.parent)).as_posix()})
    timeline = series.time_utterances(target, rows=normalized, verify_pcm=True,
                                      profile=quality.profile(project_config(root)))
    result = {"language": language, "manifest": target.relative_to(root).as_posix(),
              "utterances": len(normalized), "duration": timeline[-1]["end"], "dry_run": dry_run}
    with jobs.lock:
        exists = target.exists()
        if dry_run:
            return {**result, "target_exists": exists}
        if jobs.busy:
            raise ApiError(409, "Wait for the active job before changing narration")
        if exists and not replace:
            raise ApiError(409, "A narration manifest already exists; pass replace: true to replace it")
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             suffix=".tmp", delete=False) as stream:
                pending = Path(stream.name)
                json.dump(normalized, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            if replace:
                pending.replace(target)
            else:
                os.link(pending, target)
        except FileExistsError:
            raise ApiError(409, "A narration manifest already exists") from None
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return result


def editable_narration(root, language):
    config = project_config(root)
    if language not in config["languages"]:
        raise ValueError("Choose a configured narration language")
    settings = config["languages"][language]
    manifest = inside(root / settings.get("utterances", f"{language}/utterances.json"), root)
    if manifest.is_file() or "utterances" in settings:
        entries = sync_timeline(root, language, settings)
        rows = series.read(manifest)
        return manifest, rows, entries, None, str(file_version(manifest))
    timing = inside(root / settings.get("timeline", f"{language}/timeline.json"), root)
    audio = inside(root / settings.get("audio", f"{language}/narration.wav"), root)
    entries = sync_timeline(root, language, settings)
    series.blocks(entries)
    rows = [{**row, "cue": series.cue(row)} for row in entries]
    revision = str((file_version(timing), file_version(audio)))
    return manifest, rows, entries, audio, revision


def narration_rows(root, language):
    _, rows, entries, audio, revision = editable_narration(root, language)
    return {"revision": revision, "legacy": audio is not None,
            "rows": [{"index": index, **entry, "cue": series.cue(entry),
                      "text": row.get("text"), "audio": row.get("audio")}
                     for index, (row, entry) in enumerate(zip(rows, entries))]}


def subtitle_timestamp(milliseconds, separator):
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"


def subtitle_export(root, query, master_entries=None):
    config = project_config(root)
    language = query.get("language", [config.get("source_language")])[0]
    master = query.get("master_language", [language])[0]
    style = query.get("format", ["srt"])[0]
    if language not in config["languages"] or master not in config["languages"] or style not in ("srt", "vtt"):
        raise ValueError("Choose configured languages and SRT or VTT")
    _, script, entries, _, _ = editable_narration(root, language)
    source = series.blocks(entries)
    if master == language:
        master_entries = entries
    elif master_entries is None:
        master_entries = sync_timeline(root, master, config["languages"][master])
    target = source if master == language else series.blocks(master_entries)
    fragment = sync_fragment(root, config, query)
    window = series.slice_timeline(master_entries, fragment)[1] if fragment else None
    target, source = series.sync_blocks(target, source, window)
    if [block[0] for block in source] != [block[0] for block in target]:
        raise ValueError("Narration block IDs and order must match the master")
    timing = {}
    for original, adjusted in zip(source, target):
        if series.tempo_status(series.block_tempo(original, adjusted)) == "out_of_range":
            raise ValueError(f"Block {original[0]} requires a tempo outside the supported range")
        timing[original[0]] = (original, adjusted)
    first, last = window or (0, target[-1][2])
    ms_per_frame = 1000 / series.FPS
    lines = []
    separator = "," if style == "srt" else "."
    for entry, row in zip(entries, script, strict=True):
        identifier = entry.get("id") or series.cue(entry).rsplit(":", 1)[0]
        text = row.get("text")
        if identifier not in timing or not isinstance(text, str) or not text.strip():
            continue
        (_, source_start, source_end), (_, start, end) = timing[identifier]
        factor = (end - start) / (source_end - source_start)
        left, right = [start + (round(entry[field] * series.FPS) - source_start) * factor
                       for field in ("start", "end")]
        begin = round((max(left, first) - first) * ms_per_frame)
        finish = round((min(right, last) - first) * ms_per_frame)
        if finish <= begin:
            continue
        text = " ".join(text.split())
        if style == "vtt":
            text = escape(text, quote=False)
        lines.append(f"{len(lines)+1}\n{subtitle_timestamp(begin, separator)} --> "
                     f"{subtitle_timestamp(finish, separator)}\n{text}\n")
    if not lines:
        raise ValueError("No timed text found in the selected narration")
    prefix = "WEBVTT\n\n" if style == "vtt" else ""
    filename = f"{language}{'-on-' + master if master != language else ''}.{style}"
    mime = "text/vtt" if style == "vtt" else "application/x-subrip"
    return filename, mime + "; charset=utf-8", (prefix + "\n".join(lines)).encode("utf-8")


def split_narration(audio, entries, directory, skip, profile):
    minimum, maximum = quality.shot_frames(profile, series.FPS)
    sample_counts = {}
    with wave.open(str(audio), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        for index, row in enumerate(entries):
            first, last = [round(row[field] * series.FPS) for field in ("start", "end")]
            if not minimum <= last - first <= maximum:
                raise ValueError(f"{series.cue(row)}: legacy cue is outside the configured shot limits")
            start, end = [(frame * rate + series.FPS - 1) // series.FPS for frame in (first, last)]
            if index == skip:
                continue
            source.setpos(min(start, source.getnframes()))
            data = source.readframes(end - start)
            expected = (end - start) * channels * width
            if expected - len(data) > ((rate + series.FPS - 1) // series.FPS) * channels * width:
                raise ValueError(f"{series.cue(row)}: incomplete PCM audio")
            silence = b"\x80" if width == 1 else bytes(width)
            data += silence * ((expected - len(data)) // width)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as clip:
                clip.setparams((channels, width, rate, 0, "NONE", "not compressed"))
                clip.writeframes(data)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "wav", "-i", "pipe:0",
                            "-c:a", "libmp3lame", "-q:a", "2", str(directory / f"{index:05}.mp3")],
                           input=buffer.getvalue(), check=True)
            sample_counts[index] = end - start
    return sample_counts


def replace_narration(payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = project_root(payload.get("path"))
    language, index = payload.get("language"), payload.get("index")
    audio, dry_run = payload.get("audio"), payload.get("dry_run", False)
    if not isinstance(language, str) or type(index) is not int or not isinstance(audio, str) or type(dry_run) is not bool:
        raise ValueError("Specify a language, utterance index, and replacement audio path")
    source = inside(root / audio, root).resolve()
    if source.suffix.lower() != ".mp3" or not source.is_file():
        raise ValueError("Replacement must be an MP3 file inside the project")
    with jobs.lock:
        if jobs.busy and not dry_run:
            raise ApiError(409, "Wait for the active job before replacing narration")
        manifest, rows, entries, legacy, revision = editable_narration(root, language)
        profile = quality.profile(project_config(root))
        if payload.get("revision") != revision:
            raise ApiError(409, "Narration changed; reload the utterance list")
        if not 0 <= index < len(rows):
            raise ValueError("Utterance index is out of range")
        stage = None
        pending = None
        committed = False
        try:
            if legacy is not None:
                stage = Path(tempfile.mkdtemp(prefix="source-clips-", dir=root))
                sample_counts = split_narration(legacy, entries, stage, index, profile)
                for i, samples in sample_counts.items():
                    rows[i]["audio"] = Path(os.path.relpath(stage / f"{i:05}.mp3", manifest.parent)).as_posix()
                    rows[i]["samples"] = samples
            rows[index] = {key: value for key, value in rows[index].items() if key != "samples"}
            rows[index]["audio"] = Path(os.path.relpath(source, manifest.parent)).as_posix()
            updated = series.time_utterances(manifest, rows=rows, verify_pcm=True, profile=profile)
            identifier = updated[index]["id"]
            old = {block[0]: block for block in series.blocks(entries)}[identifier]
            new = {block[0]: block for block in series.blocks(updated)}[identifier]
            if not dry_run:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=manifest.parent,
                                                 suffix=".tmp", delete=False) as stream:
                    pending = Path(stream.name)
                    json.dump(rows, stream, ensure_ascii=False, indent=2)
                pending.replace(manifest)
                committed = True
            return {"cue": updated[index]["cue"], "block": identifier, "dry_run": dry_run,
                    "old_block_duration": (old[2] - old[1]) / series.FPS,
                    "block_duration": (new[2] - new[1]) / series.FPS,
                    "duration": updated[-1]["end"], "revision": str(file_version(manifest)) if committed else revision}
        except (wave.Error, EOFError) as error:
            raise ValueError(f"Legacy narration requires PCM WAV: {error}") from error
        finally:
            if pending:
                pending.unlink(missing_ok=True)
            if stage and not committed:
                shutil.rmtree(stage)


def match_narration_folder(folder, cues):
    names = {}
    for cue in cues:
        names.setdefault(cue.replace(":", "__").casefold(), []).append(cue)
    matches = {}
    issues = []
    for file in sorted(folder.iterdir()):
        if file.suffix.casefold() != ".mp3" or not file.is_file():
            continue
        candidates = names.get(file.stem.casefold(), [])
        if len(candidates) != 1:
            issues.append({"code": "ambiguous_wav" if candidates else "unmatched_wav",
                           "file": file.name, "candidates": candidates})
            continue
        matches.setdefault(candidates[0], []).append(file.name)
    rows = []
    for cue in cues:
        files = matches.get(cue, [])
        if len(files) != 1:
            issues.append({"code": "ambiguous_cue" if files else "missing_wav", "cue": cue, "files": files})
        else:
            rows.append({"cue": cue, "audio": files[0]})
    return rows, issues


def describe_project(root):
    config = project_config(root)
    storyboard = inside(root / config.get("storyboard", "shots.json"))
    shots = series.read(storyboard) if storyboard.is_file() else []
    cues = {series.cue(shot) for shot in shots}
    issues = [] if shots else [{"code": "missing_storyboard", "path": relative(storyboard)}]
    montage_images = {}
    for shot in shots:
        image = inside(storyboard.parent / shot["image"])
        if not image.is_file():
            issues.append({"code": "missing_frame", "cue": series.cue(shot), "path": relative(image)})
        elif image.suffix.lower() in IMAGE_TYPES and image.resolve().is_relative_to(root):
            montage_images[series.cue(shot)] = project_image_url(root, image.resolve())
    glossary = {series.key(text): translations for text, translations in config.get("glossary", {}).items()}
    aliases = {series.key(text): alias for text, alias in config.get("ocr_aliases", {}).items()}
    for language in config["languages"]:
        if series.original_art_language(config, language):
            continue
        for filename, regions in config.get("regions", {}).items():
            for region in regions:
                text = region["text"]
                key = series.key(aliases.get(series.key(text), text))
                if any(character.isalpha() for character in text) and not glossary.get(key, {}).get(language):
                    issues.append({"code": "missing_label_translation", "language": language,
                                   "path": relative(inside(storyboard.parent / filename)), "text": text})
    return {
        **project_info(root, config),
        "source_language": config.get("source_language"),
        "chapters": list(dict.fromkeys(series.chapter(shot) for shot in shots)),
        "cues": [series.cue(shot) for shot in shots],
        "montage": series.read_montage(inside(root / "montage.json", root)),
        "montage_images": montage_images,
        "issues": issues,
        "languages": [language_status(root, code, settings, cues)
                      for code, settings in config["languages"].items()],
    }


def save_montage(payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = project_root(payload.get("path"))
    config = project_config(root)
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    cue = payload.get("cue")
    if not isinstance(cue, str) or not any(series.cue(row) == cue for row in series.read(storyboard)):
        raise ApiError(404, "Cue not found in the storyboard")
    rule = series.montage_rule(payload.get("rule"))
    target = inside(root / "montage.json", root)
    with jobs.lock:
        if jobs.busy:
            raise ApiError(409, "Wait for the active job before changing montage")
        rules = series.read_montage(target)
        if rule == {"mode": "auto"}:
            rules.pop(cue, None)
        else:
            rules[cue] = rule
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
                pending = Path(stream.name)
                json.dump(rules, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            pending.replace(target)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return {"cue": cue, "rule": rule}


def quality_state(root):
    return {"quality": quality.profile(project_config(root)), "warnings": []}


def save_quality(payload, jobs):
    if not isinstance(payload, dict) or payload.keys() != {"path", "quality"}:
        raise ValueError("Expected path and quality")
    root = project_root(payload["path"])
    rules = quality.validate(payload["quality"])
    with jobs.lock:
        if jobs.busy:
            raise ApiError(409, "Wait for the queue before changing quality rules")
        target = inside(root / "series.json", root)
        config = project_config(root)
        config["quality"] = rules
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
                pending = Path(stream.name)
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            pending.replace(target)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    return {"quality": rules, "warnings": []}


def history_directory(root):
    return inside(root / ".build-history", root)


def compare_builds(root, query):
    directory = history_directory(root)
    try:
        before, after = (build_history.load(directory, query.get(key, [None])[0])
                         for key in ("before", "after"))
    except FileNotFoundError as error:
        raise ApiError(404, "Build snapshot not found") from error
    return build_history.compare(before, after)


def language_status(root, code, settings, cues):
    issues = []
    ready = narration_ready(root, code, settings)
    audio_present = ready
    manifest = inside(root / settings.get("utterances", f"{code}/utterances.json"))
    if "utterances" in settings or manifest.is_file():
        if not ready:
            issues.append({"code": "incomplete_utterances", "path": relative(manifest)})
        rows = series.read(manifest) if ready else []
    else:
        timeline = inside(root / settings.get("timeline", f"{code}/timeline.json"))
        audio = inside(root / settings.get("audio", f"{code}/narration.wav"))
        for path, problem in ((timeline, "missing_timeline"), (audio, "missing_audio")):
            if not path.is_file():
                issues.append({"code": problem, "path": relative(path)})
        try:
            rows = series.read(timeline) if timeline.is_file() else []
            if not isinstance(rows, list):
                raise ValueError("Expected a list")
        except (OSError, ValueError):
            issues.append({"code": "invalid_timeline", "path": relative(timeline)})
            rows = []
            ready = False
    audio_ids = []
    for row in rows:
        try:
            cue = series.cue(row)
            if not isinstance(cue, str):
                raise TypeError("Expected a cue string")
        except (AttributeError, KeyError, TypeError):
            issues.append({"code": "invalid_cue"})
            ready = False
            continue
        if cue not in cues:
            issues.append({"code": "unknown_cue", "cue": cue})
            ready = False
        identifier = row.get("id") or cue.rsplit(":", 1)[0]
        if not isinstance(identifier, str) or not identifier.strip():
            issues.append({"code": "invalid_block_id", "cue": cue})
            ready = False
        elif not audio_ids or audio_ids[-1] != identifier:
            audio_ids.append(identifier)
    voices = inside(root / code / "voices.json")
    if not voices.is_file():
        issues.append({"code": "missing_voice_catalog", "path": relative(voices)})
    try:
        script_ids = full_script_ids(inside(root / code / "narration.jsonl", root))
    except (OSError, ValueError):
        script_ids = None
    return {"code": code, "ready": ready, "issues": issues,
            "script_blocks": len(script_ids) if script_ids is not None else None,
            "audio_blocks": len(audio_ids) if audio_present else 0,
            "complete": (ready and audio_ids == script_ids) if script_ids is not None else None}


def diagnostic(code, retry_step, message, language=None, cue=None, file=None, severity="error"):
    file = file.resolve() if file is not None else None
    return {"code": code, "retry_step": retry_step, "message": message, "language": language,
            "cue": cue, "file": relative(file) if file and file.is_relative_to(WORKSPACE) else None,
            "severity": severity}


def full_script_ids(source):
    expected = []
    seen = set()
    with source.open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            identifier = row.get("id") if isinstance(row, dict) else None
            if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
                raise ValueError(f"Expected a unique block id on line {index}")
            seen.add(identifier)
            expected.append(identifier)
    if not expected:
        raise ValueError("The full script has no blocks")
    return expected


def complete_script_issue(root, language, entries):
    source = inside(root / language / "narration.jsonl", root)
    try:
        expected = full_script_ids(source)
    except (OSError, ValueError) as error:
        return diagnostic("invalid_full_script", "prepare_full_script", str(error), language, file=source)
    actual = [identifier for identifier, _, _ in series.blocks(entries)]
    if actual == expected:
        return None
    first = next((index for index, pair in enumerate(zip(expected, actual)) if pair[0] != pair[1]),
                 min(len(expected), len(actual)))
    wanted = expected[first] if first < len(expected) else None
    found = actual[first] if first < len(actual) else None
    return diagnostic("incomplete_script_coverage", "import_audio",
                      f"Audio has {len(actual)} blocks, full script has {len(expected)}. "
                      f"At block {first + 1} expected {wanted or 'end of script'}, got {found or 'end of audio'}.",
                      language, wanted or found, source)


def full_build_issues(payload):
    root = project_root(payload["path"])
    config = project_config(root)
    languages = payload.get("languages") or list(config["languages"])
    if payload.get("youtube"):
        master = payload.get("master_language", config.get("source_language"))
        languages = [master, *(language for language in languages if language != master)]
    issues = []
    for language in languages:
        try:
            entries = sync_timeline(root, language, config["languages"][language])
            issue = complete_script_issue(root, language, entries)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError) as error:
            file = getattr(error, "file", None) or getattr(error, "filename", None)
            issue = diagnostic(getattr(error, "code", "invalid_narration"),
                               getattr(error, "retry_step", "prepare_timing"), str(error),
                               language, getattr(error, "cue", None), Path(file) if file else None)
        if issue is not None:
            issues.append(issue)
    return issues


def project_diagnostics(root, query):
    config = project_config(root)
    profile = quality.profile(config)
    minimum, maximum = quality.shot_frames(profile, series.FPS)
    languages = query.get("language", list(config["languages"]))
    if any(code not in config["languages"] for code in languages) or len(languages) != len(set(languages)):
        raise ValueError("Choose distinct configured languages")
    complete = query.get("require_complete", ["false"])
    if complete not in (["true"], ["false"]):
        raise ValueError("require_complete must be true or false")
    youtube = query.get("youtube", ["false"])
    if youtube not in (["true"], ["false"]):
        raise ValueError("youtube must be true or false")
    issues = []
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            issues.append(diagnostic("missing_tool", "install_media_tools", f"{executable} is unavailable"))
    storyboard = inside(root / config.get("storyboard", "shots.json"), root)
    shots = {}
    marked = set(config.get("ocr_images", []))
    try:
        rows = series.read(storyboard)
        if not isinstance(rows, list) or not rows:
            raise ValueError("Expected a nonempty storyboard")
        for row in rows:
            cue = series.cue(row)
            if cue in shots:
                raise ValueError(f"Duplicate storyboard cue: {cue}")
            name = row["image"]
            image = inside(storyboard.parent / name, root)
            shots[cue] = (name, image)
            if not image.is_file():
                issues.append(diagnostic("missing_frame", "restore_frame", "Storyboard image is missing",
                                         cue=cue, file=image))
            if row.get("ocr") is True:
                marked.add(name)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        code = "invalid_storyboard" if storyboard.is_file() else "missing_storyboard"
        issues.append(diagnostic(code, "edit_storyboard", str(error), file=storyboard))
        return {"ready": False, "issues": issues}

    ocr_path = inside(root / config.get("output", "editions") / "ocr.json", root)
    try:
        ocr = series.read(ocr_path) if ocr_path.is_file() else {}
        if not isinstance(ocr, dict):
            raise ValueError("Expected an OCR cache object")
    except (OSError, ValueError) as error:
        issues.append(diagnostic("invalid_ocr_cache", "rebuild_ocr_cache", str(error), file=ocr_path))
        ocr = {}
    digests = {}
    glossary = {series.key(text): values for text, values in config.get("glossary", {}).items()}
    montage_path = inside(root / "montage.json", root)
    try:
        montage = series.read_montage(montage_path)
    except (OSError, ValueError, TypeError) as error:
        issues.append(diagnostic("invalid_montage", "edit_montage", str(error), file=montage_path))
        montage = {}
    for language in languages:
        settings = config["languages"][language]
        manifest = inside(root / settings.get("utterances", f"{language}/utterances.json"), root)
        timed = inside(root / settings.get("timeline", f"{language}/timeline.json"), root)
        voice = inside(root / settings.get("audio", f"{language}/narration.wav"), root)
        has_clips = "utterances" in settings or manifest.is_file()
        source = manifest if has_clips else timed
        missing = [(source, "missing_utterances" if has_clips else "missing_timeline", "prepare_timing")]
        if not has_clips:
            missing.append((voice, "missing_audio", "import_audio"))
        for file, code, step in missing:
            if not file.is_file():
                issues.append(diagnostic(code, step, "Narration input is missing", language, file=file))
        if any(not file.is_file() for file, _, _ in missing):
            continue
        try:
            if has_clips:
                rows = series.read(manifest)
                if not isinstance(rows, list) or not rows:
                    raise ValueError("Expected a nonempty utterance list")
                clips_missing = False
                for row in rows:
                    clip = inside(manifest.parent / row["audio"], root)
                    if not clip.is_file():
                        issues.append(diagnostic("missing_mp3" if clip.suffix.lower() == ".mp3" else "missing_wav",
                                                 "import_audio", "Utterance audio is missing",
                                                 language, series.cue(row), clip))
                        clips_missing = True
                if clips_missing:
                    continue
            entries = sync_timeline(root, language, settings)
            if complete == ["true"]:
                issue = complete_script_issue(root, language, entries)
                if issue is not None:
                    issues.append(issue)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError) as error:
            file = getattr(error, "file", None) or getattr(error, "filename", None)
            issues.append(diagnostic(getattr(error, "code", "invalid_narration"),
                                     getattr(error, "retry_step", "prepare_timing"), str(error), language,
                                     getattr(error, "cue", None), Path(file) if file else source))
            continue
        previous = 0
        for row in entries:
            try:
                cue = series.cue(row)
                start, end = (round(row[field] * series.FPS) for field in ("start", "end"))
            except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
                issues.append(diagnostic("invalid_timing", "prepare_timing", "Invalid timeline row", language,
                                         file=source))
                continue
            if start != previous or not minimum <= end - start <= maximum:
                issues.append(diagnostic("invalid_timing", "prepare_timing",
                                         f"Expected contiguous {minimum/series.FPS:g}–{maximum/series.FPS:g}s timing",
                                         language, cue, source))
            previous = end
            if cue not in shots:
                issues.append(diagnostic("unknown_cue", "edit_storyboard", "Narration cue has no storyboard row",
                                         language, cue, source))
                continue
            if end > start:
                try:
                    series.close_intervals(start, end, montage.get(cue, {}), cue, profile)
                except ValueError as error:
                    issues.append(diagnostic("invalid_cut", "edit_montage", str(error), language, cue, montage_path))
            name, image = shots[cue]
            if youtube == ["true"] or series.original_art_language(config, language):
                continue
            regions = config.get("regions", {}).get(name)
            if regions is None and name in marked and image.is_file():
                if name not in digests:
                    digests[name] = hashlib.sha256(image.read_bytes()).hexdigest()
                regions = ocr.get(digests[name])
                if regions is None:
                    issues.append(diagnostic("missing_ocr", "scan", "Localized render needs an OCR result",
                                             language, cue, image))
            try:
                series.localized_labels(regions or [], language, config, glossary)
            except ValueError as error:
                issues.append(diagnostic("missing_label_translation", "edit_glossary", str(error),
                                         language, cue, image))
            except (TypeError, KeyError, AttributeError) as error:
                issues.append(diagnostic("invalid_text_region", "edit_text_regions", str(error),
                                         language, cue, image))
    return {"ready": not issues, "issues": issues}


def validate_project_paths(root, config, output, work, codes, command):
    storyboard = inside(root / config.get("storyboard", "shots.json"))
    for shot in series.read(storyboard):
        inside(storyboard.parent / shot["image"])
    inside(output / "ocr.json")
    if command == "scan":
        inside(output / "ocr.tmp")
        return
    inside(root / "montage.json", root)
    history_directory(root)
    cache = inside(work / ".frame-cache")
    if cache.is_dir():
        for item in cache.iterdir():
            inside(item)
    cache = inside(work / ".resume-cache")
    if cache.is_dir():
        for item in cache.iterdir():
            inside(item)
    for code in codes:
        narration_ready(root, code, config["languages"][code])
        manifest = inside(root / config["languages"][code].get("utterances", f"{code}/utterances.json"))
        if manifest.is_file():
            for row in series.read(manifest):
                if isinstance(row, dict) and isinstance(row.get("audio"), str):
                    inside(manifest.parent / row["audio"])
        language_dir = inside(work / code)
        if language_dir.is_dir():
            for item in language_dir.iterdir():
                inside(item)
                if item.name == "frames" and item.is_dir():
                    for frame in item.iterdir():
                        inside(frame)


def job_command(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    allowed = {"path", "command", "languages", "youtube", "resolution", "aspect", "dry_run", "master_language",
               "chapter", "cue_start", "cue_end", "mix", "music", "ambience", "sound_map", "resume",
               "require_complete", "mux_dubs", "sidecars"}
    if unknown := payload.keys() - allowed:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    root = project_root(payload.get("path"))
    config = project_config(root)
    command = payload.get("command", "build")
    resolution = payload.get("resolution", "1080p")
    aspect = payload.get("aspect", "16:9")
    youtube = payload.get("youtube", False)
    mux_dubs = payload.get("mux_dubs", False)
    sidecars = payload.get("sidecars", youtube and not mux_dubs)
    dry_run = payload.get("dry_run", False)
    require_complete = payload.get("require_complete", False)
    mix = payload.get("mix", False)
    sound_map = payload.get("sound_map", False)
    resume = payload.get("resume", command == "build")
    languages = payload.get("languages", [])
    if command not in ("scan", "build"):
        raise ValueError("command must be build or scan")
    if resolution not in ("360p", "1080p"):
        raise ValueError("resolution must be 360p or 1080p")
    if aspect not in series.ASPECTS or (aspect == "9:16" and command != "build"):
        raise ValueError("aspect must be 16:9 or 9:16; vertical output requires build")
    if not all(isinstance(flag, bool) for flag in (youtube, dry_run, mix, sound_map, resume,
                                                    require_complete, mux_dubs, sidecars)):
        raise ValueError("youtube, dry_run, mix, sound_map, resume, require_complete, mux_dubs and sidecars must be booleans")
    if mux_dubs and not youtube:
        raise ValueError("mux_dubs requires a YouTube build")
    if sidecars and (not youtube or mux_dubs):
        raise ValueError("sidecars requires a YouTube build without mux_dubs")
    if require_complete and (command != "build" or any(key in payload for key in ("chapter", "cue_start", "cue_end"))):
        raise ValueError("require_complete needs a whole-project build")
    if resume and command != "build":
        raise ValueError("resume requires build")
    audio_fields = {key: payload[key] for key in ("music", "ambience") if key in payload}
    if (mix and command != "build") or ((audio_fields or sound_map) and not mix):
        raise ValueError("mix requires build; music, ambience and sound_map require mix")
    if mix:
        if not all(isinstance(value, str) for value in audio_fields.values()):
            raise ValueError("Music and ambience paths must be strings")
        series.mix_sources(root, audio_fields.get("music"), audio_fields.get("ambience"))
    if youtube and command != "build":
        raise ValueError("youtube requires build")
    if "master_language" in payload and (
        not youtube or not isinstance(payload["master_language"], str)
        or payload["master_language"] not in config["languages"]
    ):
        raise ValueError("master_language must be a configured language for a YouTube build")
    if not isinstance(languages, list) or not all(
        isinstance(code, str) and code in config["languages"] for code in languages
    ):
        raise ValueError("languages must contain configured language codes")
    if len(languages) != len(set(languages)):
        raise ValueError("languages must not contain duplicates")

    output = inside(root / config.get("output", "editions"))
    fragment_args = {key: payload[key] for key in ("chapter", "cue_start", "cue_end") if key in payload}
    if fragment_args:
        if command != "build" or not all(isinstance(value, str) and value.strip() for value in fragment_args.values()):
            raise ValueError("Supply nonempty fragment boundaries for build")
        shots = series.read(inside(root / config.get("storyboard", "shots.json")))
        fragment = series.select_fragment(shots, fragment_args.get("chapter"),
                                          fragment_args.get("cue_start"), fragment_args.get("cue_end"))
    else:
        fragment = None
    selected = languages or list(config["languages"])
    filename = "video-360p.mp4" if resolution == "360p" else "video.mp4"
    work = inside(series.mix_output(series.aspect_output(series.fragment_output(output, fragment), aspect),
                                    mix, sound_map))
    if youtube:
        source = config.get("source_language")
        master = payload.get("master_language", source)
        if master not in config["languages"]:
            raise ValueError("Choose a configured master_language or set source_language")
        work = inside(series.youtube_output(work, resolution, master, source))
        selected = [master, *(code for code in selected if code != master)]
        outputs = [work / master / filename, *(work / f"{code}.mp3" for code in selected[1:])]
        if mux_dubs:
            outputs.extend(work / code / filename for code in selected[1:])
    elif command == "build":
        outputs = [work / code / filename for code in selected]
    else:
        outputs = [work / "ocr.json"]
    validate_project_paths(root, config, output, work, selected, command)
    outputs = [] if dry_run else [inside(path) for path in outputs]
    for path in outputs:
        inside(path.with_name(path.name + ".checkpoint.json"))
    arguments = [sys.executable, "-u", str(ENGINE / "series.py"), command, str(root)]
    if command == "build":
        arguments.extend(("--resolution", resolution, "--aspect", aspect))
        if not resume:
            arguments.append("--no-resume")
        for key, value in fragment_args.items():
            arguments.append(f"--{key.replace('_', '-')}={value}")
        for language in languages:
            arguments.extend(("--language", language))
        if mix:
            arguments.append("--mix")
            arguments.extend(f"--{key}={value}" for key, value in audio_fields.items())
            if sound_map:
                arguments.append("--sound-map")
    if dry_run:
        arguments.append("--dry-run")
    if youtube:
        arguments.append("--youtube")
        if mux_dubs:
            arguments.append("--mux-dubs")
        if "master_language" in payload:
            arguments.extend(("--master-language", master))
    return arguments, outputs


def youtube_subtitles(payload):
    root = project_root(payload["path"])
    config = project_config(root)
    master = payload.get("master_language", config.get("source_language"))
    languages = payload.get("languages") or list(config["languages"])
    fragment = {key: [payload[key]] for key in ("chapter", "cue_start", "cue_end") if key in payload}
    master_entries = sync_timeline(root, master, config["languages"][master])
    return master, {
        code: subtitle_export(root, {**fragment, "language": [code], "master_language": [master]}, master_entries)[2]
        for code in dict.fromkeys([master, *languages])
    }


def extract_audio(source, target, frames):
    series.encode_export_audio(source, target, frames * (48000 // series.FPS))


def write_youtube_sidecars(video, master, subtitles, registry=None):
    directory = video.parent.parent
    paths = [inside(directory / f"{master}.mp3"),
             *(inside(directory / f"{code}.srt") for code in subtitles)]
    names = [relative(video), relative(paths[0])]
    if not registry or not any(registry.matches(record, names) for record in registry.recent(MAX_JOBS)):
        frames = int(series.call("ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(video)).strip())
        with series.staged_artifact(paths[0]) as pending:
            extract_audio(video, pending, frames)
            pending.replace(paths[0])
    for path, body in zip(paths[1:], subtitles.values(), strict=True):
        if path.is_file() and path.read_bytes() == body:
            continue
        with series.staged_artifact(path) as pending:
            pending.write_bytes(body)
            pending.replace(path)
    return paths


def master_switch_inputs(payload, registry=None):
    if set(payload) - {"path", "command", "video", "tracks", "languages", "master_language", "source_export"}:
        raise ValueError("Unknown fields for switch_master")
    root = project_root(payload.get("path"))
    config = project_config(root)
    languages = payload.get("languages", list(config["languages"]))
    if (not isinstance(languages, list) or not languages or
            not all(isinstance(code, str) and code in config["languages"] for code in languages) or
            len(languages) != len(set(languages))):
        raise ValueError("Choose a nonempty list of distinct configured languages")
    master = payload.get("master_language")
    if master not in languages:
        raise ValueError("Choose a master from the selected languages")
    for field in ("video", "tracks"):
        if not isinstance(payload.get(field), str) or not payload[field] or Path(payload[field]).is_absolute():
            raise ValueError(f"Specify a workspace-relative {field} path")
    video = inside(WORKSPACE / payload["video"], root)
    directory = inside(WORKSPACE / payload["tracks"], root)
    if video.suffix.lower() != ".mp4" or not video.is_file() or not directory.is_dir():
        raise ValueError("Choose an exported MP4 and its aligned track folder")
    candidates = [[inside(directory / f"{code}.{extension}", root)
                   for code in languages for extension in (audio_format, "srt")]
                  for audio_format in ("mp3", "wav")]
    tracks = next((paths for paths in candidates if all(path.is_file() for path in paths)), None)
    if registry is not None:
        identifier = payload.get("source_export")
        records = [registry.load(identifier)] if identifier is not None else registry.recent(MAX_JOBS)
        tracks = next((paths for paths in candidates if any(
            record and record["job"]["path"] == relative(root)
            and registry.matches(record, [relative(path) for path in (video, *paths)])
            for record in records)), None)
    if tracks is None:
        raise ValueError("Choose a registered export containing this video and all selected tracks")
    names = [relative(path) for path in (video, *tracks)]
    versions = [(name, file_version(path)) for name, path in zip(names, (video, *tracks))]
    identity = hashlib.sha256(repr(versions).encode()).hexdigest()[:16]
    target = inside(directory / "switch-master" / identity / master / video.name, root)
    return video, tracks, target, master


def switch_master(payload, registry=None):
    video, tracks, target, master = master_switch_inputs(payload, registry)
    stream = json.loads(series.call("ffprobe", "-v", "error", "-select_streams", "v:0",
                                    "-show_entries", "stream=duration_ts,time_base", "-of", "json",
                                    str(video)))["streams"][0]
    duration = int(stream["duration_ts"]) * Fraction(stream["time_base"])
    for track in tracks:
        if track.suffix == ".wav":
            with wave.open(str(track), "rb") as audio:
                if Fraction(audio.getnframes(), audio.getframerate()) != duration:
                    raise ValueError(f"{track.name} does not match the video clock. Export aligned tracks first.")
        elif track.suffix == ".mp3":
            # MP3 stream headers include encoder delay and frame padding.
            if abs(series.audio_duration(track) - float(duration)) > 0.05:
                raise ValueError(f"{track.name} does not match the video clock")
        else:
            captions = track.read_text(encoding="utf-8-sig")
            ranges = re.findall(r"(?m)^(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})$", captions)
            previous = 0
            for left, right in ranges:
                times = []
                for stamp in (left, right):
                    hours, minutes, seconds, ms = map(int, re.split(r"[:,]", stamp))
                    times.append(((hours * 60 + minutes) * 60 + seconds) * 1000 + ms)
                start, end = times
                if not previous <= start < end <= duration * 1000:
                    raise ValueError(f"{track.name} contains timestamps outside the video clock or out of order")
                previous = end
            if not ranges:
                raise ValueError(f"{track.name} contains no timed subtitles")
    audio = next(track for track in tracks if track.stem == master and track.suffix in (".mp3", ".wav"))
    series.mux_dub(video, audio, master, "switch-master-v1", target=target)
    return [target, *tracks]


def file_version(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def import_export_report(payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = project_root(payload.get("path"))
    report_name = payload.get("report")
    if not isinstance(report_name, str) or not re.fullmatch(r"full-export-[A-Za-z0-9-]+\.json", report_name):
        raise ValueError("Choose a full-export report from the project folder")
    with jobs.lock:
        if jobs.busy:
            raise ApiError(409, "Wait for the active export before importing a report")
        report = inside(root / report_name, root)
        data = series.read(report)
        audio_format = "mp3" if "mp3" in data else "wav"
        audio, captions = data[audio_format], data["srt"]
        config = project_config(root)
        languages = config["languages"]
        if not isinstance(audio, dict) or not isinstance(captions, dict) or not audio or audio.keys() != captions.keys():
            raise ValueError("The report must list matching audio and SRT tracks")
        video = inside(root / data["video"], root)
        if video.suffix != ".mp4":
            raise ValueError("The report must include an MP4 video")
        tracks = []
        for code in audio:
            if code not in languages:
                raise ValueError("The report contains an unconfigured language")
            for ext, value in ((audio_format, audio[code]), ("srt", captions[code])):
                track = inside(root / value, root)
                if track.parent != video.parent or track.name != f"{code}.{ext}":
                    raise ValueError("The report must list sibling tracks named by language")
                tracks.append(track)
        paths = [video, *tracks]
        if any(not path.is_file() or path.stat().st_mtime_ns > report.stat().st_mtime_ns for path in paths):
            raise ValueError("An artifact is missing or newer than the report")
        identifier = hashlib.sha256((relative(report) + ":" + repr(file_version(report))).encode()).hexdigest()[:32]
        job = {"id": identifier, "path": relative(root), "command": "build", "dry_run": False,
               "status": "succeeded", "log": f"Imported from {report_name}", "issues": [], "exit_code": 0,
               "master_language": data.get("master_language", config.get("source_language")),
               "outputs": [{"path": relative(path)} for path in paths]}
        return jobs.registry.save(job)["job"]


def publication_package(payload, jobs):
    if not isinstance(payload, dict) or payload.get("command", "build") != "build":
        raise ValueError("A publication package requires build settings")
    dry_run = payload.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean")
    with jobs.lock:
        if jobs.busy:
            raise ApiError(409, "Wait for the active export before packaging")
        return prepare_publication(payload, dry_run)


def validate_publication_video(path, frames, duration):
    count = series.call("ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)).strip()
    if not count.isdigit() or int(count) != frames:
        raise ValueError("Video frame count differs from the current timeline. Rebuild the edition.")
    if abs(series.audio_duration(path) - duration) > 1 / series.FPS:
        raise ValueError("Video audio differs from the current timeline. Rebuild the edition.")


def prepare_publication(payload, dry_run):
    _, sources = job_command({**payload, "dry_run": False})
    root = project_root(payload.get("path"))
    config = project_config(root)
    youtube = payload.get("youtube", False)
    master = payload.get("master_language", config.get("source_language")) if youtube else None
    languages = payload.get("languages") or list(config["languages"])
    if youtube:
        languages = [master, *(code for code in languages if code != master)]
    metadata_file = inside(root / "publication.json", root)
    metadata = series.read(metadata_file) if metadata_file.is_file() else {}
    if not isinstance(metadata, dict) or not isinstance(metadata.get("languages", {}), dict):
        raise ValueError("publication.json must contain a languages object")
    fragment = {key: [payload[key]] for key in ("chapter", "cue_start", "cue_end") if key in payload}
    files = {}
    extract = {}
    issues = []

    def issue(code, language, message, blocking=True):
        issues.append({"code": code, "language": language, "message": message, "blocking": blocking})

    primary, extras = sources[:len(languages)], sources[len(languages):]
    dubbed = {path.parent.name: path for path in extras}
    for code, source in zip(languages, primary, strict=True):
        if not source.is_file():
            issue("missing_render", code, f"Сначала соберите {source.name} для {code}.")
            continue
        try:
            query = {**fragment, "language": [code], "master_language": [master or code]}
            chapters = chapter_report(root, query)
            expected = round(chapters["duration"] * series.FPS)
            if source.suffix == ".mp4":
                validate_publication_video(source, expected, chapters["duration"])
                files["video.mp4" if youtube else f"{code}/video.mp4"] = source
                extract[f"{code}/audio.mp3"] = (source, expected)
            else:
                if abs(series.audio_duration(source) - chapters["duration"]) > 0.05:
                    raise ValueError("Dub duration differs from the master. Rebuild the edition.")
                files[f"{code}/audio.mp3"] = source
                if code in dubbed:
                    if not dubbed[code].is_file():
                        issue("missing_render", code, f"Сначала соберите {dubbed[code].name} для {code}.")
                    else:
                        validate_publication_video(dubbed[code], expected, chapters["duration"])
                        files[f"{code}/video.mp4"] = dubbed[code]
            for style in ("srt", "vtt"):
                _, _, body = subtitle_export(root, {**query, "format": [style]})
                files[f"{code}/subtitles.{style}"] = body
            files[f"{code}/chapters.txt"] = chapters["text"].encode("utf-8")
            if not chapters["ready"]:
                issue("chapters_not_ready", code, "Главы сохранены отдельно. Проверьте требования YouTube.", False)
            localized = metadata.get("languages", {}).get(code, {})
            if not isinstance(localized, dict):
                raise ValueError("Publication language settings must be an object")
            description = localized.get("description")
            if not isinstance(description, str) or not description.strip():
                issue("missing_description", code, "Добавьте описание языка в publication.json.")
                continue
            cover = project_image(root, localized.get("cover", metadata.get("cover")))
            files[f"{code}/cover{cover.suffix.lower()}"] = cover
            text = description.strip() + "\n"
            if chapters["ready"]:
                text += "\n" + chapters["text"]
            files[f"{code}/description.txt"] = text.encode("utf-8")
        except (OSError, ValueError, TypeError, KeyError, ApiError, subprocess.CalledProcessError) as error:
            issue("invalid_publication_input", code, str(error))
    ready = not any(item["blocking"] for item in issues)
    result = {"ready": ready, "dry_run": dry_run, "issues": issues, "outputs": []}
    if dry_run or not ready:
        return result
    destination = inside(root / "publication", root)
    destination.mkdir(exist_ok=True)
    identifier = uuid.uuid4().hex
    target = destination / identifier
    names = sorted([*files, *extract, "manifest.json"])
    with tempfile.TemporaryDirectory(dir=destination) as work:
        staging = Path(work) / "package"
        staging.mkdir()
        for name, value in files.items():
            path = staging / name
            path.parent.mkdir(exist_ok=True)
            if isinstance(value, Path):
                shutil.copyfile(value, path)
            else:
                path.write_bytes(value)
        for name, (source, frames) in extract.items():
            extract_audio(source, staging / name, frames)
        (staging / "manifest.json").write_text(json.dumps(
            {"edition": {key: value for key, value in payload.items() if key != "dry_run"},
             "languages": languages, "master_language": master, "files": names, "issues": issues},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.rename(target)
    result["folder"] = relative(target)
    result["outputs"] = [
        {"path": relative(target / name),
         "url": "/api/project/package/file?" + urlencode({"path": relative(root), "id": identifier, "file": name})}
        for name in names
    ]
    return result


def publication_file(root, query):
    identifier = query.get("id", [""])[0]
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ApiError(404, "Package not found")
    directory = inside(root / "publication" / identifier, root)
    manifest = inside(directory / "manifest.json", directory)
    name = query.get("file", [""])[0]
    if not manifest.is_file() or name not in series.read(manifest)["files"]:
        raise ApiError(404, "Package file not found")
    return inside(directory / name, directory)


class Previews:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.files = {}
        self.lock = threading.Lock()

    def create(self, root, language):
        config = project_config(root)
        if language not in config["languages"]:
            raise ValueError("Select a configured preview language")
        settings = config["languages"][language]
        manifest = inside(root / settings.get("utterances", f"{language}/utterances.json"), root)
        storyboard = inside(root / config.get("storyboard", "shots.json"), root)
        images = {series.cue(row): row["image"] for row in series.read(storyboard)}
        montage = series.read_montage(inside(root / "montage.json", root))
        identifier = uuid.uuid4().hex
        combined = "utterances" in settings or manifest.is_file()
        temporary = self.directory / f"{identifier}.wav"
        compressed = self.directory / f"{identifier}.mp3"
        staged = temporary if combined else None
        try:
            if combined:
                rows = series.read(manifest)
                if not isinstance(rows, list):
                    raise ValueError("Expected a list of utterances")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("audio"), str):
                        raise ValueError("Every utterance needs an audio path")
                    inside(manifest.parent / row["audio"], root)
                audio = temporary
                entries = series.time_utterances(manifest, audio, rows=rows, profile=quality.profile(config))
                if any(Path(row["audio"]).suffix.lower() == ".mp3" for row in rows):
                    series.encode_export_audio(temporary, compressed, round(entries[-1]["end"] * 48000))
                    temporary.unlink()
                    audio = staged = compressed
            else:
                timeline = inside(root / settings.get("timeline", f"{language}/timeline.json"), root)
                audio = inside(root / settings.get("audio", f"{language}/narration.wav"), root).resolve()
                if audio.suffix.lower() != ".wav" or not audio.is_file():
                    raise ValueError("Preview requires a WAV narration file")
                entries = series.read(timeline)
                rows = entries
            if not isinstance(entries, list) or not entries:
                raise ValueError("Preview requires a nonempty timeline")
            frames = []
            previous = 0
            urls = {}
            for row, script in zip(entries, rows, strict=True):
                cue = series.cue(row)
                start, end = (round(row[field] * series.FPS) for field in ("start", "end"))
                if start != previous or end <= start or cue not in images:
                    raise ValueError(f"Invalid timing or missing storyboard cue: {cue}")
                image = images[cue]
                if image not in urls:
                    urls[image] = project_image_url(root, project_image(root, str(storyboard.parent / image)))
                rule = montage.get(cue, {})
                zoom, x, y = series.focus_crop(rule)
                text, speaker = script.get("text"), script.get("speaker")
                frames.append({"cue": cue, "start": start / series.FPS, "end": end / series.FPS,
                               "text": text if isinstance(text, str) and text.strip() else None,
                               "speaker": speaker if isinstance(speaker, str) and speaker.strip() else None,
                               "crop": {"zoom": zoom, "origin": [x, y]},
                               "image_url": urls[image], "close_intervals": [
                                   [first / series.FPS, last / series.FPS]
                                   for first, last in series.close_intervals(start, end, rule, cue, quality.profile(config))
                               ]})
                previous = end
            with self.lock:
                if len(self.files) >= MAX_PREVIEWS:
                    oldest = next(iter(self.files))
                    _, expired = self.files.pop(oldest)
                    if expired:
                        try:
                            expired.unlink(missing_ok=True)
                        except PermissionError:
                            pass
                self.files[identifier] = (audio, staged)
        except Exception:
            temporary.unlink(missing_ok=True)
            compressed.unlink(missing_ok=True)
            raise
        return {"language": language, "duration": frames[-1]["end"], "frames": frames,
                "close_zoom": series.CLOSE_ZOOM,
                "audio_url": f"/api/project/preview/audio/{identifier}"}

    def audio(self, identifier):
        with self.lock:
            if identifier not in self.files:
                raise ApiError(404, "Preview expired; open it again")
            return self.files[identifier][0]



class Jobs:
    def __init__(self):
        self.registry = ExportRegistry(inside(WORKSPACE / "output" / ".series-exports"),
                                       lambda name: inside(WORKSPACE / name))
        self.jobs = {}
        self.lock = threading.Lock()
        self.active = None
        self.pending = deque()
        self.accepting = True
        self.idle = threading.Event()
        self.idle.set()

    def get(self, identifier):
        with self.lock:
            if identifier in self.jobs:
                return self.jobs[identifier].copy()
            record = self.registry.load(identifier)
            if record:
                return record["job"]
            raise ApiError(404, "Job not found")

    @property
    def busy(self):
        return self.active is not None or bool(self.pending)

    def list(self):
        with self.lock:
            positions = {job["id"]: index for index, (job, _) in enumerate(self.pending, 1)}
            keys = ("id", "path", "command", "dry_run", "status", "exit_code")
            archived = [record["job"] for record in reversed(self.registry.recent(MAX_JOBS))
                        if record["job"]["id"] not in self.jobs]
            return {"jobs": [{**{key: job[key] for key in keys}, "queue_position": positions.get(job["id"])}
                             for job in [*archived, *self.jobs.values()][-MAX_JOBS:]]}

    def submit(self, payload):
        with self.lock:
            if not self.accepting:
                raise ApiError(503, "The server is closing")
            if isinstance(payload, dict) and payload.get("command") == "switch_master":
                master_switch_inputs(payload, self.registry)
            else:
                job_command(payload)
            if len(self.jobs) >= MAX_JOBS:
                finished = next((key for key, job in self.jobs.items()
                                 if job["status"] in ("succeeded", "failed")), None)
                if finished is None:
                    raise ApiError(429, "The export queue is full")
                del self.jobs[finished]
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "path": relative(project_root(payload["path"])),
                   "command": payload.get("command", "build"), "dry_run": payload.get("dry_run", False),
                   "status": "queued", "log": "", "outputs": [], "issues": [], "exit_code": None}
            self.jobs[identifier] = job
            self.pending.append((job, payload))
            if self.idle.is_set():
                self.idle.clear()
                threading.Thread(target=self.drain, daemon=True).start()
            return job.copy()

    def drain(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.active = None
                    self.idle.set()
                    return
                job, payload = self.pending.popleft()
                self.active = job["id"]
                job["status"] = "running"
            self.run(job, payload)

    def run(self, job, payload):
        exit_code = 1
        outputs = []
        issues = []
        reused = set()
        try:
            if job["command"] == "switch_master":
                for path in switch_master(payload, self.registry):
                    name = relative(path)
                    outputs.append({"path": name, "url": "/api/files?" + urlencode({"path": name})})
                with self.lock:
                    job["master_language"] = payload["master_language"]
                    job["log"] = "Video stream copied. Aligned audio and SRT files use the existing video clock."
                exit_code = 0
                return
            arguments, candidates = job_command(payload)
            if payload.get("require_complete"):
                issues = full_build_issues(payload)
                if issues:
                    with self.lock:
                        job["log"] = "\n".join(f"{item['language']}: {item['message']}" for item in issues)[-MAX_LOG:]
                    return
            sidecars = payload.get("sidecars", payload.get("youtube", False) and not payload.get("mux_dubs", False))
            if sidecars:
                master, subtitles = youtube_subtitles(payload)
            before = {path: file_version(path) for path in candidates}
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "SERIES_DIAGNOSTICS": "1"}
            if os.name == "nt":
                # The API remains in its own console. Its jobs run without opening more windows.
                startup = {"creationflags": subprocess.CREATE_NO_WINDOW}
            else:
                startup = {}
            with subprocess.Popen(arguments, cwd=WORKSPACE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding="utf-8", errors="replace", env=environment, **startup) as process:
                for line in process.stdout:
                    if line.startswith(series.REUSE_PREFIX):
                        try:
                            path = Path(json.loads(line[len(series.REUSE_PREFIX):]))
                            if path in candidates:
                                reused.add(path)
                                continue
                        except (ValueError, TypeError):
                            pass
                    if line.startswith(series.DIAGNOSTIC_PREFIX):
                        try:
                            item = json.loads(line[len(series.DIAGNOSTIC_PREFIX):])
                            item["file"] = Path(item["file"]) if item["file"] else None
                            issues.append(diagnostic(**item))
                            continue
                        except (ValueError, TypeError, KeyError, OSError):
                            pass
                    with self.lock:
                        job["log"] = (job["log"] + line)[-MAX_LOG:]
                exit_code = process.wait()
            for path in candidates:
                version = file_version(inside(path))
                if version is not None and (version != before[path] or path in reused) and path.is_file():
                    name = relative(path)
                    outputs.append({"path": name, "url": "/api/files?" + urlencode({"path": name})})
            if exit_code == 0 and sidecars and candidates:
                if len(outputs) != len(candidates):
                    raise ValueError("The export did not produce all requested audio and video files")
                for path in write_youtube_sidecars(candidates[0], master, subtitles,
                                                   self.registry if payload.get("resume", True) else None):
                    name = relative(path)
                    outputs.append({"path": name, "url": "/api/files?" + urlencode({"path": name})})
                with self.lock:
                    job["master_language"] = master
        except Exception as error:
            exit_code = 1
            with self.lock:
                job["log"] = (job["log"] + str(error) + "\n")[-MAX_LOG:]
        finally:
            if exit_code == 0 and outputs:
                try:
                    receipt = self.registry.save({**job, "status": "succeeded", "outputs": outputs,
                                                  "exit_code": 0, "issues": issues})
                    outputs = receipt["job"]["outputs"]
                except (OSError, ValueError) as error:
                    exit_code = 1
                    outputs = []
                    with self.lock:
                        job["log"] = (job["log"] + "\n" + str(error))[-MAX_LOG:]
            if exit_code:
                outputs = []
            with self.lock:
                if exit_code and not issues:
                    issues.append(diagnostic("job_failed", "check_then_build", job["log"].strip()[-2000:]))
                job.update(status="succeeded" if exit_code == 0 else "failed", outputs=outputs,
                           exit_code=exit_code, issues=issues)
                if exit_code and job["command"] == "build":
                    job["retry_payload"] = payload.copy()

    def artifact(self, name, identifier=None):
        records = [self.registry.load(identifier)] if identifier is not None else self.registry.recent(MAX_JOBS)
        if name and any(record and self.registry.matches(record, [name]) for record in records):
            return inside(WORKSPACE / name)
        raise ApiError(404, "Artifact not found or replaced by a newer export")

    def finish_active_job(self):
        with self.lock:
            self.accepting = False
        if not self.idle.is_set():
            print("Waiting for the export queue to finish before closing.", flush=True)
            self.idle.wait()


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        if code == 200 and (self.path == "/api/jobs" or self.path.startswith("/api/jobs/")):
            return
        super().log_request(code, size)

    def reply(self, status, body, content_type, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, data):
        self.reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def authorize(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update(("127.0.0.1", "localhost"))
        if self.headers.get("Host") not in hosts:
            raise ApiError(403, "Use the local server address")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in hosts}:
            raise ApiError(403, "Cross-origin access is unavailable")

    def download(self, file):
        try:
            stream = file.open("rb")
        except FileNotFoundError as error:
            raise ApiError(404, "Artifact no longer exists") from error
        with stream:
            size = os.fstat(stream.fileno()).st_size
            first, last = 0, size - 1
            partial = self.headers.get("Range")
            if partial:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", partial)
                if not match or not any(match.groups()):
                    self.range_error(size)
                    return
                start, end = match.groups()
                if start:
                    first, last = int(start), min(int(end), last) if end else last
                else:
                    first = max(0, size - int(end))
                if first > last:
                    self.range_error(size)
                    return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", MIME_TYPES[file.suffix.lower()])
            self.send_header("Content-Length", str(last - first + 1))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.end_headers()
            if not partial:
                shutil.copyfileobj(stream, self.wfile)
                return
            stream.seek(first)
            remaining = last - first + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def range_error(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def dispatch(self, method):
        try:
            self.authorize()
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            path = query.get("path", [None])[0]
            if method == "GET" and url.path == "/":
                self.reply(200, (ENGINE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            elif method == "GET" and url.path == "/api/health":
                tools = {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe")}
                self.json(200, {"ready": all(tools.values()), "tools": tools})
            elif method == "GET" and url.path == "/api/projects":
                self.json(200, {"projects": discover_projects()})
            elif method == "GET" and url.path == "/api/project":
                self.json(200, describe_project(project_root(path)))
            elif method == "GET" and url.path == "/api/project/quality":
                self.json(200, quality_state(project_root(path)))
            elif method == "GET" and url.path == "/api/project/builds":
                directory = history_directory(project_root(path))
                self.json(200, {"builds": build_history.list_builds(directory)})
            elif method == "GET" and url.path == "/api/project/builds/diff":
                self.json(200, compare_builds(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/characters":
                self.json(200, project_characters(project_root(path), query.get("cue", [None])[0]))
            elif method == "GET" and url.path == "/api/project/library":
                self.json(200, project_library(project_root(path), query.get("kind", [None])[0],
                                               query.get("cue", [None])[0]))
            elif method == "GET" and url.path == "/api/project/continuity":
                self.json(200, continuity_report(project_root(path)))
            elif method == "GET" and url.path == "/api/project/diagnostics":
                self.json(200, project_diagnostics(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/readability":
                self.json(200, readability_report(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/repeats":
                self.json(200, repeats_report(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/subtitles":
                filename, mime, body = subtitle_export(project_root(path), query)
                self.reply(200, body, mime, filename)
            elif method == "GET" and url.path == "/api/project/sound-map":
                self.json(200, sound_map_report(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/chapters":
                self.json(200, chapter_report(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/package/file":
                self.download(publication_file(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/regions":
                root = project_root(path)
                config = project_config(root)
                cue = query.get("cue", [None])[0]
                self.json(200, region_state(root, config, cue, region_frame(root, config, cue)))
            elif method == "GET" and url.path == "/api/project/image":
                self.download(project_image(project_root(path), query.get("image", [None])[0]))
            elif method == "GET" and url.path == "/api/project/preview":
                self.json(200, self.server.previews.create(project_root(path), query.get("language", [None])[0]))
            elif method == "GET" and url.path == "/api/project/sync":
                self.json(200, sync_report(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/sync/masters":
                self.json(200, compare_masters(project_root(path), query))
            elif method == "GET" and url.path == "/api/project/sync/audio":
                with tempfile.TemporaryDirectory() as work:
                    self.download(sync_audio(project_root(path), query, Path(work)))
            elif method == "GET" and url.path == "/api/narration/utterances":
                self.json(200, narration_rows(project_root(path), query.get("language", [None])[0]))
            elif method == "GET" and url.path == "/api/narration/pronunciation":
                language = query.get("language", [None])[0]
                rules = read_pronunciation(pronunciation_path(project_root(path), language))
                self.json(200, {"language": language, "rules": rules})
            elif method == "GET" and url.path == "/api/narration/tts":
                body = narration_for_tts(project_root(path), query.get("language", [None])[0])
                self.reply(200, body, "application/x-ndjson; charset=utf-8")
            elif method == "GET" and url.path == "/api/narration/tts/settings":
                self.json(200, tts_settings(project_root(path), query))
            elif method == "GET" and url.path in {"/api/narration/tts/health", "/api/narration/tts/voices"}:
                self.json(200, call_tts_bridge(tts_settings(project_root(path), query), url.path.rsplit("/", 1)[1]))
            elif method == "GET" and (preview := re.fullmatch(r"/api/project/preview/audio/([a-f0-9]{32})", url.path)):
                self.download(self.server.previews.audio(preview[1]))
            elif method == "GET" and (job := re.fullmatch(r"/api/jobs/([a-f0-9]{32})", url.path)):
                self.json(200, self.server.jobs.get(job[1]))
            elif method == "GET" and url.path == "/api/jobs":
                self.json(200, self.server.jobs.list())
            elif method == "GET" and url.path == "/api/files":
                self.download(self.server.jobs.artifact(path, query.get("export", [None])[0]))
            elif method == "POST" and url.path in {"/api/jobs", "/api/projects", "/api/narration/import",
                                                   "/api/narration/replace", "/api/narration/pronunciation",
                                                   "/api/project/montage", "/api/project/regions",
                                                   "/api/project/regions/preview", "/api/project/package",
                                                   "/api/project/quality", "/api/narration/tts/synthesize",
                                                   "/api/exports/import"}:
                if self.headers.get_content_type() != "application/json":
                    raise ApiError(415, "Send application/json")
                length = int(self.headers.get("Content-Length", "0"))
                limit = 256 if url.path == "/api/narration/pronunciation" else 16
                if not 0 < length <= limit * 1024:
                    raise ApiError(413, f"Expected a JSON body of up to {limit} KiB")
                payload = json.loads(self.rfile.read(length))
                if url.path == "/api/exports/import":
                    self.json(200, import_export_report(payload, self.server.jobs))
                elif url.path == "/api/narration/tts/synthesize":
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a JSON object")
                    options = {key: [payload[key]] for key in ("language", "provider") if key in payload}
                    profile = tts_settings(project_root(payload.get("path")), options)
                    speech = call_tts_bridge(profile, "synthesize", payload)
                    with tempfile.TemporaryDirectory() as work:
                        output = Path(work) / "speech.mp3"
                        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "wav", "-i", "pipe:0",
                                        "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-q:a", "2", str(output)],
                                       input=speech, check=True)
                        self.reply(200, output.read_bytes(), "audio/mpeg", "speech.mp3")
                elif url.path == "/api/project/montage":
                    self.json(200, save_montage(payload, self.server.jobs))
                elif url.path == "/api/project/quality":
                    self.json(200, save_quality(payload, self.server.jobs))
                elif url.path == "/api/project/package":
                    result = publication_package(payload, self.server.jobs)
                    self.json(201 if result.get("folder") else 200, result)
                elif url.path.startswith("/api/project/regions"):
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a JSON object")
                    root = project_root(payload.get("path"))
                    if url.path.endswith("/preview"):
                        with tempfile.TemporaryDirectory() as work:
                            self.download(preview_regions(root, payload, Path(work)))
                    else:
                        self.json(200, save_regions(root, payload, self.server.jobs))
                elif url.path == "/api/narration/import":
                    self.json(200, import_narration(payload, self.server.jobs))
                elif url.path == "/api/narration/replace":
                    self.json(200, replace_narration(payload, self.server.jobs))
                elif url.path == "/api/narration/pronunciation":
                    self.json(200, save_pronunciation(payload, self.server.jobs))
                elif url.path == "/api/projects":
                    result = create_project(payload)
                    self.json(200 if result["dry_run"] else 201, result)
                else:
                    self.json(202, self.server.jobs.submit(payload))
            else:
                raise ApiError(404, "Endpoint not found")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            pass
        except ApiError as error:
            self.json(error.status, {"error": str(error)})
        except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
            self.json(400, {"error": str(error)})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


def main():
    global WORKSPACE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE,
                        help="Folder containing your series projects (default: parent of the engine)")
    parser.add_argument("--open-browser", action="store_true", help="Open the UI after the server starts")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    WORKSPACE = args.workspace.resolve()
    if not WORKSPACE.is_dir():
        parser.error("--workspace must be an existing folder")
    try:
        with tempfile.TemporaryDirectory(prefix="series-preview-", ignore_cleanup_errors=True) as previews, \
             ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            server.jobs = Jobs()
            server.previews = Previews(previews)
            address = f"http://127.0.0.1:{server.server_port}"
            print(f"Series Engine: {address}", flush=True)
            if args.open_browser:
                threading.Thread(target=webbrowser.open, args=(address,), daemon=True).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                server.jobs.finish_active_job()
    except OSError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
