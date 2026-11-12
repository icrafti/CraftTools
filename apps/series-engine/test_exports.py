import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import server
import stable_audio
import tts_bridge


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def silent_wav():
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(96000))
    return result.getvalue()


class ExportTest(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.workspace = Path(work).resolve()
        self.enterContext(patch.object(server, "WORKSPACE", self.workspace))
        self.root = self.workspace / "show"
        self.root.mkdir()
        write_json(self.root / "series.json", {"source_language": "en", "languages": {"en": {}, "ru": {}}})

    def test_stable_audio_job_uses_the_configured_workspace(self):
        write_json(self.root / "shots.json", [])
        arguments, _ = server.job_command({
            "path": "show", "languages": ["en"], "mix": True, "stable_audio": True, "dry_run": True,
        })
        index = arguments.index("--stable-audio-home")
        self.assertEqual(Path(arguments[index + 1]), self.workspace / "output" / ".stable-audio")
        self.assertIn("--dry-run", arguments)

    def test_stable_audio_dry_run_merges_adjacent_cues_without_writing(self):
        directory = self.root / "generated"
        rows = [{"cue": "A", "start": 0, "end": 2}, {"cue": "B", "start": 2, "end": 4}]
        cue = lambda row: row["cue"]
        prompts = {("A", "small-sfx"): "Rain on leaves", ("B", "small-sfx"): "Rain on leaves"}
        with patch.object(stable_audio, "synthesize") as synthesize:
            plan = stable_audio.export_tracks(self.workspace, directory, prompts, rows, cue, 25, (25, 75), True)
            synthesize.assert_not_called()
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][1:], (0, 50, 25))
        self.assertFalse(directory.exists())

    def test_receipt_after_restart_and_replacement(self):
        jobs = server.Jobs()
        artifact = self.root / "video.mp4"
        artifact.write_bytes(b"first")
        name = server.relative(artifact)
        job = {"id": "1" * 32, "path": "show", "command": "build", "dry_run": False,
               "status": "succeeded", "exit_code": 0, "outputs": [{"path": name}]}
        jobs.registry.save(job)
        restarted = server.Jobs()
        self.assertEqual(restarted.get(job["id"])["status"], "succeeded")
        self.assertEqual(restarted.artifact(name, job["id"]), artifact)
        artifact.write_bytes(b"replacement")
        with self.assertRaises(server.ApiError):
            restarted.artifact(name, job["id"])

    def test_natural_dub_reports_only_overruns(self):
        master = [("A", 0, 50), ("B", 50, 100)]
        source = [("A", 0, 40), ("B", 40, 95)]
        report = server.dub_report("ru", master, source, None)
        self.assertFalse(report["ready"])
        self.assertEqual([row["status"] for row in report["blocks"]], ["ok", "rewrite"])
        self.assertEqual(report["blocks"][1]["overrun"], 5 / server.series.FPS)
        self.assertEqual(server.series.dub_block_filter(10, 30, 40),
                         "atrim=start_sample=10:end_sample=30,asetpts=PTS-STARTPTS,"
                         "apad=whole_len=40,atrim=end_sample=40")
        with self.assertRaisesRegex(ValueError, "master window"):
            server.series.dub_block_filter(10, 51, 40)
        with self.assertRaisesRegex(ValueError, r"\bB \(\+0\.20s\)"):
            server.series.youtube_dub("ru", master, [
                {"id": "A", "start": 0, "end": 40 / server.series.FPS},
                {"id": "B", "start": 40 / server.series.FPS, "end": 95 / server.series.FPS},
            ], None, self.root, True)

    def test_sync_brief_estimates_words_from_recorded_duration(self):
        target = [{"id": "A", "cue": "A:1", "start": 0, "end": 2}]
        source = [{"id": "A", "cue": "A:1", "start": 0, "end": 3}]
        rows = [{"text": "Этот блок сейчас состоит из шести слов."}]
        with patch.object(server, "sync_timeline", return_value=target), patch.object(
            server, "editable_narration", return_value=(None, rows, source, None, "revision")
        ):
            result = server.sync_report(self.root, {"language": ["ru"]})
        self.assertEqual(result["duration"], 2)
        self.assertEqual(result["languages"][0]["adaptation"][0]["suggested_words"], 4)

    def test_subtitles_keep_original_cue_lengths_in_master_window(self):
        source = [
            {"id": "A", "cue": "A:1", "start": 0, "end": 0.8},
            {"id": "A", "cue": "A:2", "start": 0.8, "end": 1.6},
            {"id": "B", "cue": "B:1", "start": 1.6, "end": 2.6},
        ]
        master = [{"id": "A", "start": 0, "end": 2}, {"id": "B", "start": 2, "end": 4}]
        script = [{"text": text} for text in ("First.", "Second.", "Third.")]
        with patch.object(server, "editable_narration", return_value=(None, script, source, None, "")):
            result = server.subtitle_export(self.root, {"language": ["ru"], "master_language": ["en"]}, master)
        self.assertIn(b"00:00:00,800 --> 00:00:01,600", result[2])
        self.assertIn(b"00:00:02,000 --> 00:00:03,000", result[2])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    def test_mp3_source_sample_count_preserves_the_wav_clock(self):
        source = self.root / "source.wav"
        target = self.root / "source.mp3"
        manifest = self.root / "utterances.json"
        with wave.open(str(source), "wb") as pcm:
            pcm.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            pcm.writeframes(bytes(82951 * 2))
        server.series.call("ffmpeg", "-v", "error", "-i", str(source), "-c:a", "libmp3lame", str(target))
        cue = {"cue": "C01-001"}
        baseline = server.series.time_utterances(manifest, rows=[{**cue, "audio": source.name}])
        result = self.root / "temporary.wav"
        migrated = server.series.time_utterances(manifest, result,
            rows=[{**cue, "audio": target.name, "samples": 82951}])
        self.assertEqual(migrated, baseline)
        with wave.open(str(result), "rb") as pcm:
            self.assertEqual(pcm.getnframes(), round(baseline[-1]["end"] * 24000))
        with self.assertRaisesRegex(ValueError, "sample count"):
            server.series.time_utterances(manifest, rows=[{**cue, "audio": target.name, "samples": 96000}])

    def test_tts_bridge_contract(self):
        wav = silent_wav()
        received = []

        class Bridge(BaseHTTPRequestHandler):
            def answer(self, body):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                received.append(self.path)
                self.answer(b'{"ready":true}' if self.path == "/health" else b'["test-voice"]')

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.answer(wav)

        bridge = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Bridge))
        worker = threading.Thread(target=bridge.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(bridge.shutdown)
        for provider in ("qwen", "omnivoice"):
            profile = {"provider": provider, "endpoint": f"http://127.0.0.1:{bridge.server_port}",
                       "language": "ru", "model": "test-model", "timeout_seconds": 1,
                       "voice_overrides": {"narrator": "test-voice"}}
            self.assertEqual(tts_bridge.request(profile, "health"), {"ready": True})
            self.assertEqual(tts_bridge.request(profile, "voices"), ["test-voice"])
            self.assertEqual(tts_bridge.request(profile, "synthesize", {
                "id": "C01-001", "speaker": "narrator", "text": "Привет."
            }), wav)
            self.assertEqual(received[-2:], ["/voices?language=ru", {
                "id": "C01-001", "text": "Привет.", "language": "ru",
                "model": "test-model", "voice": "test-voice",
            }])
            with self.assertRaisesRegex(ValueError, "loopback"):
                tts_bridge.request({**profile, "endpoint": "http://192.0.2.1"}, "health")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    def test_build_package_and_switch_via_http(self):
        row = {"id": "C01-001", "chapter": "C01", "part": 1, "start": 0, "end": 1, "text": "Hello."}
        write_json(self.root / "shots.json", [{**row, "image": "frame.png"}])
        server.series.call("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=teal:s=320x180",
                           "-frames:v", "1", "-threads", "1", str(self.root / "frame.png"))
        for language in ("en", "ru"):
            folder = self.root / language
            folder.mkdir()
            write_json(folder / "timeline.json", [row])
            source = self.workspace / "source.wav"
            source.write_bytes(silent_wav())
            clip = folder / (server.series.cue(row).replace(":", "__") + ".mp3")
            server.series.call("ffmpeg", "-v", "error", "-i", str(source), "-c:a", "libmp3lame", str(clip))
        write_json(self.root / "publication.json", {"cover": "frame.png", "languages": {
            language: {"description": "Description"} for language in ("en", "ru")}})

        httpd = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), server.Handler))
        httpd.jobs = server.Jobs()
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"

        def api(path, data=None):
            body = json.dumps(data).encode() if data is not None else None
            with urlopen(Request(base + path, body, {"Content-Type": "application/json"}), timeout=10) as reply:
                return json.load(reply)

        def run(data):
            submitted = api("/api/jobs", data)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                job = api("/api/jobs/" + submitted["id"])
                if job["status"] in ("succeeded", "failed"):
                    self.assertEqual(job["status"], "succeeded", job["log"])
                    return job
                time.sleep(0.1)
            self.fail("Export did not finish")

        settings = {"path": "show", "command": "build", "youtube": True, "resolution": "360p"}
        def fake_tinyaudio(tracks):
            [(target, description)] = tracks.items()
            self.assertEqual(description, "Rain on glass")
            target.write_bytes(b"test-mp3")

        with patch.object(server.tinyaudio_bridge, "synthesize", side_effect=fake_tinyaudio):
            request = Request(base + "/api/audio/tinyaudio", json.dumps({"prompt": "Rain on glass"}).encode(),
                              {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers.get_content_type(), "audio/mpeg")
                self.assertEqual(response.read(), b"test-mp3")
        with patch.object(server, "call_tts_bridge", return_value=silent_wav()):
            request = Request(base + "/api/narration/tts/synthesize",
                              json.dumps({"path": "show", "provider": "qwen", "language": "en",
                                          "id": "C01-001", "speaker": "narrator", "text": "Hello."}).encode(),
                              {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers.get_content_type(), "audio/mpeg")
                synthesized = self.workspace / "synthesized.mp3"
                synthesized.write_bytes(response.read())
            self.assertGreater(server.series.audio_duration(synthesized), 0)
        for language in ("en", "ru"):
            imported = api("/api/narration/import", {"path": "show", "language": language, "folder": language})
            self.assertEqual(imported["duration"], 1)
            manifest = self.root / imported["manifest"]
            rows = json.loads(manifest.read_text(encoding="utf-8"))
            write_json(manifest, [{**rows[0], "text": row["text"]}])
        dry = run({**settings, "mix": True, "tinyaudio": "scenes", "dry_run": True})
        self.assertIn("TinyAudio: 0 scene effects", dry["log"])
        self.assertFalse(list(self.root.rglob(".tinyaudio")))
        original = run(settings)
        self.assertFalse(list(self.root.rglob("*.wav")))
        self.assertEqual(sorted(Path(item["path"]).suffix for item in original["outputs"]),
                         [".mp3", ".mp3", ".mp4", ".srt", ".srt"])
        for item in original["outputs"]:
            if item["path"].endswith(".mp3"):
                with urlopen(Request(base + item["url"], headers={"Range": "bytes=0-127"}), timeout=10) as reply:
                    self.assertEqual(reply.status, 206)
                    self.assertEqual(reply.headers.get_content_type(), "audio/mpeg")
                decoded = self.workspace / "decoded.wav"
                server.series.call("ffmpeg", "-v", "error", "-y", "-i", str(self.workspace / item["path"]), str(decoded))
                with wave.open(str(decoded), "rb") as audio:
                    self.assertEqual(audio.getnframes(), 48000)
        current = run(settings)
        video = next(item["path"] for item in original["outputs"] if item["path"].endswith(".mp4"))
        for item in original["outputs"]:
            self.assertEqual(server.Jobs().artifact(item["path"], original["id"]), self.workspace / item["path"])
        switch = {"command": "switch_master", "path": "show", "master_language": "ru",
                  "source_export": current["id"], "video": video, "tracks": str(Path(video).parent.parent)}
        switched = run(switch)
        self.assertEqual(len(switched["outputs"]), 5)

        def picture_hash(path):
            return server.series.call("ffmpeg", "-v", "error", "-i", str(self.workspace / path),
                                      "-map", "0:v:0", "-c", "copy", "-f", "hash", "-")

        self.assertEqual(picture_hash(video), picture_hash(switched["outputs"][0]["path"]))
        wrong = {**switch, "video": switched["outputs"][0]["path"]}
        with self.assertRaisesRegex(ValueError, "registered export"):
            server.master_switch_inputs(wrong, httpd.jobs.registry)
        run({**settings, "mux_dubs": True})
        package = api("/api/project/package", {**settings, "mux_dubs": True})
        self.assertTrue(package["ready"], package["issues"])
        self.assertTrue(any(item["path"].endswith("/ru/video.mp4") for item in package["outputs"]))
        self.assertEqual(sum(item["path"].endswith("/audio.mp3") for item in package["outputs"]), 2)
