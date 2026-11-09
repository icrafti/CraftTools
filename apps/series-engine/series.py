"""Localize shared storyboard frames and assemble language editions with FFmpeg."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import wave
from contextlib import ExitStack
from itertools import groupby
from pathlib import Path

from audio_mix import mix_audio, render_location_bed, validate_bed
from checkpoints import cached_stage, commit, current, signature as checkpoint_key
import build_history
import quality

FPS = 25
CLOSE_ZOOM = 1.08
RESOLUTIONS = {"1080p": (1920, 1080), "360p": (640, 360)}
ASPECTS = ("16:9", "9:16")
FRAME_SIZE = RESOLUTIONS["1080p"]
DIAGNOSTIC_PREFIX = "SERIES_DIAGNOSTIC "
REUSE_PREFIX = "SERIES_REUSE "


class BuildIssue(ValueError):
    def __init__(self, message, code, retry_step, cue_id=None, file=None):
        super().__init__(message)
        self.code = code
        self.retry_step = retry_step
        self.cue = cue_id
        self.file = file


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def key(text):
    return " ".join(text.casefold().split())


def call(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, encoding="utf-8")


def cue(row):
    return row.get("cue") or f"{row['id']}:{row.get('part', 1)}"

def pronunciation_rules(rules):
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Pronunciation dictionary must contain up to 100 rules")
    result = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Each pronunciation rule must be an object")
        term, spoken = rule.get("term"), rule.get("spoken")
        whole_word = rule.get("whole_word", True)
        if not all(isinstance(value, str) and 0 < len(value.strip()) <= 160
                   and not any(char in value for char in "\r\n") for value in (term, spoken)):
            raise ValueError("Each pronunciation rule needs a term and spoken form on one line")
        if type(whole_word) is not bool:
            raise ValueError("whole_word must be a boolean")
        term = term.strip()
        if term.casefold() in seen:
            raise ValueError(f"Duplicate pronunciation term: {term}")
        seen.add(term.casefold())
        result.append({"term": term, "spoken": spoken.strip(), "whole_word": whole_word})
    return result


def pronunciation_transform(rules):
    if not rules:
        return lambda value: value
    alternatives = []
    spoken = {}
    for index, rule in enumerate(sorted(rules, key=lambda item: -len(item["term"]))):
        name = f"rule_{index}"
        term = re.escape(rule["term"])
        pattern = rf"(?<!\w){term}(?!\w)" if rule["whole_word"] else term
        alternatives.append(f"(?P<{name}>{pattern})")
        spoken[name] = rule["spoken"]
    matcher = re.compile("|".join(alternatives), re.IGNORECASE)
    return lambda value: matcher.sub(lambda match: spoken[match.lastgroup], value)


def montage_rule(rule):
    if not isinstance(rule, dict) or rule.get("mode") not in ("auto", "wide", "close", "cut"):
        raise ValueError("Montage mode must be auto, wide, close or cut")
    mode = rule["mode"]
    result = {"mode": mode}
    if mode == "cut":
        at = rule.get("at")
        if type(at) not in (int, float) or not 0.04 <= at <= 8.96:
            raise ValueError("Cut time must be between 0.04 and 8.96 seconds")
        result["at"] = round(at * FPS) / FPS
    if "focus" in rule:
        box = rule["focus"]
        if not isinstance(box, list) or len(box) != 4 or any(
            type(value) not in (int, float) or not 0 <= value <= 1 for value in box
        ):
            raise ValueError("Focus must be a normalized [x, y, width, height] box")
        x, y, width, height = box
        if not width or not height or x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
            raise ValueError("Focus box must fit inside the image and have positive area")
        result["focus"] = box
    if "vertical_center" in rule:
        center = rule["vertical_center"]
        if type(center) not in (int, float) or not 0 <= center <= 1:
            raise ValueError("Vertical center must be between 0 and 1")
        result["vertical_center"] = center
    return result


def vertical_center(rule):
    if "vertical_center" in rule:
        return rule["vertical_center"]
    if "focus" in rule:
        x, _, width, _ = rule["focus"]
        return x + width / 2
    return .5


def focus_crop(rule):
    if "focus" not in rule:
        return CLOSE_ZOOM, .5, .5
    x, y, width, height = rule["focus"]
    # Leave a small margin around large subjects for pixel rounding in the renderer.
    visible = min(1, max(1 / CLOSE_ZOOM, width + .004, height + .004))
    if visible == 1:
        return 1, .5, .5
    shift = 1 - visible
    left = min(shift, max(0, x + (width - visible) / 2))
    top = min(shift, max(0, y + (height - visible) / 2))
    return 1 / visible, left / shift, top / shift


def close_crop(rule, vertical):
    return (CLOSE_ZOOM, .5, .5) if vertical else focus_crop(rule)


def output_dimensions(resolution, vertical):
    width, height = RESOLUTIONS[resolution]
    return (height, width) if vertical else (width, height)


def crop_window(rule, vertical, close):
    width, height = FRAME_SIZE
    left = 0
    if vertical:
        width = height * 9 / 16
        left = min(FRAME_SIZE[0] - width, max(0, FRAME_SIZE[0] * vertical_center(rule) - width / 2))
    zoom, x, y = close_crop(rule, vertical) if close else (1, 0, 0)
    return left + width * (1 - 1 / zoom) * x, height * (1 - 1 / zoom) * y, width / zoom, height / zoom


def read_montage(path):
    rules = read(path) if path.is_file() else {}
    if not isinstance(rules, dict):
        raise ValueError("montage.json must contain rules keyed by cue")
    return {identifier: montage_rule(rule) for identifier, rule in rules.items()}


def close_intervals(start, end, rule, identifier, profile=None):
    mode = rule.get("mode", "auto")
    if mode == "wide":
        return []
    if mode == "close":
        return [(start, end)]
    if mode == "cut":
        cut = start + round(rule["at"] * FPS)
        if cut >= end:
            raise ValueError(f"{identifier}: manual cut must fall inside the shot ({(end-start)/FPS:.2f}s)")
        return [(cut, end)]
    low, high = quality.cut_frames(profile or quality.DEFAULTS, FPS)
    length = end - start
    if length < low + high:
        return []
    count = (length + high - 1) // high
    if count * low > length:
        raise ValueError(f"{identifier}: shot cannot be divided within the configured cut interval")
    return [(start + round(length * part / count), start + round(length * (part + 1) / count))
            for part in range(1, count, 2)]



def chapter(row):
    return row.get("chapter") or cue(row).split(":", 1)[0].partition("-")[0]


def select_fragment(shots, chapter_id=None, start=None, end=None):
    if chapter_id is None and start is None and end is None:
        return None
    if chapter_id is not None:
        if start is not None or end is not None:
            raise ValueError("Choose a chapter or a cue range")
        indices = [index for index, row in enumerate(shots) if chapter(row) == chapter_id]
        if not indices or indices[-1] - indices[0] + 1 != len(indices):
            raise ValueError("Chapter is missing or noncontiguous in the storyboard")
        first, last = indices[0], indices[-1]
    else:
        ids = [cue(row) for row in shots]
        if start not in ids or end not in ids or ids.index(start) > ids.index(end):
            raise ValueError("Specify two existing cues in storyboard order")
        first, last = ids.index(start), ids.index(end)
    return tuple(cue(row) for row in shots[first:last + 1])


def fragment_output(output, fragment):
    if fragment is None:
        return output
    digest = hashlib.sha256(json.dumps(fragment).encode()).hexdigest()[:16]
    return output / "fragments" / digest


def aspect_output(output, aspect):
    return output / "vertical" if aspect == "9:16" else output


def mix_output(output, enabled, sound_map=False):
    if not enabled:
        return output
    return output / "mixed" / "mapped" if sound_map else output / "mixed"


def mix_sources(root, music, ambience):
    sources = []
    for value in (music, ambience):
        if value is None:
            sources.append(None)
            continue
        if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
            raise ValueError("Specify background audio relative to the project")
        path = (root / value).resolve()
        if (not path.is_relative_to(root) or path.suffix.lower() not in (".wav", ".mp3", ".flac", ".ogg", ".m4a")
                or not path.is_file()):
            raise ValueError(f"Background audio must be an existing WAV, MP3, FLAC, OGG or M4A inside the project: {value}")
        sources.append(path)
    return tuple(sources)


def read_sound_map(root, cues):
    path = (root / "sound-map.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError("sound-map.json must be inside the project")
    if not path.is_file():
        return None
    data = read(path)
    if not isinstance(data, dict) or not all(isinstance(data.get(key), dict) for key in ("locations", "cues")):
        raise ValueError("sound-map.json must contain locations and cues objects")
    locations = {}
    for identifier, item in data["locations"].items():
        if (not identifier.strip() or not isinstance(item, dict)
                or not all(isinstance(item.get(key), str) and item[key].strip() for key in ("name", "audio"))):
            raise ValueError(f"{identifier}: sound-map location needs name and audio")
        audio, _ = mix_sources(root, item["audio"], None)
        locations[identifier] = {"name": item["name"], "audio": audio}
    for cue_id, identifier in data["cues"].items():
        if cue_id not in cues or not isinstance(identifier, str) or identifier not in locations:
            raise ValueError(f"{cue_id}: unknown storyboard cue or sound-map location")
    return locations, data["cues"]


def sound_map_plan(entries, mapping, window=None):
    locations, assignments = mapping
    first, last = window or (0, round(entries[-1]["end"] * FPS))
    runs = []
    covered = 0
    for row in entries:
        location = assignments.get(cue(row))
        if location is None:
            continue
        start, end = (round(row[key] * FPS) for key in ("start", "end"))
        covered += start < last and end > first
        if runs and runs[-1][0] == location and runs[-1][2] == start:
            runs[-1] = (location, runs[-1][1], end)
        else:
            runs.append((location, start, end))
    return covered, [
        (locations[location], max(start, first) - first, min(end, last) - first, max(0, first - start))
        for location, start, end in runs if start < last and end > first
    ]


def render_mix(voice, output, duration, mix, offset, plan):
    location_bed = None
    if plan:
        location_bed = output.with_name("location.wav")
        render_location_bed(location_bed, duration, FPS, [
            (start, end, location["audio"], offset_frames) for location, start, end, offset_frames in plan
        ])
    mix_audio(voice, output, duration, FPS, *mix, offset_frames=offset, location_bed=location_bed)


def slice_timeline(entries, fragment):
    selected = set(fragment)
    indices = [index for index, row in enumerate(entries) if cue(row) in selected]
    if not indices or indices[-1] - indices[0] + 1 != len(indices):
        raise ValueError("The chosen fragment has no contiguous narration")
    rows = entries[indices[0]:indices[-1] + 1]
    start = round(rows[0]["start"] * FPS)
    end = round(rows[-1]["end"] * FPS)
    rebased = [{**row, **{field: (round(row[field] * FPS) - start) / FPS
                         for field in ("start", "end")}} for row in rows]
    return rebased, (start, end)


def reuse_output(target):
    print(f"Resume: reused {target.name}")
    if os.environ.get("SERIES_DIAGNOSTICS") == "1":
        print(REUSE_PREFIX + json.dumps(str(target)))


def stage_audio(args, stack, stage, inputs, produce):
    if args.resume:
        key = checkpoint_key(args.cache_version, stage, inputs)
        return cached_stage(args.cache_directory, stage, key, produce)
    target = Path(stack.enter_context(tempfile.TemporaryDirectory())) / f"{stage}.wav"
    produce(target)
    return target


def slice_audio(audio, entries, window, args, stack):
    if audio is None:
        return None
    if abs(audio_duration(audio) - entries[-1]["end"]) > 1 / FPS:
        raise ValueError("Narration audio duration differs from its timeline")
    if args.dry_run:
        return None
    start, end = window
    return stage_audio(args, stack, "fragment", [audio, window], lambda target: call(
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start/FPS:.2f}", "-i", str(audio),
        "-t", f"{(end-start)/FPS:.2f}", "-map", "0:a:0", "-c:a", "pcm_s16le", str(target)))


def time_utterances(manifest, audio_path=None, *, rows=None, verify_pcm=False, profile=None):
    if rows is None:
        rows = read(manifest)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{manifest}: expected a nonempty list of utterances")
    timeline = []
    position = 0
    audio_format = None
    minimum, maximum = quality.shot_frames(profile or quality.DEFAULTS, FPS)
    with ExitStack() as stack:
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not all(
                isinstance(row.get(field), str) and row[field].strip() for field in ("cue", "audio")
            ):
                raise ValueError(f"{manifest}: utterance {index} needs cue and audio")
            clip = manifest.parent / row["audio"]
            try:
                with wave.open(str(clip), "rb") as source:
                    clip_format = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                    channels, width, rate = clip_format
                    if audio_format is None:
                        audio_format = clip_format
                        if audio_path is not None:
                            output = stack.enter_context(wave.open(str(audio_path), "wb"))
                            output.setparams((*audio_format, 0, "NONE", "not compressed"))
                    elif clip_format != audio_format:
                        raise ValueError(f"{clip}: all clips in a language must use the same PCM format")
                    samples = source.getnframes()
                    if not 0 < samples * FPS <= maximum * rate:
                        raise ValueError(f"{clip}: empty or longer than {maximum/FPS:g} seconds for {row['cue']}; split long phrases by cue")
                    # Use absolute frame boundaries so rounding never accumulates across clips.
                    start_sample = (position * rate + FPS - 1) // FPS
                    end = max(position + minimum, ((start_sample + samples - 1) * FPS) // rate + 1)
                    end_sample = (end * rate + FPS - 1) // FPS
                    if audio_path is not None:
                        pcm = source.readframes(samples)
                        if len(pcm) != samples * channels * width:
                            raise ValueError(f"{clip}: truncated WAV data")
                        silence = b"\x80" if width == 1 else bytes(width)
                        output.writeframesraw(pcm + silence * ((end_sample - start_sample - samples) * channels))
                    elif verify_pcm:
                        source.setpos(samples - 1)
                        if len(source.readframes(1)) != channels * width:
                            raise ValueError(f"{clip}: truncated WAV data")
            except FileNotFoundError as error:
                raise BuildIssue(f"{clip}: utterance WAV is missing", "missing_wav", "import_audio",
                                 row["cue"], clip) from error
            except (wave.Error, EOFError) as error:
                raise BuildIssue(f"{clip}: expected a PCM WAV ({error})", "invalid_wav", "import_audio",
                                 row["cue"], clip) from error
            timeline.append({"cue": row["cue"], "id": row.get("id", row["cue"].rsplit(":", 1)[0]),
                             "start": position / FPS, "end": end / FPS})
            position = end
    return timeline


def scan(images, dest, dry_run):
    old = read(dest) if dest.is_file() else {}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in images.items()}
    missing = {}
    for name, digest in hashes.items():
        if digest not in old:
            missing.setdefault(digest, images[name])
    print(f"OCR: {len(missing)} new images, {len(hashes)} frame references")
    if dry_run or not missing:
        return
    with tempfile.TemporaryDirectory() as temp:
        request, result = Path(temp) / "request.json", Path(temp) / "result.json"
        request.write_text(json.dumps([{"id": h, "path": str(p)} for h, p in missing.items()]),
                           encoding="utf-8")
        call("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(Path(__file__).with_name("ocr-windows.ps1")), str(request), str(result))
        old.update(read(result))
    dest.parent.mkdir(parents=True, exist_ok=True)
    pending = dest.with_suffix(".tmp")
    pending.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(dest)


def translation(text, lang, glossary, offline, art_language="en"):
    exact = glossary.get(key(text), {}).get(lang)
    if exact is not None:
        return exact
    if lang.split("-")[0].lower() == art_language.split("-")[0].lower():
        return text
    if offline:
        from argostranslate.translate import get_translation_from_codes
        return get_translation_from_codes(art_language.split("-")[0].lower(), lang.split("-")[0].lower()).translate(text)
    raise ValueError(f"{lang}: missing glossary translation for {text!r}; add it or use --argos")


def original_art_language(config, lang):
    return lang.split("-")[0].lower() == quality.profile(config)["art_language"].split("-")[0].lower()


def localized_labels(regions, lang, config, glossary, offline=False):
    labels = []
    aliases = {key(k): v for k, v in config.get("ocr_aliases", {}).items()}
    for region in regions:
        text = aliases.get(key(region["text"]), region["text"])
        if config.get("glossary_only") and key(text) not in glossary:
            continue
        translated = translation(text, lang, glossary, offline, quality.profile(config)["art_language"])
        if key(translated) != key(region["text"]):
            x, y, w, h = region["box"]
            left, top = max(0, x - .006), max(0, y - .009)
            labels.append((translated, [left, top, min(1, x+w+.006)-left, min(1, y+h+.009)-top],
                           region.get("angle", 0)))
    return labels


def localize(image, labels, font, work, cache_version=None):
    style = [3, labels, font, FRAME_SIZE]
    if cache_version:
        style.extend([cache_version, checkpoint_key(Path(font)) if labels else None])
    signature = hashlib.sha256(image.read_bytes() + json.dumps(
        style, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    name = f"{signature}.jpg"
    target = work / name
    if target.is_file() and (not cache_version or current(target, signature)):
        return target
    filters = [f"scale={FRAME_SIZE[0]}:{FRAME_SIZE[1]}:flags=lanczos", "setsar=1"]
    font_arg = str(font).replace("\\", "/").replace(":", r"\:")
    with tempfile.TemporaryDirectory(dir=work) as temp:
        staging = Path(temp)
        graph = ["[0:v]" + ",".join(filters) + "[base0]"]
        for index, (value, box, angle) in enumerate(labels):
            x, y, w, h = (round(a * b) for a, b in zip(box, (*FRAME_SIZE, *FRAME_SIZE)))
            w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
            padding = 5
            units = sum(1 if unicodedata.east_asian_width(c) in "WF" else .57 for c in value)
            size = max(8, min(h - 2 * padding, int((w - 2 * padding) / max(units, 1))))
            textfile = f"label-{index}.txt"
            (staging / textfile).write_text(value, encoding="utf-8")
            graph.append(
                f"color=c=0x111821:s={w}x{h}:r=25,format=rgba,"
                f"drawtext=fontfile='{font_arg}':textfile={textfile}:expansion=none:"
                f"fontcolor=white:fontsize={size}:x=(w-tw)/2:y=(h-th)/2,"
                f"rotate={angle}:ow=rotw({angle}):oh=roth({angle}):c=none[label{index}]"
            )
            graph.append(
                f"[base{index}][label{index}]overlay=x='{x}+({w}-overlay_w)/2':"
                f"y='{y}+({h}-overlay_h)/2':shortest=1[base{index+1}]"
            )
        call("ffmpeg", "-nostdin", "-v", "error", "-i", str(image), "-filter_complex_threads", "1",
             "-filter_complex", ";".join(graph), "-map", f"[base{len(labels)}]",
             "-frames:v", "1", "-pix_fmt", "yuvj420p", "-q:v", "2", "-update", "1",
             "frame.jpg", cwd=staging)
        (staging / "frame.jpg").replace(target)
        if cache_version:
            commit(target, signature)
    return target


def build(lang, entries, audio, frames, images, marked, config, ocr, glossary, out, args, montage, records):
    plans = []
    close_plans = []
    vertical_plans = []
    vertical = args.aspect == "9:16"
    last = 0
    font = config.get("fonts", {}).get(lang, "C:/Windows/Fonts/arial.ttf")
    overrides = config.get("regions", {})
    use_original_text = args.youtube or original_art_language(config, lang)
    minimum, maximum = quality.shot_frames(args.quality, FPS)
    for row in entries:
        shot = cue(row)
        start, end = (round(row[field] * FPS) for field in ("start", "end"))
        if start != last or not minimum <= end - start <= maximum:
            raise BuildIssue(f"Invalid or noncontiguous {minimum/FPS:g}–{maximum/FPS:g}s timing for {shot}", "invalid_timing",
                             "prepare_timing", shot)
        last = end
        if shot not in frames:
            raise BuildIssue(f"No storyboard frame for {shot}", "unknown_cue", "edit_storyboard", shot)
        name = frames[shot]
        if not images[name].is_file():
            raise BuildIssue(f"Missing storyboard image for {shot}", "missing_frame", "restore_frame",
                             shot, images[name])
        regions = [] if use_original_text else overrides.get(name)
        if regions is None and name in marked:
            digest = hashlib.sha256(images[name].read_bytes()).hexdigest()
            if digest not in ocr:
                raise BuildIssue(f"OCR cache missing for {name}; run scan first", "missing_ocr",
                                 "scan", shot, images[name])
            regions = ocr[digest]
        try:
            labels = localized_labels(regions or [], lang, config, glossary, args.argos)
        except ValueError as error:
            raise BuildIssue(str(error), "missing_label_translation", "edit_glossary", shot, images[name]) from error
        plans.append((start, end, name, labels))
        rule = montage.get(shot, {})
        if vertical:
            center = vertical_center(rule)
            if center != .5:
                vertical_plans.append(f"{center-.5:+.9f}*between(n,{start},{end-1})")
        crop = close_crop(rule, vertical)
        close_plans.extend((first, last, crop) for first, last in close_intervals(start, end, rule, shot, args.quality))
        if args.command == "build" and not args.dry_run:
            source_regions = overrides.get(name)
            if source_regions is None and name in marked:
                source_regions = ocr.get(hashlib.sha256(images[name].read_bytes()).hexdigest(), [])
            records.append({"cue": shot, "start": start / FPS, "end": end / FPS, "image": images[name],
                            "labels": {"source": source_regions or [], "overlays": labels}})
    if not plans:
        raise ValueError(f"{lang}: empty timeline")
    if audio is not None:
        seconds = audio_duration(audio)
        if abs(seconds - last / FPS) > 1 / FPS:
            raise ValueError(f"{lang}: audio ({seconds:.3f}s) differs from timeline ({last/FPS:.3f}s)")
    if any(labels for _, _, _, labels in plans) and not Path(font).is_file():
        raise ValueError(f"{lang}: font not installed: {font}")
    width, height = output_dimensions(args.resolution, vertical)
    resolution = f"{width}x{height}"
    output_size = f", {resolution}" if args.command == "build" else ""
    print(f"{lang}: {len(plans)} shots, {last/FPS:.2f}s{output_size}")
    if args.dry_run:
        return
    language_dir = out / lang
    suffix = "-360p" if args.resolution == "360p" else ""
    output_video = language_dir / f"video{suffix}.mp4"
    unique = {name: labels for _, _, name, labels in plans}
    video_key = None
    if args.resume and audio is not None:
        video_key = checkpoint_key(args.cache_version, resolution, args.aspect, plans, close_plans, vertical_plans,
                                   {name: images[name] for name in unique}, audio,
                                   Path(font) if any(unique.values()) else None)
        if current(output_video, video_key):
            reuse_output(output_video)
            return output_video
    work = language_dir / "frames"
    work.mkdir(parents=True, exist_ok=True)
    concat = ["ffconcat version 1.0"]
    manifest = []
    shared = out / ".frame-cache"
    shared.mkdir(exist_ok=True)

    def prepare(name):
        labels = unique[name]
        path = localize(images[name], labels, font if labels else "", shared, args.cache_version)
        target = work / path.name
        if not target.exists() or (args.resume and checkpoint_key(target) != checkpoint_key(path)):
            target.write_bytes(path.read_bytes())
        return name, target

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        localized = dict(pool.map(prepare, unique))
    for row, (start, end, name, labels) in zip(entries, plans):
        path = localized[name]
        manifest.append({"cue": cue(row), "image": f"frames/{path.name}", "labels": labels})
        concat.extend((f"file 'frames/{path.name}'", f"option framerate {FPS}",
                       f"duration {(end-start)/FPS:.2f}"))
    (language_dir / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    if audio is None:
        return
    concat.extend((f"file 'frames/{path.name}'", f"option framerate {FPS}"))
    (language_dir / "shots.ffconcat").write_text("\n".join(concat) + "\n", encoding="utf-8")
    def motion_value(index, base):
        return str(base) + "".join(
            f"{crop[index]-base:+.9f}*between(on,{first},{end-1})"
            for first, end, crop in close_plans if crop[index] != base
        )

    zoom = motion_value(0, 1)
    x = motion_value(1, .5)
    y = motion_value(2, .5)
    center = "0.5" + "".join(vertical_plans)
    portrait = f"crop=w=ih*9/16:h=ih:x='clip(iw*({center})-ow/2,0,iw-ow)':y=0," if vertical else ""
    motion = (f"fps={FPS},{portrait}zoompan=z='{zoom}':x='(iw-iw/zoom)*({x})':y='(ih-ih/zoom)*({y})':"
              f"d=1:s={resolution}:fps={FPS},format=yuv420p")
    motion_file = f"motion{suffix}.ffscript"
    pending = f"video{suffix}.pending.mp4"
    (language_dir / motion_file).write_text(motion, encoding="utf-8")
    call("ffmpeg", "-nostdin", "-v", "error", "-y", "-safe", "0", "-i", "shots.ffconcat",
         "-i", str(audio), "-map", "0:v", "-map", "1:a", "-t", f"{last/FPS:.2f}",
         "-filter_script:v", motion_file, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
         pending, cwd=language_dir)
    video_frames = int(call("ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_frames", "-of", "csv=p=0",
                            pending, cwd=language_dir).strip())
    if video_frames != last:
        raise ValueError(f"{lang}: export has {video_frames} video frames, expected {last}")
    (language_dir / pending).replace(output_video)
    if video_key:
        commit(output_video, video_key)
    return output_video


def narration(root, lang, settings, args, stack):
    utterances = root / settings.get("utterances", f"{lang}/utterances.json")
    if "utterances" in settings or utterances.is_file():
        if args.resume and not args.dry_run:
            rows = read(utterances)
            entries = time_utterances(utterances, rows=rows, verify_pcm=True, profile=args.quality)
            paths = [utterances.parent / row["audio"] for row in rows]
            audio = stage_audio(args, stack, "narration", [entries, paths],
                                lambda target: time_utterances(utterances, target, rows=rows, profile=args.quality))
            return entries, audio
        audio = None
        if not args.dry_run:
            audio = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "narration.wav"
        return time_utterances(utterances, audio, profile=args.quality), audio
    return (read(root / settings.get("timeline", f"{lang}/timeline.json")),
            root / settings.get("audio", f"{lang}/narration.wav"))


def history_records(root, lang, settings, entries, records):
    scripts = {}
    script = root / lang / "narration.jsonl"
    if script.is_file():
        try:
            with script.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        row = json.loads(line)
                        scripts[row.get("cue") or row.get("id")] = row.get("text")
        except (OSError, ValueError, AttributeError):
            scripts.clear()
    manifest = root / settings.get("utterances", f"{lang}/utterances.json")
    clips = {cue(row): manifest.parent / row["audio"] for row in read(manifest)} if manifest.is_file() else {}
    for entry, record in zip(entries, records, strict=True):
        identifier = cue(entry)
        record["text"] = scripts.get(identifier) or scripts.get(
            entry.get("id") or identifier.rsplit(":", 1)[0]) or entry.get("text")
        record["audio"] = clips.get(identifier)
    return records


def blocks(entries):
    result = []
    previous_end = 0
    seen = set()
    for identifier, group in groupby(entries, key=lambda row: row.get("id") or cue(row).rsplit(":", 1)[0]):
        if identifier in seen:
            raise ValueError(f"Noncontiguous narration block: {identifier}")
        seen.add(identifier)
        first = previous_end
        for row in group:
            start, end = (round(row[field] * FPS) for field in ("start", "end"))
            if start != previous_end or end <= start:
                raise ValueError(f"Invalid narration timing in block {identifier}")
            previous_end = end
        result.append((identifier, first, previous_end))
    if not result:
        raise ValueError("Empty narration")
    return result


def audio_duration(audio):
    return float(call("ffprobe", "-v", "error", "-select_streams", "a:0",
                      "-show_entries", "stream=duration", "-of", "csv=p=0", str(audio)).strip())


def youtube_output(out, resolution, master, source):
    directory = out / "youtube" / resolution
    return directory if master == source else directory / "masters" / master


def sync_blocks(master, source, window=None):
    if window is not None:
        first, last = window
        master = [block for block in master if block[2] > first and block[1] < last]
        identifiers = {block[0] for block in master}
        source = [block for block in source if block[0] in identifiers]
    return master, source


def block_tempo(source, target):
    return (source[2] - source[1]) / (target[2] - target[1])


def tempo_status(tempo):
    if not .5 <= tempo <= 2:
        return "out_of_range"
    return "noticeable" if abs(tempo - 1) > .2 else "ok"


def dub_block_filter(start, end, length, tempo):
    speed = f"atempo={tempo:.10f}," if tempo != 1 else ""
    return (f"atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS,"
            f"{speed}apad=whole_len={length},atrim=end_sample={length}")


def youtube_dub(lang, master, entries, audio, destination, dry_run, window=None, mix=None, plan=None,
                cache_version=None):
    source = blocks(entries)
    source_end = source[-1][2]
    master, source = sync_blocks(master, source, window)
    if window is not None:
        first, last = window
        trim_start = first - master[0][1]
        duration_frames = last - first
    else:
        trim_start = 0
        duration_frames = master[-1][2]
    if [row[0] for row in source] != [row[0] for row in master]:
        raise ValueError("Narration block IDs and order must match the master")
    if audio is not None and abs(audio_duration(audio) - source_end / FPS) > 1 / FPS:
        raise ValueError("Narration audio duration differs from its timeline")
    rate = 48000
    samples_per_frame = rate // FPS
    filters = []
    large_changes = 0
    for index, (original, target) in enumerate(zip(source, master)):
        identifier, start, end = original
        _, target_start, target_end = target
        length = (target_end - target_start) * samples_per_frame
        tempo = block_tempo(original, target)
        status = tempo_status(tempo)
        if status == "out_of_range":
            raise ValueError(f"Block {identifier} requires {tempo:.2f}x speech tempo. Revise or rerecord it.")
        large_changes += status == "noticeable"
        block_filter = dub_block_filter(start*samples_per_frame, end*samples_per_frame, length, tempo)
        filters.append(f"[s{index}]{block_filter}[d{index}]")
    duration = duration_frames / FPS
    print(f"{lang}: {len(source)} blocks, {duration:.2f}s, {large_changes} blocks with tempo changes over 20%")
    if dry_run:
        return
    target = destination / f"{lang}.wav"
    key = checkpoint_key(cache_version, audio, source, master, window, mix, plan) if cache_version else None
    if key and current(target, key):
        reuse_output(target)
        return target
    inputs = "".join(f"[s{i}]" for i in range(len(source)))
    outputs = "".join(f"[d{i}]" for i in range(len(source)))
    trim = (f",atrim=start_sample={trim_start*samples_per_frame}:"
            f"end_sample={(trim_start+duration_frames)*samples_per_frame},asetpts=PTS-STARTPTS") if window else ""
    graph = [f"[0:a]aresample={rate},asplit={len(source)}{inputs}", *filters,
             f"{outputs}concat=n={len(source)}:v=0:a=1{trim}[dub]"]
    with tempfile.TemporaryDirectory(dir=destination) as work:
        script = Path(work) / "dub.ffscript"
        script.write_text(";\n".join(graph), encoding="ascii")
        # Stage in the output directory so the WAV inherits its permissions on Windows.
        with tempfile.NamedTemporaryFile(dir=destination, suffix=".pending.wav", delete=False) as staging:
            pending = Path(staging.name)
        try:
            call("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio), "-filter_complex_script",
                 str(script), "-map", "[dub]", "-c:a", "pcm_s16le", str(pending))
            with wave.open(str(pending), "rb") as result:
                if result.getnframes() != duration_frames * samples_per_frame:
                    raise ValueError("Exported dub duration differs from the master")
            result = pending
            if mix is not None:
                result = Path(work) / "mixed.wav"
                render_mix(pending, result, duration_frames, mix, window[0] if window else 0, plan)
            result.replace(target)
            if key:
                commit(target, key)
        finally:
            pending.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "frames", "build"))
    parser.add_argument("series", type=Path)
    parser.add_argument("--language", action="append", help="Defaults to all configured languages")
    parser.add_argument("--resolution", choices=RESOLUTIONS, default="1080p",
                        help="Video resolution for build (default: %(default)s)")
    parser.add_argument("--aspect", choices=ASPECTS, default="16:9")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--argos", action="store_true", help="Use installed Argos models for missing translations")
    parser.add_argument("--youtube", action="store_true",
                        help="Build one master video and aligned WAV dubs")
    parser.add_argument("--master-language", help="Override the master language for this YouTube build")
    parser.add_argument("--chapter", help="Build only this storyboard chapter")
    parser.add_argument("--cue-start", help="First cue in the fragment, inclusive")
    parser.add_argument("--cue-end", help="Last cue in the fragment, inclusive")
    parser.add_argument("--mix", action="store_true", help="Normalize and mix narration on export")
    parser.add_argument("--music", help="Looping background music inside the project")
    parser.add_argument("--ambience", help="Looping background ambience inside the project")
    parser.add_argument("--sound-map", action="store_true", help="Use project sound-map.json in the mix")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=None,
                        help="Reuse verified checkpoints (on by default for build)")
    args = parser.parse_args()
    if args.resume is None:
        args.resume = args.command == "build"
    if args.resume and args.command != "build":
        parser.error("--resume requires build")
    args.cache_version = None
    if args.youtube and args.command != "build":
        parser.error("--youtube requires build")
    if args.master_language and not args.youtube:
        parser.error("--master-language requires --youtube")
    if args.aspect == "9:16" and args.command != "build":
        parser.error("--aspect 9:16 requires build")
    if (args.mix and args.command != "build") or ((args.music or args.ambience or args.sound_map) and not args.mix):
        parser.error("--mix requires build; background audio requires --mix")
    root = args.series.resolve()
    try:
        mix = mix_sources(root, args.music, args.ambience) if args.mix else None
        for path in mix or ():
            if path is not None:
                validate_bed(path)
    except ValueError as error:
        parser.error(str(error))
    config = read(root / "series.json")
    args.quality = quality.profile(config)
    storyboard = root / config.get("storyboard", "shots.json")
    storyboard_rows = read(storyboard)
    try:
        fragment = select_fragment(storyboard_rows, args.chapter, args.cue_start, args.cue_end)
        if fragment and args.command != "build":
            raise ValueError("Fragment selection requires build")
    except ValueError as error:
        parser.error(str(error))
    frames = {cue(row): row["image"] for row in storyboard_rows}
    if len(frames) != len(storyboard_rows):
        parser.exit(1, "Storyboard cue IDs must be unique.\n")
    sound_map = None
    if args.sound_map:
        try:
            sound_map = read_sound_map(root, frames)
            if sound_map is None:
                raise ValueError("No sound-map.json in the project")
            for path in {item["audio"] for item in sound_map[0].values()}:
                validate_bed(path)
        except ValueError as error:
            parser.error(str(error))
    images = {name: (storyboard.parent / name).resolve() for name in set(frames.values())}
    marked = {row["image"] for row in storyboard_rows if row.get("ocr") is True}
    marked.update(config.get("ocr_images", []))
    if marked - images.keys():
        parser.exit(1, f"Unknown OCR frames: {sorted(marked - images.keys())}\n")
    out = root / config.get("output", "editions")
    cache = out / "ocr.json"
    if args.command == "scan":
        scan({name: images[name] for name in sorted(marked) if name not in config.get("regions", {})},
             cache, args.dry_run)
        return
    glossary = {key(k): v for k, v in config.get("glossary", {}).items()}
    ocr = read(cache) if cache.is_file() else {}
    montage = read_montage(root / "montage.json")
    out = mix_output(aspect_output(fragment_output(out, fragment), args.aspect), args.mix, args.sound_map)
    failures = []
    languages = args.language or list(config["languages"])
    if args.youtube:
        source = config.get("source_language")
        master_lang = args.master_language or source
        if master_lang not in config["languages"] or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", master_lang):
            parser.error("The YouTube master must be a configured language")
        languages = [master_lang, *(lang for lang in languages if lang != master_lang)]
        out = youtube_output(out, args.resolution, master_lang, source)
    if args.resume and not args.dry_run:
        args.cache_directory = out / ".resume-cache"
        args.cache_version = checkpoint_key(Path(__file__), Path(__file__).with_name("audio_mix.py"),
                                            call("ffmpeg", "-version").splitlines()[0])
    master = None
    master_window = None
    master_plan = None
    history = {}
    artifacts = {}
    capture = args.command == "build" and not args.dry_run
    for lang in languages:
        try:
            if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", lang):
                raise ValueError(f"Invalid language code: {lang}")
            settings = config["languages"][lang]
            with ExitStack() as stack:
                if args.command == "frames":
                    entries, audio = storyboard_rows, None
                else:
                    entries, audio = narration(root, lang, settings, args, stack)
                if args.youtube and lang != master_lang:
                    target = youtube_dub(lang, master, entries, audio, out, args.dry_run, master_window, mix,
                                         master_plan, args.cache_version)
                    if capture:
                        _, selected_blocks = sync_blocks(master, blocks(entries), master_window)
                        selected = {block[0] for block in selected_blocks}
                        source_entries = [row for row in entries
                                          if (row.get("id") or cue(row).rsplit(":", 1)[0]) in selected]
                        records = [{"cue": cue(row), "start": row["start"], "end": row["end"]}
                                   for row in source_entries]
                        history[lang] = history_records(root, lang, settings, source_entries, records)
                else:
                    master_blocks = blocks(entries) if args.youtube else None
                    full_entries = entries
                    offset = 0
                    window = None
                    if fragment:
                        sliced, window = slice_timeline(entries, fragment)
                        audio = slice_audio(audio, entries, window, args, stack)
                        entries = sliced
                        offset = window[0]
                        if args.youtube:
                            master_window = window
                    plan = None
                    if sound_map is not None:
                        _, plan = sound_map_plan(full_entries, sound_map, window)
                        if args.youtube:
                            master_plan = plan
                    if mix is not None and not args.dry_run:
                        duration = round(entries[-1]["end"] * FPS)
                        if abs(audio_duration(audio) - duration / FPS) > 1 / FPS:
                            raise ValueError("Narration audio duration differs from its timeline")
                        audio = stage_audio(args, stack, "mixed", [audio, duration, mix, offset, plan],
                                            lambda target: render_mix(audio, target, duration, mix, offset, plan))
                    records = []
                    target = build(lang, entries, audio, frames, images, marked, config, ocr, glossary, out, args,
                                   montage, records)
                    if capture:
                        history[lang] = history_records(root, lang, settings, entries, records)
                    master = master_blocks
                if capture:
                    artifacts[target.relative_to(root).as_posix()] = target
        except (OSError, ValueError, KeyError, ImportError, subprocess.CalledProcessError) as error:
            if os.environ.get("SERIES_DIAGNOSTICS") == "1":
                file = getattr(error, "file", None) or getattr(error, "filename", None)
                print(DIAGNOSTIC_PREFIX + json.dumps({
                    "code": getattr(error, "code", "render_failed"), "language": lang,
                    "cue": getattr(error, "cue", None), "file": str(file) if file else None,
                    "retry_step": getattr(error, "retry_step", "check_then_build"),
                    "message": str(error), "severity": "error",
                }, ensure_ascii=False))
            failures.append(f"{lang}: {error}")
            if args.youtube and lang == master_lang:
                break
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")
    if capture:
        snapshot = build_history.capture(root / ".build-history",
                                         {"resolution": args.resolution, "aspect": args.aspect,
                                          "master_language": master_lang if args.youtube else None,
                                          "fragment": fragment, "mix": args.mix, "sound_map": args.sound_map,
                                          "quality": args.quality}, history, artifacts)
        print(f"Build snapshot: {snapshot['id']}")


if __name__ == "__main__":
    main()
