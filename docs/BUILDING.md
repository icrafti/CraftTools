# Building the applications

Build on Windows x64. Ready-made executables do not require Python; building
from source does. Use Python 3.12 x64 with Tcl/Tk and a virtual environment.
The checked dependency versions are pinned in `requirements-build.txt`.

## Preparation

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install --no-compile -r requirements-build.txt
```

If pip is unavailable or blocked, an existing `uv` installation can populate the
same environment without changing global packages:

```powershell
uv --cache-dir work/uv-cache pip install --python .venv/Scripts/python.exe -r requirements-build.txt
```

Obtain a Windows x64 static [FFmpeg build](https://www.gyan.dev/ffmpeg/builds/).
The validated binaries are **FFmpeg 8.1-full_build-www.gyan.dev**. Put
`ffmpeg.exe` and `ffprobe.exe` in `bin/`, or use their actual absolute paths.
The files are not committed to source control. Preserve their licenses and
source-distribution information when distributing a binary package.

## Audio Converter

The build uses the standard .NET Framework C# compiler, normally located at
`C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe`.

```powershell
python -B .agent/scripts/build_audio_converter.py --source apps/audio-converter/AudioConverter.cs --ffmpeg bin/ffmpeg.exe --output dist/AudioConverter.exe
```

## Media Downloader

```powershell
python -B .agent/scripts/build_media_downloader.py --source apps/media-downloader/yt_downloader.py --python .venv/Scripts/python.exe --ffmpeg bin/ffmpeg.exe --ffprobe bin/ffprobe.exe --output dist/MediaDownloader.exe
```

PyInstaller bundles Python, Tkinter, yt-dlp, libtorrent, and the two FFmpeg
executables. Node.js/Deno is not bundled. Settings and crash logs are stored next
to `MediaDownloader.exe` at runtime.

## Repeatable builds

Both build tools support `--dry-run`, return JSON, and store diagnostic logs in
`.agent/logs/`. They stage compilation in a temporary directory and publish a
result only after success. Repeating a build with unchanged inputs returns
`unchanged: true`. Use `--force` to replace a previous result after changing an
input. `.build.json` files record input hashes and the executable hash; do not
commit them because their contents are machine-specific.

This is input-based repeatability, not a claim of byte-for-byte reproducibility
across machines or compiler versions.

## Checks

Unit tests for the two build tools:

```powershell
python -B -m unittest discover -s .agent/tests -p "test_build_*.py" -v
```

After building Audio Converter, with FFmpeg/ffprobe in PATH:

```powershell
python -B -m unittest discover -s .agent/tests -p test_audio_converter.py -v
```

Media Downloader's source checks, Tkinter GUI smoke check, and loopback-only
download/conversion test:

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s .agent/tests -p test_media_downloader.py -v
```

The media test serves its own synthetic WAV on `127.0.0.1`; it does not download
third-party content or inspect browser cookies. Audio Converter's packaged tests
launch its EXE with an empty PATH and verify the bundled engine is sufficient.
