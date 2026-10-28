"""Create deterministic source and portable ZIPs for CraftTools."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
IGNORED = {'.git', 'dist', 'work', 'build', '.venv', '__pycache__', 'node_modules'}


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def source_files(root):
    allowed_root = {'README.md', 'LICENSE', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md',
                    'CHANGELOG.md', '.gitignore', '.gitattributes', 'requirements-build.txt'}
    paths = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts) or relative.parts[:2] == ('.agent', 'logs'):
            continue
        if relative.parts[0] not in {'apps', 'docs', 'licenses', '.agent'} and str(relative) not in allowed_root:
            continue
        if path.name in {'settings.json', 'error.log'} or path.name.startswith(('.env', 'cookies')) or path.suffix.lower() in {'.exe', '.mp3', '.m4a', '.part', '.pyc', '.torrent'} or path.name.endswith('.build.json'):
            continue
        paths.append((relative.as_posix(), path))
    return sorted(paths)


def write_zip(path, entries, force):
    with tempfile.TemporaryDirectory(prefix='zip-stage-', dir=str(path.parent)) as temporary:
        staged = Path(temporary) / path.name
        with zipfile.ZipFile(staged, 'w', allowZip64=True) as archive:
            for name, value in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_STORED if name.endswith('.exe') else zipfile.ZIP_DEFLATED
                with archive.open(info, 'w') as destination:
                    if isinstance(value, Path):
                        with value.open('rb') as source:
                            shutil.copyfileobj(source, destination, 1024 * 1024)
                    else:
                        destination.write(value.encode('utf-8'))
        with zipfile.ZipFile(staged) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP integrity check failed')
        digest = sha256(staged)
        if path.exists() and sha256(path) == digest and not force:
            return {'name': path.name, 'sha256': digest, 'unchanged': True}
        if path.exists() and not force:
            raise ValueError('Archive already exists with different content; use --force: ' + str(path))
        # A Windows TemporaryDirectory is private to the creating account.
        # Create the publication file under its destination parent so it inherits
        # that parent's normal ACL instead of the sandbox account's private ACL.
        published = path.with_name(path.name + '.building')
        try:
            shutil.copyfile(staged, published)
            published.replace(path)
        finally:
            published.unlink(missing_ok=True)
        return {'name': path.name, 'sha256': digest, 'unchanged': False}


def package(root, output, version, dry_run=False, force=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not version or any(character not in '0123456789.-' for character in version):
        raise ValueError('Version must contain only digits, dots, and hyphens')
    for name in ['README.md', 'LICENSE', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md',
                 'apps/audio-converter/AudioConverter.cs', 'apps/media-downloader/yt_downloader.py',
                 'dist/AudioConverter.exe', 'dist/MediaDownloader.exe']:
        if not (root / name).is_file():
            raise ValueError('Required collection file missing: ' + name)
    files = source_files(root)
    report = {'ok': True, 'dry_run': dry_run, 'source_files': len(files), 'output_directory': str(output)}
    if dry_run:
        return report
    output.mkdir(parents=True, exist_ok=True)
    artifacts = [write_zip(output / ('CraftTools-source-' + version + '.zip'), files, force)]
    notices = [(path.relative_to(root).as_posix(), path) for path in (root / 'licenses').rglob('*') if path.is_file()]
    notices += [(name, root / name) for name in ['LICENSE', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md']]
    for name, description in [('AudioConverter', 'Convert local audio to MP3, WAV, FLAC, M4A, and OGG. Works offline.'),
                              ('MediaDownloader', 'Download media and torrents. Internet is required; Node.js/Deno may be needed for some sites.')]:
        readme = f'{name} {version}\n\nDouble-click {name}.exe on Windows 10/11 x64.\n{description}\n\nSoftware by icrafti\nhttps://github.com/icrafti/CraftTools\n\nRetain the author credit and read LICENSE and THIRD_PARTY_NOTICES.md.\nFull source and instructions: https://github.com/icrafti/CraftTools\n'
        entries = notices + [(name + '.exe', root / 'dist' / (name + '.exe')), ('README.txt', readme)]
        artifacts.append(write_zip(output / (name + '-' + version + '-windows-x64.zip'), entries, force))
    sums = []
    for name in ['AudioConverter.exe', 'MediaDownloader.exe']:
        sums.append(sha256(root / 'dist' / name) + '  ' + name)
    sums += [item['sha256'] + '  ' + item['name'] for item in artifacts]
    checksum = output / 'SHA256SUMS.txt'
    staged = checksum.with_suffix('.txt.building')
    try:
        staged.write_text('\n'.join(sums) + '\n', encoding='ascii')
        staged.replace(checksum)
    finally:
        staged.unlink(missing_ok=True)
    log = ROOT / '.agent/logs/package_collection.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as stream:
        stream.write(datetime.now(timezone.utc).isoformat() + ' ' + json.dumps(artifacts) + '\n')
    return dict(report, artifacts=artifacts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output-directory', required=True)
    parser.add_argument('--version', default='1.1.0')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.source, args.output_directory, args.version, args.dry_run, args.force)))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
