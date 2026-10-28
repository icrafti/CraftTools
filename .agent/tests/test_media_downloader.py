import contextlib
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'apps/media-downloader/yt_downloader.py'
SPEC = importlib.util.spec_from_file_location('media_downloader', SOURCE)
MEDIA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEDIA)


class MediaTests(unittest.TestCase):
    def test_english_source_and_options(self):
        self.assertIsNone(re.search('[\u0400-\u04ff]', SOURCE.read_text(encoding='utf-8')))
        audio = MEDIA.build_opts('audio', 'output', lambda _: None, audio_format='flac')
        self.assertTrue(audio['noplaylist'])
        self.assertEqual(audio['postprocessors'][0]['preferredcodec'], 'flac')
        self.assertIn('height<=1080', MEDIA.build_opts('video', 'output', None, video_quality='1080p')['format'])
        self.assertNotIn('height<=', MEDIA.build_opts('video', 'output', None, video_quality='Best')['format'])
        self.assertEqual(MEDIA.fmt_size(1024), '1.0 KiB')

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(MEDIA, 'SETTINGS_FILE', str(Path(directory) / 'settings.json')):
                self.assertEqual(MEDIA.load_settings(), {})
                MEDIA.save_settings({'mode': 'audio', 'audio_format': 'mp3'})
                self.assertEqual(MEDIA.load_settings()['audio_format'], 'mp3')

    def test_gui_labels_and_credit(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            with mock.patch.object(MEDIA, 'load_settings', return_value={}), mock.patch.object(MEDIA.DownloaderApp, '_clipboard_text', return_value=''):
                app = MEDIA.DownloaderApp(root)
                root.update_idletasks()
                self.assertIn('Video / Audio', app.notebook.tab(0, 'text'))
                self.assertIn('Torrents', app.notebook.tab(1, 'text'))
                self.assertEqual(app.download_btn.cget('text'), 'Download')
                self.assertEqual(app.tcancel_btn.cget('text'), 'Cancel')
                texts = [child.cget('text') for child in root.winfo_children() if 'text' in child.keys()]
                self.assertTrue(any('Software by icrafti' in text for text in texts))
        finally:
            root.destroy()

    def test_tcl_bootstrap_without_site_startup_hooks(self):
        environment_site = str(Path(sys.executable).resolve().parent.parent / 'Lib/site-packages')
        script = ('import sys; sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); '
                  'import yt_downloader as m; m._initialize_tcl(); '
                  'r=m.tk.Tk(); r.withdraw(); r.destroy(); print("Tcl/Tk initialized")')
        completed = subprocess.run([sys.executable, '-S', '-c', script, environment_site, str(SOURCE.parent)], capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('Tcl/Tk initialized', completed.stdout)

    def test_local_http_download_and_conversion(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / 'sample.wav'
            output = directory / 'output'
            output.mkdir()
            with wave.open(str(source), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b'\0\0' * 8000)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(directory)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                opts = MEDIA.build_opts('audio', str(output), lambda _: None)
                app = object.__new__(MEDIA.DownloaderApp)
                app._download('http://127.0.0.1:%s/sample.wav' % server.server_port, opts)
                result = output / 'sample.mp3'
                self.assertTrue(result.is_file())
                subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(result), '-f', 'null', '-'], check=True, capture_output=True)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
