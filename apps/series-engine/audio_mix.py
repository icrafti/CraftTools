"""Deterministic FFmpeg mix for a timed narration and optional background beds."""

import json
import math
import re
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path


SAMPLE_RATE = 48000
VOICE_LUFS = -16
MUSIC_DB = -22
AMBIENCE_DB = -29
BED_FORMATS = "wav,mp3,flac,ogg,mov"
PCM_BLOCK = 16384
FRAME_BYTES = 4
BED_FADE = SAMPLE_RATE // 50


def validate_bed(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", BED_FORMATS,
         "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode or not result.stdout.strip():
        raise ValueError(f"No supported audio stream in {path}")


def _ffmpeg(arguments):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", *arguments],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise ValueError(f"Audio mix failed: {result.stderr.strip()[-2000:]}")
    return result.stderr


def _voice_gain(voice):
    log = _ffmpeg([
        "-v", "info", "-i", str(voice), "-vn",
        "-af", "aformat=channel_layouts=stereo,loudnorm=I=-16:print_format=json",
        "-f", "null", "-",
    ])
    measurement = re.search(r'\{\s*"input_i"\s*:.*?\}', log, re.S)
    if not measurement:
        raise ValueError("FFmpeg did not report voice loudness")
    loudness = float(json.loads(measurement[0])["input_i"])
    return min(24, VOICE_LUFS - loudness) if math.isfinite(loudness) else 0


def _silence(target, count):
    block = bytes(PCM_BLOCK * FRAME_BYTES)
    while count > 0:
        frames = min(count, PCM_BLOCK)
        target.writeframesraw(block[:frames * FRAME_BYTES])
        count -= frames


def _write_loop(target, source, count, offset, fade_in, fade_out):
    length = source.getnframes()
    if not length:
        raise ValueError("Location sound is empty")
    offset %= length
    source.setpos(offset)
    short_loop = None
    if length < PCM_BLOCK:
        source.rewind()
        short_loop = source.readframes(length)
    position = 0
    while position < count:
        frames = min(count - position, PCM_BLOCK)
        if short_loop is not None:
            # Repeat small clips in blocks rather than writing one loop at a time.
            end = offset + frames
            pcm = (short_loop * ((end + length - 1) // length))[offset * FRAME_BYTES:end * FRAME_BYTES]
        else:
            pcm = source.readframes(frames)
            if offset + frames > length:
                source.rewind()
                pcm += source.readframes(offset + frames - length)
            elif offset + frames == length:
                source.rewind()
        if len(pcm) != frames * FRAME_BYTES:
            raise ValueError("Decoded location sound has incomplete PCM data")
        if position < fade_in or position + frames > count - fade_out:
            samples = array("h")
            samples.frombytes(pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            for index in range(max(0, min(frames, fade_in - position))):
                gain = (position + index) / fade_in
                samples[index * 2] = round(samples[index * 2] * gain)
                samples[index * 2 + 1] = round(samples[index * 2 + 1] * gain)
            for index in range(max(0, count - fade_out - position), frames):
                gain = (count - position - index - 1) / fade_out
                samples[index * 2] = round(samples[index * 2] * gain)
                samples[index * 2 + 1] = round(samples[index * 2 + 1] * gain)
            if sys.byteorder != "little":
                samples.byteswap()
            pcm = samples.tobytes()
        target.writeframesraw(pcm)
        position += frames
        offset = (offset + frames) % length


def render_location_bed(output, frames, fps, segments):
    """Write a location bed from ordered (start, end, path, source offset) video frames."""
    if type(frames) is not int or frames <= 0 or type(fps) is not int or fps <= 0 or SAMPLE_RATE % fps:
        raise ValueError("Invalid frame boundaries for the location bed")
    previous = 0
    segments = list(segments)
    for start, end, path, offset in segments:
        if (type(start) is not int or type(end) is not int or type(offset) is not int
                or not previous <= start < end <= frames or offset < 0):
            raise ValueError("Location segments must be ordered, nonoverlapping and inside the output")
        if output.resolve() == path.resolve():
            raise ValueError("Location bed output must differ from the source")
        previous = end

    factor = SAMPLE_RATE // fps
    with tempfile.TemporaryDirectory(dir=output.parent) as work:
        decoded = {}
        for _, _, path, _ in segments:
            if path not in decoded:
                target = Path(work) / f"source-{len(decoded)}.wav"
                _ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-format_whitelist", BED_FORMATS,
                         "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE),
                         "-ac", "2", str(target)])
                decoded[path] = target
        staged = Path(work) / "location.wav"
        with wave.open(str(staged), "wb") as target:
            target.setparams((2, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            previous = 0
            for start, end, path, offset in segments:
                _silence(target, (start - previous) * factor)
                count = (end - start) * factor
                fade = min(count // 2, BED_FADE)
                with wave.open(str(decoded[path]), "rb") as source:
                    _write_loop(target, source, count, offset * factor, fade if offset == 0 else 0, fade)
                previous = end
            _silence(target, (frames - previous) * factor)
        staged.replace(output)


def mix_audio(voice, output, frames, fps, music=None, ambience=None, offset_frames=0, *,
              location_bed=None, effects_bed=None):
    """Write stereo PCM at the specified video duration, preserving the bed offset."""
    if frames <= 0 or offset_frames < 0 or fps <= 0 or SAMPLE_RATE % fps:
        raise ValueError("Invalid frame boundaries for the audio mix")
    inputs = (voice, music, ambience, location_bed, effects_bed)
    if output.resolve() in {path.resolve() for path in inputs if path is not None}:
        raise ValueError("Audio mix output must differ from the input tracks")
    samples_per_frame = SAMPLE_RATE // fps
    length = frames * samples_per_frame
    offset = offset_frames * samples_per_frame
    gain = _voice_gain(voice)
    arguments = ["-v", "error", "-y", "-i", str(voice)]
    audio_format = f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo"
    trim = f"apad=whole_len={length},atrim=end_sample={length}"
    beds = [(path, level, loop) for path, level, loop in (
        (music, MUSIC_DB, True), (ambience, AMBIENCE_DB, True),
        (location_bed, AMBIENCE_DB, False),
        (effects_bed, -18, False),
    ) if path is not None]
    graph = [f"[0:a]{audio_format},{trim},volume={gain:.6f}dB[voice]"]
    for index, (path, level, loop) in enumerate(beds, 1):
        arguments.extend(["-protocol_whitelist", "file", "-format_whitelist", BED_FORMATS])
        if loop:
            arguments.extend(["-stream_loop", "-1"])
        arguments.extend(["-i", str(path)])
        begin = offset if loop else 0
        graph.append(f"[{index}:a]{audio_format},atrim=start_sample={begin}:end_sample={begin + length},"
                     f"asetpts=PTS-STARTPTS,volume={level}dB[bed{index}]")
    if beds:
        tracks = "".join(f"[bed{index}]" for index in range(1, len(beds) + 1))
        if len(beds) > 1:
            graph.append(f"{tracks}amix=inputs={len(beds)}:normalize=0[bed]")
            tracks = "[bed]"
        graph.extend([
            "[voice]asplit[lead][key]",
            f"{tracks}[key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=250[ducked]",
            "[lead][ducked]amix=inputs=2:duration=first:normalize=0[mix]",
        ])
    signal = "[mix]" if beds else "[voice]"
    graph.append(f"{signal}alimiter=limit=0.891:level=0:latency=1,{trim}[out]")
    _ffmpeg([*arguments, "-filter_complex", ";".join(graph), "-map", "[out]",
             "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", str(output)])
    with wave.open(str(output), "rb") as result:
        if result.getnframes() != length:
            raise ValueError("Mixed audio duration differs from the video")
