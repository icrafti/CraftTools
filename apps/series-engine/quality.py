"""Project-wide timing and artwork language rules shared by the CLI and API."""

import math
import re


FPS = 25
MAX_SECONDS = 60
DEFAULTS = {
    "min_shot_seconds": 1,
    "max_shot_seconds": 9,
    "min_cut_seconds": 2,
    "max_cut_seconds": 3,
    "art_language": "en",
}
LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def validate(values):
    """Merge a quality object with defaults and validate its persisted values."""
    if not isinstance(values, dict):
        raise ValueError("quality must be an object")
    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError(f"Unknown quality fields: {', '.join(sorted(unknown))}")

    result = {**DEFAULTS, **values}
    for field in ("min_shot_seconds", "max_shot_seconds", "min_cut_seconds", "max_cut_seconds"):
        value = result[field]
        if type(value) not in (int, float) or not 1 / FPS <= value <= MAX_SECONDS:
            raise ValueError(f"{field} must be a number between {1 / FPS:g} and {MAX_SECONDS} seconds")
        frames = round(value * FPS)
        if not math.isclose(value * FPS, frames, rel_tol=0, abs_tol=1e-8):
            raise ValueError(f"{field} must align to a video frame ({1 / FPS:g}s at {FPS} FPS)")
        result[field] = frames / FPS

    for kind in ("shot", "cut"):
        if result[f"min_{kind}_seconds"] > result[f"max_{kind}_seconds"]:
            raise ValueError(f"min_{kind}_seconds must not exceed max_{kind}_seconds")

    language = result["art_language"]
    if not isinstance(language, str) or len(language) > 35 or not LANGUAGE_TAG.fullmatch(language):
        raise ValueError("art_language must be a language tag such as en, en-US or pt-BR")
    return result


def profile(config):
    """Read quality rules from a series config, including configs predating the profile."""
    return validate(config.get("quality", {}))


def shot_frames(rules, fps=FPS):
    return round(rules["min_shot_seconds"] * fps), round(rules["max_shot_seconds"] * fps)


def cut_frames(rules, fps=FPS):
    return round(rules["min_cut_seconds"] * fps), round(rules["max_cut_seconds"] * fps)
