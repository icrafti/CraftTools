import io
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import dub_adaptation
import server


def pcm_wav(seconds, rate=24000):
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(round(seconds * rate) * 2))
    return data.getvalue()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class DubAdaptationTest(unittest.TestCase):
    def test_rejected_batch_preserves_manifest_and_reuses_unchanged_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            root = workspace / "show"
            root.mkdir()
            (root / "series.json").write_text(json.dumps({
                "source_language": "en", "languages": {"en": {}, "ru": {}}, "tts": {"provider": "qwen"},
            }), encoding="utf-8")
            for language in ("en", "ru"):
                folder = root / language
                folder.mkdir()
                rows = []
                for index, seconds in enumerate((2, 2, 2) if language == "en" else (3, 3, 2), start=1):
                    audio = f"{index}.wav"
                    (folder / audio).write_bytes(pcm_wav(seconds))
                    rows.append({"cue": f"B{index}:1", "text": "Original", "audio": audio,
                                 "speaker": "narrator"})
                (folder / "utterances.json").write_text(json.dumps(rows), encoding="utf-8")
            manifest = root / "ru" / "utterances.json"
            original = manifest.read_bytes()
            requests = []

            def synthesize(profile, operation, speech):
                self.assertEqual(profile["provider"], "qwen")
                self.assertEqual(operation, "synthesize")
                self.assertEqual(speech["speaker"], "narrator")
                requests.append(speech["text"])
                return pcm_wav(2.5 if speech["text"] == "Still too long" else 1.5, rate=16000)

            with patch.object(server, "WORKSPACE", workspace), patch.object(server, "call_tts_bridge", synthesize):
                jobs = server.Jobs()
                payload = {"path": "show", "language": "ru", "master_language": "en",
                           "revision": str(server.file_version(manifest)),
                           "changes": [{"cue": "B1:1", "text": "Short enough"},
                                       {"cue": "B2:1", "text": "Still too long"}]}
                rejected = dub_adaptation.apply(server, payload, jobs)
                self.assertFalse(rejected["applied"])
                self.assertEqual(rejected["overruns"][0]["block"], "B2")
                self.assertEqual(manifest.read_bytes(), original)
                self.assertFalse(list(manifest.parent.glob("adapted-clips-*")))
                payload["changes"][1]["text"] = "Better"
                applied = dub_adaptation.apply(server, payload, jobs)
                self.assertTrue(applied["applied"])
                self.assertEqual(applied["changed"], 2)
                self.assertNotEqual(applied["revision"], payload["revision"])
                updated = json.loads(manifest.read_bytes())
                self.assertEqual(updated[2], json.loads(original)[2])
                self.assertEqual([row["text"] for row in updated[:2]], ["Short enough", "Better"])
                self.assertTrue(all(row["adapted"] for row in updated[:2]))
                for row in updated[:2]:
                    self.assertTrue((manifest.parent / row["audio"]).is_file())
                self.assertEqual(requests, ["Short enough", "Still too long", "Better"])
                with self.assertRaises(server.ApiError):
                    dub_adaptation.apply(server, payload, jobs)
            dub_adaptation.short_speech.cache_clear()
