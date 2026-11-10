"""Client for the studio's local Qwen and OmniVoice adapter contract."""

import io
import json
import wave
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(profile, operation, speech=None):
    endpoint = profile["endpoint"]
    if profile["provider"] not in ("qwen", "omnivoice") or not endpoint:
        raise ValueError("Select a Qwen or OmniVoice bridge")
    address = urlsplit(endpoint)
    if address.hostname not in ("127.0.0.1", "::1", "localhost") or address.query or address.fragment:
        raise ValueError("A TTS bridge must use a loopback URL without a query or fragment")
    url = endpoint.rstrip("/") + "/" + operation
    body = None
    if operation == "voices":
        url += "?" + urlencode({"language": profile["language"]})
    elif operation == "synthesize":
        for field in ("id", "speaker", "text"):
            if not isinstance(speech.get(field), str) or not speech[field].strip():
                raise ValueError(f"TTS synthesis requires {field}")
        speaker = speech["speaker"]
        body = json.dumps({"id": speech["id"], "text": speech["text"],
                           "language": profile["language"], "model": profile["model"],
                           "voice": profile["voice_overrides"].get(speaker, speaker)}).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body else {}
    limit = 32 * 1024 * 1024 if body else 256 * 1024
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, data=body, headers=headers),
                         timeout=min(profile["timeout_seconds"], 300)) as response:
            result = response.read(limit + 1)
    except (URLError, OSError) as error:
        raise RuntimeError(f"TTS bridge request failed: {error}") from error
    if len(result) > limit:
        raise RuntimeError("TTS bridge response exceeded the size limit")
    if body:
        try:
            with wave.open(io.BytesIO(result), "rb") as audio:
                count = audio.getnframes()
                if not count or len(audio.readframes(count)) != count * audio.getnchannels() * audio.getsampwidth():
                    raise ValueError("Empty or incomplete WAV")
        except (ValueError, EOFError, wave.Error) as error:
            raise RuntimeError("TTS bridge must return a complete PCM WAV") from error
        return result
    try:
        return json.loads(result)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("TTS bridge must return JSON") from error
