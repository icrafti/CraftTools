"""Batch replacement of revised dub utterances after natural-speed synthesis."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path


MICROSOFT_SCRIPT = Path(__file__).with_name("microsoft_speech.ps1")
MICROSOFT_WAV_CACHE = OrderedDict()
MICROSOFT_WAV_LOCK = threading.Lock()
WINDOWS_LOCALES = {"de": "de-DE", "en": "en-US", "es": "es-ES", "fr": "fr-FR",
                   "it": "it-IT", "ja": "ja-JP", "ko": "ko-KR", "pt": "pt-BR",
                   "ru": "ru-RU", "tr": "tr-TR"}


def read_changes(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("Передайте от 1 до 100 изменённых реплик.")
    changes = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("Каждой правке нужны идентификатор реплики и новый текст.")
        cue, text = row.get("cue"), row.get("text")
        if (not isinstance(cue, str) or not cue.strip() or cue in changes
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000):
            raise ValueError("Укажите уникальный идентификатор реплики и непустой текст до 2000 символов.")
        changes[cue] = text.strip()
    return changes


def read_speakers(path):
    speakers = {}
    if path.is_file():
        with path.open(encoding="utf-8-sig") as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Каждая строка narration.jsonl должна содержать объект реплики.")
                speaker = row.get("speaker")
                if isinstance(speaker, str) and speaker.strip():
                    speakers[row.get("cue") or row.get("id")] = speaker
    return speakers


@lru_cache(maxsize=24)
def short_speech(synthesize, key, profile, speech):
    # Reuse successful short clips if the same batch needs another wording pass.
    result = synthesize(json.loads(profile), "synthesize", json.loads(speech))
    if len(result) > 1024 * 1024:
        raise LargeSpeech(result)
    return result


class LargeSpeech(Exception):
    """Return a large synthesized response without retaining it in the retry cache."""


def windows_voice_request(profile, speaker, text, target):
    language = profile["language"]
    locale = WINDOWS_LOCALES.get(language, language)
    voice_data = profile.get("voices") or {}
    speakers = voice_data.get("speakers", {}) if isinstance(voice_data, dict) else {}
    details = speakers.get(speaker, {}) if isinstance(speakers, dict) else {}
    if not isinstance(details, dict):
        details = {}
    voice_id = details.get("voice_id") or speaker
    voice = profile["voice_overrides"].get(speaker) or profile["voice_overrides"].get(voice_id)
    gender = details.get("gender")
    if gender not in ("female", "male") and not voice:
        raise ValueError(f"Для голоса {speaker} задайте gender в voices.json или голос Microsoft в настройках проекта.")
    rate = (1.07 if voice_id == "timur" else .97) if locale == "ru-RU" else 1.0
    return {"language": locale, "gender": "Male" if gender == "male" else "Female",
            "text": text, "voice": voice, "rate": rate, "target": str(target)}


def synthesize_microsoft(profile, requests, directory):
    if os.name != "nt":
        raise ValueError("Локальные голоса Microsoft доступны только в Windows.")
    missing = {}
    for request in requests:
        key = tuple(request[field] for field in ("language", "gender", "voice", "rate", "text"))
        with MICROSOFT_WAV_LOCK:
            cached = MICROSOFT_WAV_CACHE.get(key)
            if cached is not None:
                MICROSOFT_WAV_CACHE.move_to_end(key)
        if cached is None:
            missing.setdefault(key, []).append(request)
        else:
            Path(request["target"]).write_bytes(cached)
    if not missing:
        return
    manifest = directory / "microsoft-requests.json"
    manifest.write_text(json.dumps([group[0] for group in missing.values()], ensure_ascii=False), encoding="utf-8")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(MICROSOFT_SCRIPT), "-Request", str(manifest)],
                                capture_output=True, timeout=profile["timeout_seconds"] * len(missing))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Не удалось запустить локальный синтез Microsoft: {error}") from error
    finally:
        manifest.unlink(missing_ok=True)
    if result.returncode:
        raise ValueError("Не удалось озвучить реплики установленными голосами Microsoft. "
                         + result.stderr.decode(errors="replace").strip()[-1600:])
    for key, group in missing.items():
        wav = Path(group[0]["target"]).read_bytes()
        for request in group[1:]:
            Path(request["target"]).write_bytes(wav)
        if len(wav) <= 512 * 1024:
            with MICROSOFT_WAV_LOCK:
                MICROSOFT_WAV_CACHE[key] = wav
                MICROSOFT_WAV_CACHE.move_to_end(key)
                while len(MICROSOFT_WAV_CACHE) > 64:
                    MICROSOFT_WAV_CACHE.popitem(last=False)


def apply(server, payload, jobs):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    root = server.project_root(payload.get("path"))
    language, master = payload.get("language"), payload.get("master_language")
    if not isinstance(language, str) or not isinstance(master, str) or language == master:
        raise ValueError("Выберите основной язык и язык дубляжа.")
    changes = read_changes(payload.get("changes"))
    config_path = root / "series.json"
    config_revision = server.file_version(config_path)
    config = server.project_config(root)
    if master not in config["languages"]:
        raise ValueError("Основной язык должен быть настроен в проекте.")

    manifest, rows, entries, legacy, revision = server.editable_narration(root, language)
    if payload.get("revision") != revision:
        raise server.ApiError(409, "Озвучка изменилась. Заново проверьте тайминг.")
    if legacy is not None:
        raise ValueError("Сначала импортируйте отдельные MP3 реплик для языка дубляжа.")
    indexes = {server.series.cue(row): index for index, row in enumerate(rows)}
    if len(indexes) != len(rows) or changes.keys() - indexes.keys():
        raise ValueError("Проверьте идентификаторы реплик в манифесте озвучки.")
    edits = {indexes[cue]: text for cue, text in changes.items()
             if rows[indexes[cue]].get("text") != text}
    result = {"applied": True, "changed": len(edits), "revision": revision, "overruns": []}
    if not edits:
        return result
    with jobs.lock:
        if jobs.busy:
            raise server.ApiError(409, "Дождитесь завершения экспорта и повторите переозвучку.")

    master_settings = config["languages"][master]
    master_manifest = server.inside(root / master_settings.get("utterances", f"{master}/utterances.json"), root)
    master_paths = [master_manifest]
    if not master_manifest.is_file():
        master_paths.extend(server.inside(root / master_settings.get(field, default), root)
                            for field, default in (("timeline", f"{master}/timeline.json"),
                                                   ("audio", f"{master}/narration.wav")))
    master_versions = {path: server.file_version(path) for path in master_paths}
    fps = server.series.FPS
    targets = {block: end - start for block, start, end in server.series.blocks(
        server.sync_timeline(root, master, master_settings))}
    groups = {}
    for index, entry in enumerate(entries):
        groups.setdefault(entry["id"], []).append(index)
    affected = list(dict.fromkeys(entries[index]["id"] for index in sorted(edits)))
    if any(block not in targets for block in affected):
        raise ValueError("Блоки изменённых реплик должны существовать в основной дорожке.")

    query = {"language": [language]}
    if "provider" in payload:
        query["provider"] = [payload["provider"]]
    profile = server.tts_settings(root, query)
    microsoft = profile["provider"] == "microsoft"
    profile_json = json.dumps({key: value for key, value in profile.items() if key != "voices"}, sort_keys=True)
    pronunciation = server.pronunciation_path(root, language)
    transform = server.series.pronunciation_transform(server.read_pronunciation(pronunciation))
    script_speakers = read_speakers(server.inside(root / language / "narration.jsonl", root)) if any(
        not rows[index].get("speaker") for index in edits
    ) else {}
    quality = server.quality.profile(config)
    _, max_frames = server.quality.shot_frames(quality, fps)
    source = server.inside(manifest.parent / rows[0]["audio"], root)
    if source.suffix.lower() == ".mp3":
        version = source.stat()
        (channels, width, rate), _ = server.series.mp3_timing(
            source, (version.st_size, version.st_mtime_ns, version.st_ino), max_frames / fps)
    else:
        with wave.open(str(source), "rb") as audio:
            channels, width, rate = audio.getnchannels(), audio.getsampwidth(), audio.getframerate()
    if width != 2:
        raise ValueError("Для пакетной переозвучки используйте манифест с PCM16 или MP3.")

    stage = Path(tempfile.mkdtemp(prefix="adapted-clips-", dir=manifest.parent))
    committed = False
    try:
        pending_edits = {}
        for index, text in sorted(edits.items()):
            row = rows[index]
            cue = server.series.cue(row)
            block = entries[index]["id"]
            speaker = row.get("speaker") or script_speakers.get(cue) or script_speakers.get(block)
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError(f"Для реплики {cue} укажите speaker в манифесте или сценарии озвучки.")
            pending_edits[index] = (speaker, transform(text))
        if microsoft:
            synthesize_microsoft(profile, [
                windows_voice_request(profile, speaker, speech_text, stage / f"{index:05}.wav")
                for index, (speaker, speech_text) in pending_edits.items()
            ], stage)
        for index, (speaker, speech_text) in pending_edits.items():
            row = rows[index]
            if microsoft:
                wav_path = stage / f"{index:05}.wav"
                wav = wav_path.read_bytes()
                wav_path.unlink()
            else:
                speech = json.dumps({"id": server.series.cue(row), "speaker": speaker, "text": speech_text},
                                    ensure_ascii=False)
                try:
                    wav = short_speech(server.call_tts_bridge, (str(manifest), revision), profile_json, speech)
                except LargeSpeech as response:
                    wav = response.args[0]
            target = stage / f"{index:05}.mp3"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "wav", "-i", "pipe:0",
                            "-ac", str(channels), "-ar", str(rate), "-c:a", "libmp3lame", "-q:a", "2", str(target)],
                           input=wav, check=True)
            rows[index] = {key: value for key, value in row.items() if key != "samples"}
            rows[index].update(text=edits[index], speaker=speaker, adapted=True,
                               audio=target.relative_to(manifest.parent).as_posix())

        for block in affected:
            edited = server.series.time_utterances(manifest, rows=[rows[i] for i in groups[block]],
                                                   verify_pcm=True, profile=quality)
            duration = round(edited[-1]["end"] * fps)
            if duration > targets[block]:
                result["overruns"].append({"block": block, "recorded_seconds": duration / fps,
                    "target_seconds": targets[block] / fps, "overrun_seconds": (duration - targets[block]) / fps})
        if result["overruns"]:
            return {**result, "applied": False, "changed": 0}
        pending = stage / "manifest.json"
        pending.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with jobs.lock:
            if jobs.busy:
                raise server.ApiError(409, "Дождитесь завершения экспорта и повторите переозвучку.")
            if (str(server.file_version(manifest)) != revision
                    or server.file_version(config_path) != config_revision
                    or any(server.file_version(path) != version for path, version in master_versions.items())):
                raise server.ApiError(409, "Проект изменился. Заново проверьте тайминг.")
            pending.replace(manifest)
            committed = True
            return {**result, "revision": str(server.file_version(manifest))}
    finally:
        if not committed:
            shutil.rmtree(stage)
