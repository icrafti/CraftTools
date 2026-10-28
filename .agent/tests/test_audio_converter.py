"""Real packaged-executable integration tests; all fixtures are removed afterward."""
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / 'dist' / 'AudioConverter.exe'
FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


class PackagedTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='audio-check-', dir=str(ROOT / 'work'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Папка с пробелами'
        self.root.mkdir()
        self.source = self.root / 'Тестовая запись.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i * 2 * math.pi * 440 / 48000))) for i in range(96000)))
        self.environment = dict(os.environ, PATH='', TEMP=str(self.root), TMP=str(self.root))

    def run_exe(self, *args):
        completed = subprocess.run([str(EXE), *map(str, args)], capture_output=True,
                                   encoding='utf-8-sig', timeout=90, env=self.environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        return completed.returncode, json.loads(completed.stdout)

    def test_self_test(self):
        code, data = self.run_exe('--self-test')
        self.assertEqual(code, 0, data)

    def test_all_formats_without_path_and_no_overwrite(self):
        original = self.source.read_bytes()
        for extension, codec in [('mp3', 'mp3'), ('wav', 'pcm_s16le'), ('flac', 'flac'), ('m4a', 'aac'), ('ogg', 'vorbis')]:
            with self.subTest(format=extension):
                code, data = self.run_exe('--convert', self.source, self.root, extension, 192, 0)
                self.assertEqual(code, 0, data)
                result = Path(data['output'])
                self.assertNotEqual(result, self.source)
                info = json.loads(subprocess.check_output([FFPROBE, '-v', 'error', '-show_entries',
                    'stream=codec_name,sample_rate,channels:format=duration', '-of', 'json', str(result)]))
                self.assertEqual(info['streams'][0]['codec_name'], codec)
                self.assertEqual(info['streams'][0]['channels'], 1)
                self.assertEqual(info['streams'][0]['sample_rate'], '48000')
                self.assertAlmostEqual(float(info['format']['duration']), 2, delta=0.1)
                subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-i', str(result), '-f', 'null', '-'], check=True, capture_output=True)
        self.assertEqual(self.source.read_bytes(), original)
        first = self.root / 'Тестовая запись.mp3'
        preserved = first.read_bytes()
        code, data = self.run_exe('--convert', self.source, self.root, 'mp3', 192, 0)
        self.assertEqual(code, 0, data)
        self.assertEqual(Path(data['output']).name, 'Тестовая запись (1).mp3')
        self.assertEqual(first.read_bytes(), preserved)
        self.assertEqual(list(self.root.glob('.audioconverter-*')), [])
        self.assertEqual(list(self.root.glob('PortableAudioConverter-*')), [])

    def test_errors_and_cancel_cleanup(self):
        code, data = self.run_exe('--convert', self.root / 'missing.wav', self.root, 'mp3', 192, 0)
        self.assertEqual(code, 1, data)
        broken = self.root / 'broken.m4a'
        broken.write_bytes(b'not an audio file')
        code, data = self.run_exe('--convert', broken, self.root, 'mp3', 192, 0)
        self.assertEqual(code, 1, data)
        code, data = self.run_exe('--convert', self.source, self.root, 'badformat', 192, 0)
        self.assertEqual(code, 1, data)
        code, data = self.run_exe('--convert', self.source, self.root, 'mp3', 192, 10)
        self.assertEqual(code, 2, data)
        self.assertTrue(data['cancelled'])
        self.assertEqual(list(self.root.glob('.audioconverter-*')), [])
        self.assertEqual(list(self.root.glob('PortableAudioConverter-*')), [])
        self.assertFalse((self.root / 'Тестовая запись.mp3').exists())


if __name__ == '__main__':
    unittest.main()
