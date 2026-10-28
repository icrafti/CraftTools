# Validation report

Collection version: **1.1.0**. Validation date: **2026-10-28**.

## Automated checks

| Check | Result |
| --- | --- |
| Audio Converter build tool: safe overwrite, compilation failure, dry-run, and idempotence | 6 tests passed |
| Media Downloader build tool: staging, idempotence, dependency validation, and rejection of missing GUI resources | 6 tests passed |
| Release packager: stable archives, dry-run, required files, private-file exclusion, and safe overwrite | 4 tests passed |
| Packaged Audio Converter: five formats, decoding, Unicode/spaced paths, empty PATH, cancellation, errors, and no overwrite | 3 tests passed |
| Media Downloader: English UI/credit, settings, format options, local HTTP download and MP3 conversion, and Tcl initialization without site startup hooks | 5 tests passed |

The 24 unittest cases include subchecks for all five Audio Converter output
formats. Media Downloader's download fixture is generated locally and served on
`127.0.0.1`; no public website, torrent swarm, or browser cookie is accessed.

## Additional checks

- Audio Converter's actual Convert button processed a synthetic WAV into MP3;
  progress reached 100%, and the action buttons returned to their ready state.
- A rendered English interface image uses illustrative file paths, not a user
  recording or private filesystem location.
- The Media Downloader build verifies that the packaged executable contains
  `_tkinter.pyd`, Tcl/Tk DLLs, and the Tcl/Tk library data.
- The packaged Media Downloader stayed running without a crash log when started
  from an isolated test folder with an empty PATH, outside the execution sandbox.
  The sandbox prevented native Tcl from seeing extracted resources, so that
  sandbox-only failure is not reported as a failure on an ordinary desktop.
- Internal documentation links and ZIP integrity are checked before packaging.

## Limits

Validation ran on the current Windows machine, not a separate clean computer.
Live website availability, browser-cookie authentication, and torrent seeding
are not claimed to have been tested. Package publication status is reported by
the publishing chat rather than inferred from this file.
