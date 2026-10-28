# Third-party notices and licensing scope

The root custom attribution license covers original icrafti material identified
with `LicenseRef-Icrafti-Attribution-1.0`. It does **not** relicense dependencies,
claim ownership of media, or revoke permissions on historical releases.

| Component | Version used | Terms / source |
| --- | --- | --- |
| FFmpeg and ffprobe | 8.1-full_build-www.gyan.dev | GPL v3+; [build provider](https://www.gyan.dev/ffmpeg/builds/), [upstream commit](https://github.com/FFmpeg/FFmpeg/commit/9047fa1b08) |
| Python | 3.12.10 | PSF license and bundled component notices; [Python source](https://www.python.org/downloads/release/python-31210/) |
| Tcl/Tk | Python 3.12 distribution | Tcl/Tk permissive licenses; included license files |
| yt-dlp | 2026.10.26 | Unlicense and upstream dependency notices; [source](https://github.com/yt-dlp/yt-dlp/tree/2026.10.26) |
| libtorrent | 2.1.1 | BSD license; [source](https://github.com/arvidn/libtorrent) |
| PyInstaller | 6.22.3 | GPL with the bootloader exception; [license](https://pyinstaller.org/en/stable/license.html) |
| PyInstaller hooks | 2026.9 | Included upstream license; build-time hooks and runtime helpers |

Copies of available upstream notices are in [`licenses/`](licenses/), including
the Python/Tk license texts, FFmpeg build configuration, and package license
files. Some Python packages in that directory are build-time tools rather than
runtime dependencies. They are listed to preserve their notices, not to apply
the custom license to them.

## FFmpeg

The applications invoke FFmpeg as a separate executable through command-line
arguments and progress text; they do not link the application source to FFmpeg
libraries. The portable files are self-extracting packages containing separate
engine executables. The engines' own GPL rights remain intact, including the
right to extract and use them separately under their original terms.

The FFmpeg build is unmodified. Exact configuration and external-library names
are preserved in `licenses/FFmpeg-build-README.txt`; the engine license is
`licenses/FFmpeg-GPL-3.0.txt`. Corresponding FFmpeg source is identified by commit
`9047fa1b08`; source and build information are linked from the build provider.
Distributors must satisfy the applicable corresponding-source obligations for
the exact engines and their compiled dependencies. The application source ZIP
alone is not a complete FFmpeg/dependency source distribution.

## Historical application licenses

Media Downloader was imported from `icrafti/media-downloader`, commit `1b9be06`,
whose MIT notice is retained as `licenses/Media-Downloader-Historical-MIT.txt`.
That historical version remains available under MIT. Audio Converter 1.0 was
delivered under GPL v3+; that earlier grant is not revoked. The new English
collection versions and original collection documentation are supplied by the
copyright holder under the root custom attribution license.

Other contributors retain their copyrights. Do not apply the custom license to
contributions or dependencies unless their rightsholders have permitted it.
