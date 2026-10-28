import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

SPEC = importlib.util.spec_from_file_location('package_collection', Path(__file__).resolve().parents[1] / 'scripts/package_collection.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.output = Path(self.temp.name) / 'release'
        for name in ['README.md', 'LICENSE', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md',
                     'apps/audio-converter/AudioConverter.cs', 'apps/media-downloader/yt_downloader.py',
                     'dist/AudioConverter.exe', 'dist/MediaDownloader.exe', 'licenses/test.txt']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')

    def test_dry_run(self):
        self.assertTrue(MODULE.package(self.root, self.output, '1.1.0', dry_run=True)['dry_run'])
        self.assertFalse(self.output.exists())

    def test_missing_binary(self):
        (self.root / 'dist/AudioConverter.exe').unlink()
        with self.assertRaisesRegex(ValueError, 'Required collection file'):
            MODULE.package(self.root, self.output, '1.1.0')

    def test_packages_are_idempotent_and_exclude_private_files(self):
        for name in ['apps/media-downloader/settings.json', 'apps/media-downloader/cookies.txt', '.env', 'work/private.mp3', '.agent/logs/test.log']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private')
        with mock.patch.object(MODULE, 'ROOT', Path(self.temp.name)):
            first = MODULE.package(self.root, self.output, '1.1.0')
            second = MODULE.package(self.root, self.output, '1.1.0')
        self.assertTrue(all(item['unchanged'] for item in second['artifacts']))
        self.assertFalse(any(item['unchanged'] for item in first['artifacts']))
        with zipfile.ZipFile(self.output / 'SoftwareCollection-source-1.1.0.zip') as archive:
            self.assertTrue(all('private' not in archive.read(name).decode(errors='replace') for name in archive.namelist()))
            self.assertNotIn('dist/AudioConverter.exe', archive.namelist())
        with zipfile.ZipFile(self.output / 'AudioConverter-1.1.0-windows-x64.zip') as archive:
            self.assertIn('AudioConverter.exe', archive.namelist())
            self.assertIn('LICENSE', archive.namelist())
        self.assertEqual(len((self.output / 'SHA256SUMS.txt').read_text().splitlines()), 5)

    def test_refuses_overwrite(self):
        with mock.patch.object(MODULE, 'ROOT', Path(self.temp.name)):
            MODULE.package(self.root, self.output, '1.1.0')
            (self.root / 'README.md').write_text('changed')
            with self.assertRaisesRegex(ValueError, '--force'):
                MODULE.package(self.root, self.output, '1.1.0')

    def test_publish_stages_under_destination_parent(self):
        output = self.output / 'fixture.zip'
        self.output.mkdir()
        observed = []
        original = Path.replace
        def replace(source, target):
            observed.append((source, Path(target)))
            return original(source, target)
        with mock.patch.object(Path, 'replace', replace):
            MODULE.write_zip(output, [('README.txt', 'test')], force=False)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0].parent, output.parent)
        self.assertEqual(list(self.output.glob('*.building')), [])


if __name__ == '__main__':
    unittest.main()
