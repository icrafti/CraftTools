"""Build a single portable WinForms executable with an embedded FFmpeg resource."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def log(message):
    directory = ROOT / '.agent' / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'build_audio_converter.log').open('a', encoding='utf-8') as stream:
        stream.write(datetime.now(timezone.utc).isoformat() + ' ' + message + '\n')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def plan(args):
    source, engine, compiler, output = [Path(value).resolve() for value in
                                         (args.source, args.ffmpeg, args.compiler, args.output)]
    for label, path in [('source', source), ('ffmpeg', engine), ('compiler', compiler)]:
        if not path.is_file():
            raise ValueError(f'{label} not found: {path}')
    if output.suffix.lower() != '.exe':
        raise ValueError('Output must have the .exe extension')
    if output in (source, engine, compiler):
        raise ValueError('Output cannot replace an input')
    return source, engine, compiler, output


def build(args):
    source, engine, compiler, output = plan(args)
    report = {'ok': True, 'dry_run': args.dry_run, 'output': str(output),
              'source': str(source), 'ffmpeg': str(engine), 'compiler': str(compiler)}
    if args.dry_run:
        return report
    stamp = {'source_sha256': digest(source), 'ffmpeg_sha256': digest(engine),
             'compiler_sha256': digest(compiler), 'build_script_sha256': digest(Path(__file__))}
    manifest = output.with_suffix('.build.json')
    previous = {}
    if manifest.is_file():
        try:
            previous = json.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    if output.exists():
        if all(previous.get(key) == value for key, value in stamp.items()) and previous.get('exe_sha256') == digest(output):
            return dict(report, unchanged=True, size=output.stat().st_size)
        if not args.force:
            raise ValueError('Output already exists with different inputs. Use --force to rebuild it.')
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / 'work'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='audio-build-', dir=str(scratch)) as temporary:
        temporary = Path(temporary)
        resource = temporary / 'ffmpeg.gz'
        with engine.open('rb') as incoming, resource.open('wb') as raw:
            with gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=6, mtime=0) as packed:
                shutil.copyfileobj(incoming, packed, length=1024 * 1024)
        compiled = temporary / 'AudioConverter.exe'
        command = [str(compiler), '/nologo', '/utf8output', '/target:winexe', '/platform:x64', '/optimize+',
                   '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll',
                   '/resource:' + str(resource) + ',ffmpeg.gz', '/out:' + str(compiled), str(source)]
        completed = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', timeout=180)
        log(f'Compiler exit={completed.returncode}\n{completed.stdout}\n{completed.stderr}')
        if completed.returncode != 0:
            raise RuntimeError('C# compilation failed: ' + completed.stdout.strip() + ' ' + completed.stderr.strip())
        if not compiled.is_file():
            raise RuntimeError('Compiler produced no executable')
        # Stage on the output volume, then publish atomically.
        staged = output.with_name(output.name + '.building')
        try:
            shutil.copyfile(compiled, staged)
            stamp['exe_sha256'] = digest(staged)
            os.replace(staged, output)
        finally:
            staged.unlink(missing_ok=True)
        staged_manifest = manifest.with_suffix('.json.building')
        try:
            staged_manifest.write_text(json.dumps(stamp, indent=2) + '\n', encoding='utf-8')
            os.replace(staged_manifest, manifest)
        finally:
            staged_manifest.unlink(missing_ok=True)
    log(f'Built {output} ({output.stat().st_size} bytes)')
    return dict(report, unchanged=False, size=output.stat().st_size, sha256=stamp['exe_sha256'])


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--source', required=True)
    result.add_argument('--ffmpeg', required=True)
    result.add_argument('--output', required=True)
    result.add_argument('--compiler', default='C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')
    result.add_argument('--dry-run', action='store_true', help='Validate inputs and report paths without writing files')
    result.add_argument('--force', action='store_true', help='Replace a previously generated executable')
    return result


def main():
    try:
        print(json.dumps(build(parser().parse_args()), ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
