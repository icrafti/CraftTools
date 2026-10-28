import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('build_media', Path(__file__).resolve().parents[1] / 'scripts/build_media_downloader.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        flags = []
        for flag in ('source', 'ffmpeg', 'ffprobe', 'python'):
            path = self.root / (flag + '.fixture')
            path.write_bytes(b'fixture')
            flags += ['--' + flag, str(path)]
        self.args = MODULE.parser().parse_args(flags + ['--output', str(self.root / 'dist/MediaDownloader.exe')])

    def fake_process(self, command, **kwargs):
        if '-c' in command:
            if 'CArchiveReader' in command[command.index('-c') + 1]:
                return mock.Mock(returncode=0, stdout='{"tk_present":true}')
            return mock.Mock(returncode=0, stdout='{"pyinstaller":"test"}')
        folder = Path(command[command.index('--distpath') + 1])
        folder.mkdir()
        (folder / 'MediaDownloader.exe').write_bytes(b'MZ-test')
        return mock.Mock(returncode=0)

    def test_dry_run_writes_nothing(self):
        self.args.dry_run = True
        self.assertTrue(MODULE.build(self.args)['dry_run'])
        self.assertFalse((self.root / 'dist').exists())

    def test_missing_engine(self):
        Path(self.args.ffmpeg).unlink()
        with self.assertRaisesRegex(ValueError, 'ffmpeg not found'):
            MODULE.build(self.args)

    def test_build_idempotence_and_cleanup(self):
        with mock.patch.object(MODULE, 'ROOT', self.root), mock.patch.object(MODULE.subprocess, 'run', side_effect=self.fake_process) as run:
            self.assertFalse(MODULE.build(self.args)['unchanged'])
            self.assertTrue(MODULE.build(self.args)['unchanged'])
        self.assertEqual(run.call_count, 4)
        self.assertEqual(list((self.root / 'work').iterdir()), [])

    def test_refuses_existing_output(self):
        target = Path(self.args.output)
        target.parent.mkdir()
        target.write_bytes(b'original')
        with mock.patch.object(MODULE.subprocess, 'run', side_effect=self.fake_process):
            with self.assertRaisesRegex(ValueError, '--force'):
                MODULE.build(self.args)
        self.assertEqual(target.read_bytes(), b'original')

    def test_failure_does_not_publish(self):
        def fail(command, **kwargs):
            return mock.Mock(returncode=0, stdout='{}') if '-c' in command else mock.Mock(returncode=1)
        with mock.patch.object(MODULE, 'ROOT', self.root), mock.patch.object(MODULE.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'PyInstaller failed'):
                MODULE.build(self.args)
        self.assertFalse(Path(self.args.output).exists())
        self.assertEqual(list((self.root / 'work').iterdir()), [])

    def test_missing_gui_does_not_publish(self):
        def missing_gui(command, **kwargs):
            if '-c' in command and 'CArchiveReader' in command[command.index('-c') + 1]:
                return mock.Mock(returncode=0, stdout='{"tk_present":false}')
            return self.fake_process(command, **kwargs)
        with mock.patch.object(MODULE, 'ROOT', self.root), mock.patch.object(MODULE.subprocess, 'run', side_effect=missing_gui):
            with self.assertRaisesRegex(RuntimeError, 'missing Tcl/Tk'):
                MODULE.build(self.args)
        self.assertFalse(Path(self.args.output).exists())


if __name__ == '__main__':
    unittest.main()
