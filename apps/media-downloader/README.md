# Media Downloader

A portable desktop downloader for **Windows 10/11 x64**, by **icrafti**.
Two tabs handle website downloads and torrents in an English interface.

## Video and audio

Paste a URL, choose **Audio** or **Video (MP4)**, select a format or video quality,
and click **Download**. Pressing Enter in the URL field also starts a download.

- Website support is provided by yt-dlp: YouTube, Vimeo, TikTok, VK, Instagram,
  X, and many other extractors. Support depends on the current upstream service.
- Video quality choices: Best, 2160p, 1440p, 1080p, 720p, 480p, and 360p.
- Audio formats: MP3, M4A, Opus, WAV, and FLAC.
- Playlists are disabled: URLs with playlist parameters download one item.
- Use **Browse** and **Open folder** to manage the output location.

If a website needs an authenticated session, you can explicitly enable
**Browser cookies** and choose a browser. Close that browser before retrying if
its cookie database is locked. Credentials and cookies are not bundled with this
project. yt-dlp can retrieve its official challenge-solver components from
GitHub; an installed Node.js or Deno runtime may be required for YouTube.

## Torrents

Paste a magnet link or choose a `.torrent` file, select the folder, and click
**Download**. Progress includes speed and peer count. **Cancel** stops the
torrent and retains partial files. Torrent availability depends on seeders;
metadata lookup times out if no peers are found within four minutes.

## Portable use

Download the complete portable ZIP from [Releases](https://github.com/icrafti/CraftTools/releases)
and open `MediaDownloader.exe`. Python, yt-dlp, libtorrent, FFmpeg, and ffprobe
are bundled. Node.js/Deno is optional for general use and is not bundled.

`settings.json` and `error.log` are stored beside the executable. Run it from a
writable folder. Only torrent downloads have a cancellation button in this
version; the website-download tab keeps the existing upstream workflow.

## Run from source

```powershell
python -m pip install -r requirements.txt
python yt_downloader.py
```

Source runs need FFmpeg and ffprobe in PATH. See the [build guide](../../docs/BUILDING.md#media-downloader)
for a portable executable. Use **Launch Media Downloader.bat** to run the source
without a console when `pythonw` is available.

## License and origin

The collection's English version is **1.1**, based on
[icrafti/media-downloader](https://github.com/icrafti/media-downloader) at commit
`1b9be06`. The new original-code distribution uses the
[Icrafti Attribution License 1.0](../../LICENSE). The historical MIT grant for
the upstream version is preserved in [licenses](../../licenses/Media-Downloader-Historical-MIT.txt)
and is not revoked. Dependencies keep their own licenses.

**Software by icrafti — https://github.com/icrafti/CraftTools**
