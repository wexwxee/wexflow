# GitHub Release Template

Copy this text into the next GitHub release and replace the placeholders.

````markdown
# WexFlow vX.Y.Z

Release date: YYYY-MM-DD

## Download

Recommended download for Windows users:

- `WexFlow-Setup.exe` - installer for normal users.
- `WexFlow-X.Y.Z.zip` - portable/update archive, if included in this release.

GitHub also shows `Source code (zip)` and `Source code (tar.gz)` below the release assets. Those archives are generated automatically by GitHub from this repository tag. They contain source code, not the compiled Windows `.exe`.

## Security And Verification

SHA256:

```text
WexFlow-Setup.exe
SHA256: PASTE_HASH_HERE

WexFlow-X.Y.Z.zip
SHA256: PASTE_HASH_HERE
```

VirusTotal:

- `WexFlow-Setup.exe`: PASTE_VIRUSTOTAL_LINK_HERE
- main app `.exe` from the zip, if scanned separately: PASTE_VIRUSTOTAL_LINK_HERE

To verify a downloaded file on Windows:

```powershell
Get-FileHash .\WexFlow-Setup.exe -Algorithm SHA256
```

Compare the printed hash with the hash above.

## Why Antivirus May Warn About The .exe

This Windows build is packaged with PyInstaller. PyInstaller bundles the Python interpreter, app code, and dependencies so users can run WexFlow without installing Python.

Because the executable is new, currently unsigned, and includes local server/updater/browser-automation behavior, some antivirus engines may flag it heuristically. That does not automatically mean the file is malicious. The source code is available in this repository, and cautious users can run the project from source instead of using the compiled `.exe`.

If you do not trust the compiled build, download `Source code (zip)` from this release and follow the "Run From Source Code" section in the README.

Important: do not upload private documents, credentials, or logs to VirusTotal. Uploaded files may be shared with security vendors and partners.

## What's New

- PASTE_CHANGE_1
- PASTE_CHANGE_2
- PASTE_CHANGE_3

## Fixed

- PASTE_FIX_1
- PASTE_FIX_2

## Notes

- User data is stored separately in `%AppData%\WexFlow`, so updating should not delete saved profile data or documents.
- Close WexFlow before running the installer manually.
- If Windows SmartScreen appears, check that the file came from this GitHub release and compare the SHA256 hash before choosing "Run anyway".
````

## Release Checklist

1. Update the app version.
2. Build from a clean virtual environment or clean Windows runner.
3. Package the release assets.
4. Calculate SHA256 hashes for every uploaded asset.
5. Scan the exact uploaded `.exe` or installer on VirusTotal.
6. Paste the VirusTotal links and SHA256 hashes into the release text.
7. Upload only the intended assets.
8. Confirm that GitHub generated `Source code (zip)` and `Source code (tar.gz)` automatically.
9. Publish the release.
