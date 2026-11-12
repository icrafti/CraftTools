"""Local Stable Audio 3 inference with MP3 output."""

import os
import csv
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


MODELS = ("small-sfx", "small-music")


def clip_paths(directory, model, prompt, duration):
    key = hashlib.sha256(json.dumps(["sa3-v0.1.8-q4-v1", model, prompt, duration]).encode()).hexdigest()[:24]
    description = re.split(r"[,.;]", prompt, maxsplit=1)[0].strip()
    slug = "-".join(re.findall(r"[a-z0-9]+", description.lower())[:8])[:64].strip("-")
    return directory / f"{model}-{slug or 'audio'}-{key}.mp3", directory / f"{model}-{key}.mp3"


def write_catalog(directory, clips):
    if not clips:
        return
    catalog = directory / "sound-library.csv"
    records = {}
    if catalog.is_file():
        with catalog.open(encoding="utf-8-sig", newline="") as source:
            records.update((row["file"], row["description"]) for row in csv.DictReader(source))
    records.update(clips)
    with tempfile.TemporaryDirectory(dir=directory) as work:
        staged = Path(work) / catalog.name
        with staged.open("w", encoding="utf-8-sig", newline="") as destination:
            writer = csv.writer(destination)
            writer.writerow(("file", "description"))
            writer.writerows(sorted(records.items()))
        staged.replace(catalog)


def synthesize(installation, payload, output):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    model = payload.get("model", "small-sfx")
    prompt = payload.get("prompt")
    duration = payload.get("duration", 5)
    if model not in MODELS:
        raise ValueError("model must be small-sfx or small-music")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000 or "\0" in prompt:
        raise ValueError("prompt must contain 1–1000 characters")
    if type(duration) not in (int, float) or not 1 <= duration <= 30:
        raise ValueError("duration must be between 1 and 30 seconds")

    executable = installation / "runtime" / ("sa3-generate.exe" if os.name == "nt" else "sa3-generate")
    weights = installation / "models"
    prefix = f"stable-audio-3-{model}"
    files = {
        "--tok": "t5gemma-b-b-ul2-v1.0-vocab.gguf",
        "--t5": "t5gemma-b-b-ul2-encoder-0.3B-v1.0-Q8_0.gguf",
        "--cond": f"{prefix}-conditioner-v1.0-F32.gguf",
        "--dit": f"{prefix}-dit-0.5B-v1.0-Q4_K_M.gguf",
        "--same": f"{prefix}-same-s-v1.0-Q4_K_M.gguf",
    }
    command = [str(executable)]
    for flag, name in files.items():
        path = weights / name
        if not path.is_file():
            raise ValueError(f"Stable Audio 3 weights missing: {name}")
        command.extend((flag, str(path)))
    if not executable.is_file():
        raise ValueError("Stable Audio 3 runtime is missing")

    wav = Path(output).with_suffix(".wav")
    try:
        seed = int.from_bytes(hashlib.sha256(prompt.strip().encode()).digest()[:4], "big") & 0x7fffffff
        command.extend(("--prompt", prompt.strip(), "--duration", str(duration), "--seed", str(seed),
                        "--out", str(wav)))
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=900)
        if result.returncode:
            raise ValueError(f"Stable Audio 3 exited with code {result.returncode}: {result.stderr[-1000:]}")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), "-t", str(duration),
                        "-c:a", "libmp3lame", "-q:a", "2", str(output)], check=True, timeout=60)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Stable Audio 3 generation timed out") from error
    finally:
        wav.unlink(missing_ok=True)


def scene_prompts(rows, cue):
    prompts = {}
    for row in rows:
        for model, field in zip(MODELS, ("sound_prompt", "music_prompt")):
            value = row.get(field)
            if value is None or value is False:
                continue
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000 or "\0" in value:
                raise ValueError(f"{cue(row)}: {field} must be false or an audio prompt")
            prompts[cue(row), model] = value.strip()
    return prompts


def export_tracks(installation, directory, prompts, entries, cue, fps, window, dry_run):
    first, last = window or (0, round(entries[-1]["end"] * fps))
    plan = []
    clips = {}
    for model in MODELS:
        runs = []
        for row in entries:
            prompt = prompts.get((cue(row), model))
            if not prompt:
                continue
            start, end = (round(row[key] * fps) for key in ("start", "end"))
            if start >= end:
                continue
            if runs and runs[-1][0] == prompt and runs[-1][2] == start:
                runs[-1] = (prompt, runs[-1][1], end)
            else:
                runs.append((prompt, start, end))
        for prompt, start, end in runs:
            if start >= last or end <= first:
                continue
            duration = 10 if model == "small-sfx" else 30
            target, legacy = clip_paths(directory, model, prompt, duration)
            if not dry_run and not target.is_file():
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=directory) as work:
                    staged = Path(work) / target.name
                    if legacy.is_file():
                        shutil.copyfile(legacy, staged)
                    else:
                        synthesize(installation, dict(model=model, prompt=prompt, duration=duration), staged)
                    staged.replace(target)
            if not dry_run:
                clips[target.name] = prompt
            plan.append(({"audio": target, "generated_effect": model == "small-sfx",
                          "generated_music": model == "small-music"},
                         max(start, first) - first, min(end, last) - first, max(0, first - start)))
    write_catalog(directory, clips)
    print(f"Stable Audio 3: {len(plan)} sound placements, "
          f"{len({item[0]['audio'] for item in plan})} unique clips", flush=True)
    return plan
