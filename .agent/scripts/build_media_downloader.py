"""Build the portable Media Downloader using an isolated Python environment."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TCL_BOOTSTRAP = '''import ctypes, os, sys
if os.name == "nt":
    _tcl = ctypes.CDLL(os.path.join(sys.base_prefix, "DLLs", "tcl86t.dll"))
    _tcl.Tcl_FindExecutable.argtypes = [ctypes.c_char_p]
    _tcl.Tcl_FindExecutable.restype = None
    _tcl.Tcl_FindExecutable(sys.executable.encode("utf-8"))
'''


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inputs(args):
    files = {key: Path(getattr(args, key)).resolve() for key in ('source', 'ffmpeg', 'ffprobe', 'python')}
    for key, path in files.items():
        if not path.is_file():
            raise ValueError(f'{key} not found: {path}')
    target = Path(args.output).resolve()
    if target.suffix.lower() != '.exe' or target.name != 'MediaDownloader.exe':
        raise ValueError('Output filename must be MediaDownloader.exe')
    if target in files.values():
        raise ValueError('Output cannot replace an input')
    return files, target


def build(args):
    files, target = inputs(args)
    report = {'ok': True, 'dry_run': args.dry_run, 'output': str(target)}
    if args.dry_run:
        return report
    fingerprint = {key: sha256(path) for key, path in files.items()}
    fingerprint['builder'] = sha256(Path(__file__))
    versions = subprocess.run([str(files['python']), '-B', '-c',
        TCL_BOOTSTRAP + '\nimport importlib.metadata as m,json,tkinter; tkinter.Tcl(); print(json.dumps({n:m.version(n) for n in ["pyinstaller","yt-dlp","libtorrent","pyinstaller-hooks-contrib"]},sort_keys=True))'],
        capture_output=True, text=True, timeout=30, check=True)
    fingerprint['versions'] = json.loads(versions.stdout)
    manifest = target.with_suffix('.build.json')
    previous = {}
    if manifest.is_file():
        try:
            previous = json.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    if target.exists():
        if previous.get('inputs') == fingerprint and previous.get('sha256') == sha256(target):
            return dict(report, unchanged=True, size=target.stat().st_size)
        if not args.force:
            raise ValueError('Output already exists with different inputs. Use --force to replace it.')
    work = ROOT / 'work'
    work.mkdir(exist_ok=True)
    log = ROOT / '.agent/logs/build_media_downloader.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='media-build-', dir=str(work)) as temporary:
        temporary = Path(temporary)
        bootstrap = temporary / 'bootstrap'
        bootstrap.mkdir()
        (bootstrap / 'sitecustomize.py').write_text(TCL_BOOTSTRAP, encoding='utf-8')
        command = [str(files['python']), '-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--noconsole',
            '--name', 'MediaDownloader', '--distpath', str(temporary / 'dist'), '--workpath', str(temporary / 'build'),
            '--specpath', str(temporary), '--collect-all', 'yt_dlp', '--hidden-import', 'libtorrent',
            '--add-binary', str(files['ffmpeg']) + ';.', '--add-binary', str(files['ffprobe']) + ';.', str(files['source'])]
        environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                           PYTHONPATH=str(bootstrap) + os.pathsep + os.environ.get('PYTHONPATH', ''))
        with log.open('a', encoding='utf-8') as stream:
            stream.write(datetime.now(timezone.utc).isoformat() + ' Building portable EXE\n')
            stream.flush()
            completed = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=environment, timeout=420)
        if completed.returncode != 0:
            raise RuntimeError('PyInstaller failed; see .agent/logs/build_media_downloader.log')
        compiled = temporary / 'dist/MediaDownloader.exe'
        if not compiled.is_file():
            raise RuntimeError('PyInstaller produced no executable')
        check = subprocess.run([str(files['python']), '-B', '-c',
            'from PyInstaller.archive.readers import CArchiveReader; import json,sys; a=CArchiveReader(sys.argv[1]); names=[n.replace(chr(92),"/") for n in a.toc]; print(json.dumps({"tk_present":all(any(n.endswith(suffix) for n in names) for suffix in ["_tkinter.pyd","tcl86t.dll","tk86t.dll","_tcl_data/init.tcl","_tk_data/tk.tcl"])}))', str(compiled)],
            capture_output=True, text=True, timeout=30, check=True)
        if not json.loads(check.stdout).get('tk_present'):
            raise RuntimeError('The compiled executable is missing Tcl/Tk; refusing to publish a broken GUI')
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + '.building')
        try:
            shutil.copyfile(compiled, staged)
            result = {'inputs': fingerprint, 'sha256': sha256(staged)}
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        staged_manifest = manifest.with_suffix('.json.building')
        try:
            staged_manifest.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            staged_manifest.replace(manifest)
        finally:
            staged_manifest.unlink(missing_ok=True)
    return dict(report, unchanged=False, size=target.stat().st_size, sha256=result['sha256'])


def parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--ffprobe', required=True)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    return parser


def main():
    try:
        print(json.dumps(build(parser().parse_args())))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
