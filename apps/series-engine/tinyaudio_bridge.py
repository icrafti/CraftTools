"""Local TinyAudio synthesis and storyboard sound selection."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


RUNTIME = Path(__file__).resolve().parents[1] / "output" / "tinyaudio-test"
SCENE_SOUNDS = (
    (r"\b(?:thunder\w*|гром|грома|громом)\b", "A thunderclap followed by a deep rolling decay."),
    (r"\b(?:footsteps|шаги|шагов|шагами)\b", "Distinct footsteps at walking pace in a quiet space."),
    (r"\b(?:rain|raining|дождь|дождя|дождём)\b", "Steady rain pattering gently nearby."),
    (r"\b(?:train passes|passing train|train approaching|поезд проехал|приближался поезд|стук колёс)\b",
     "A passing train with rhythmic steel wheel clatter."),
    (r"\b(?:door slams|slamming door|хлопнула дверь|дверь захлопнулась)\b", "A door slams with a brief room echo."),
    (r"\b(?:crackling fire|fire crackles|треск дров|дрова трещали)\b", "Firewood crackling in a quiet room."),
)


def prompt_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise ValueError("Введите описание звука длиной от 1 до 1000 символов.")
    return value.strip()


def scene_prompt(row):
    explicit = row.get("sound_prompt")
    if explicit is False:
        return None
    if explicit is not None:
        return prompt_text(explicit)
    description = " ".join(str(row.get(field) or "") for field in ("description", "text"))
    for pattern, prompt in SCENE_SOUNDS:
        if re.search(pattern, description, flags=re.IGNORECASE):
            return prompt
    return None


def synthesize(tracks):
    """Generate missing MP3s atomically in one offline model session."""
    pending = {target: prompt for target, prompt in tracks.items() if not target.is_file()}
    if not pending:
        return
    runtime = Path(os.environ.get("TINYAUDIO_HOME", RUNTIME)).resolve()
    scripts = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = runtime / ".venv" / scripts
    if not python.is_file() or not (runtime / "tinyaudio_onnx.py").is_file():
        raise ValueError("Не найден локальный TinyAudio. Укажите папку установки через TINYAUDIO_HOME.")
    parent = next(iter(pending)).parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        work = Path(directory)
        targets = [(work / f"{index}.mp3", target) for index, target in enumerate(pending)]
        requests = [{"target": str(staged), "prompt": pending[target]}
                    for staged, target in targets]
        result = subprocess.run(
            [str(python), str(Path(__file__).resolve()), str(runtime)],
            input=json.dumps(requests), capture_output=True, encoding="utf-8", errors="replace",
            env={**os.environ, "HF_HOME": str(runtime / "hf-cache"), "HF_HUB_OFFLINE": "1",
                 "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode:
            raise ValueError(f"Ошибка локального TinyAudio. {result.stderr.strip()[-1800:]}")
        for staged, target in targets:
            staged.replace(target)


def track_path(directory, prompt):
    digest = hashlib.sha256(json.dumps(["tinyaudio-mf-sfx-v1", prompt]).encode()).hexdigest()[:24]
    return directory / f"{digest}.mp3"


def export_tracks(directory, prompts, entries, cue, fps, window, dry_run):
    """Resolve generated tracks for the selected timeline without writing during preflight."""
    start, end = window or (0, round(entries[-1]["end"] * fps))
    tracks = {}
    effects = []
    for row in entries:
        prompt = prompts.get(cue(row))
        if not prompt:
            continue
        first = round(row["start"] * fps)
        last = min(round(row["end"] * fps), first + 10 * fps, end)
        if max(first, start) >= last:
            continue
        path = track_path(directory, prompt)
        tracks[path] = prompt
        effects.append(({"audio": path, "generated_effect": True},
                        max(first, start) - start, last - start, max(0, start - first)))
    if not dry_run:
        synthesize(tracks)
    print(f"TinyAudio: {len(effects)} scene effects", flush=True)
    return effects


def run_worker(runtime):
    sys.path.insert(0, str(runtime))
    from tinyaudio_onnx import TinyAudio

    requests = json.load(sys.stdin)
    model = TinyAudio(local_files_only=True, num_threads=4)
    for request in requests:
        target = Path(request["target"])
        with tempfile.TemporaryDirectory(dir=target.parent) as directory:
            clip = Path(directory) / "sound.wav"
            model.generate(request["prompt"], clip, seed=123)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(clip),
                            "-c:a", "libmp3lame", "-q:a", "2", str(target)], check=True)


if __name__ == "__main__":
    run_worker(Path(sys.argv[1]))
