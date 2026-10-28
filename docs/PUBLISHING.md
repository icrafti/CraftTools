# Publishing

This guide describes the intended publication steps. Its presence does not mean
the target GitHub repository or release already exists.

1. Authenticate with GitHub CLI as the owner:

```powershell
gh auth login --hostname github.com --web
gh auth status
```

2. From the collection directory, commit the source tree if it is not already
   committed, then create the public repository and push the source:

```powershell
gh repo create icrafti/software-collection --public --source . --remote origin --push --description "Portable Windows tools by icrafti: Media Downloader and Audio Converter. Attribution required."
```

3. Prepare the portable and source ZIPs:

```powershell
python -B .agent/scripts/package_collection.py --source . --output-directory dist --version 1.1.0
```

4. Review the licensing notices and applicable corresponding-source obligations
   for bundled dependencies before distributing binary packages. Publish the
   artifacts using the prepared English release notes:

```powershell
gh release create v1.1.0 dist/AudioConverter.exe dist/MediaDownloader.exe dist/AudioConverter-1.1.0-windows-x64.zip dist/MediaDownloader-1.1.0-windows-x64.zip dist/SoftwareCollection-source-1.1.0.zip dist/SHA256SUMS.txt --repo icrafti/software-collection --title "Software Collection 1.1.0" --notes-file docs/RELEASE_NOTES.md
```

Do not rerun repository or release creation blindly. First check whether the
repository/tag exists. On updates, inspect the current branch and release assets,
then push/update only the intended changes. The original standalone Media
Downloader repository and historical releases are not removed by this process.
