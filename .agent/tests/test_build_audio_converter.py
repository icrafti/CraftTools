import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build_audio_converter.py'
SPEC = importlib.util.spec_from_file_location('build_audio_converter', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [self.root / name for name in ('source.cs', 'ffmpeg.exe', 'csc.exe')]
        for path in self.paths:
            path.write_bytes(b'test-input')
        self.args = MODULE.parser().parse_args(['--source', str(self.paths[0]), '--ffmpeg', str(self.paths[1]),
                                               '--compiler', str(self.paths[2]), '--output', str(self.root / 'out' / 'app.exe')])

    def test_dry_run_creates_nothing(self):
        self.args.dry_run = True
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertTrue(MODULE.build(self.args)['dry_run'])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_missing_input(self):
        self.paths[1].unlink()
        with self.assertRaisesRegex(ValueError, 'ffmpeg not found'):
            MODULE.plan(self.args)

    def test_refuses_input_overwrite(self):
        self.args.output = str(self.paths[1])
        with self.assertRaisesRegex(ValueError, 'replace an input'):
            MODULE.plan(self.args)

    def test_refuses_unknown_output(self):
        output = Path(self.args.output)
        output.parent.mkdir()
        output.write_bytes(b'original')
        with mock.patch.object(MODULE, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                MODULE.build(self.args)
        self.assertEqual(output.read_bytes(), b'original')

    def test_build_is_idempotent_and_cleans_up(self):
        def compile_fake(command, **kwargs):
            target = next(value[5:] for value in command if value.startswith('/out:'))
            Path(target).write_bytes(b'MZ-test-executable')
            return mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(MODULE, 'ROOT', self.root), mock.patch.object(MODULE.subprocess, 'run', side_effect=compile_fake) as compiler:
            first = MODULE.build(self.args)
            second = MODULE.build(self.args)
        self.assertFalse(first['unchanged'])
        self.assertTrue(second['unchanged'])
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(list((self.root / 'work').iterdir()), [])
        manifest = json.loads(Path(self.args.output).with_suffix('.build.json').read_text())
        self.assertEqual(manifest['exe_sha256'], MODULE.digest(Path(self.args.output)))

    def test_compiler_failure_does_not_publish(self):
        with mock.patch.object(MODULE, 'ROOT', self.root), mock.patch.object(MODULE.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='syntax error', stderr='')):
            with self.assertRaisesRegex(RuntimeError, 'syntax error'):
                MODULE.build(self.args)
        self.assertFalse(Path(self.args.output).exists())
        self.assertEqual(list((self.root / 'work').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
