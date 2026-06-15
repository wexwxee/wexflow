# WexFlow

WexFlow is a Windows desktop helper for job search automation in Denmark.

It brings job discovery, filtering, distance sorting, profile data, documents, and assisted application flows into one local app. The current build includes:

- **Salling Jobs** - vacancy feed, filters, saved job states, distance/radius tools, routes, profile data, CV/cover letter handling, and assisted applications.
- **7-Eleven Apply Studio** - profile tools, store selection, address/location helpers, routes, and assisted Typeform applications.

The app runs on your own PC. Profile data, selected documents, local job state, browser data, and logs are stored locally, not in this repository.

> WexFlow is an independent helper app. It is not affiliated with Salling Group, 7-Eleven, or their hiring platforms. Always review forms before submitting applications and follow the rules of the websites you use.

## Download

1. Open the latest release:
   [github.com/wexwxee/wexflow/releases/latest](https://github.com/wexwxee/wexflow/releases/latest)
2. Download the Windows installer asset, usually named like:
   `WexFlow-Setup.exe`
3. Run the installer.
4. Start WexFlow from the desktop shortcut or from the installed app folder.

Some releases may also include a portable/update archive named like `WexFlow-<version>.zip`. Normal users should use the installer unless the release notes say otherwise.

## What The App Does

WexFlow is meant to reduce repeated manual work while searching for entry-level jobs:

- loads and stores a local job database;
- filters jobs by text, city, brand, region, category, job type, level, status, and radius;
- sorts jobs by distance when a home address is available;
- keeps track of viewed, applied, hidden, interview, offer, and rejected jobs;
- stores reusable profile fields and selected documents locally;
- opens routes from your home address to a job or store;
- helps prepare application forms so you can review them before submitting.

The app is a personal assistant, not a replacement for your own review. It should not submit anything you have not checked.

## Why Is My Antivirus Flagging The .exe?

Short answer: a warning on the `.exe` can be a false positive caused by how Python desktop apps are packaged.

WexFlow is built with **PyInstaller**. PyInstaller analyzes the Python application, collects the Python interpreter and required dependencies, and packages them into a Windows app bundle. This is convenient for users because they do not need to install Python, but antivirus products often treat new unsigned bundled executables with extra suspicion.

False positives are more likely when an app:

- is new and has little download reputation;
- is not code-signed yet;
- is made with a bundler such as PyInstaller;
- starts local background servers for its own interface;
- launches browser automation through Playwright;
- downloads a browser runtime on first use;
- includes an updater that replaces app files between versions.

Those behaviors can be legitimate, and they are normal for this app, but they can look similar to behavior that heuristic antivirus engines watch closely.

What you can do:

- Download builds only from this repository's GitHub Releases page.
- Check the SHA256 hash published in the release notes.
- Check the VirusTotal report linked in the release notes.
- If you do not trust the compiled `.exe`, run WexFlow from source code instead.

Important privacy note: do not upload private documents, credentials, logs, or personal builds containing secrets to VirusTotal. Files submitted to VirusTotal can be shared with security vendors and partners.

## Run From Source Code

Use this path if you prefer to inspect the code and avoid downloading the compiled `.exe`.

### Requirements

- Windows 10 or Windows 11.
- Python 3.11 recommended.
- Git.
- Microsoft Edge WebView2 Runtime for the desktop window.
- Internet access for job data, maps/routes, and optional Playwright browser installation.

### 1. Clone The Repository

```powershell
git clone https://github.com/wexwxee/wexflow.git
cd wexflow
```

If the repository is renamed later, use the new GitHub URL shown on the repository page.

### 2. Create A Clean Virtual Environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. Install Playwright Browser Files

This is needed for assisted application flows that use browser automation.

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 4. Start The Web App

For the Salling Jobs web interface:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Then open:

```text
http://127.0.0.1:8000
```

### 5. Start The Desktop Wrapper

For the full desktop shell:

```powershell
.\.venv\Scripts\python.exe desktop_app.py
```

Development note: in source mode, the 7-Eleven module is expected at `C:\seven11-apply`. Release builds include that module inside the app bundle. If you only want to inspect or run the Salling Jobs part, the `uvicorn app:app` command above is enough.

## Local Data

WexFlow stores user data locally on your Windows account. Common locations include:

- `%AppData%\WexFlow` - app user data, WebView2 storage, Playwright browser files, logs, and update data.
- project folder during source development - local database and development files.

Local files can include:

- `jobs.db` - local vacancy database;
- `profile.json` or settings files - saved profile values;
- `uploads` - selected CV and cover letter copies;
- log files - troubleshooting information.

Do not publish your local profile, credentials, documents, logs, database, or browser profile.

## Updating

WexFlow checks GitHub Releases for newer versions. A normal update replaces the app files while keeping user data separately in `%AppData%\WexFlow`.

Manual update path:

1. Download the newest installer from [Releases](https://github.com/wexwxee/wexflow/releases).
2. Close WexFlow.
3. Run the installer again.

If you use the source-code version, update with Git:

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Build Notes For Maintainers

The Windows release is built with PyInstaller as a one-folder app and then packaged for distribution. The current project uses `WexFlow_dist.spec` for the distributable app.

Recommended release hygiene:

1. Build from a clean virtual environment.
2. Keep dependencies pinned in `requirements.txt`.
3. Prefer PyInstaller one-folder output over one-file output for easier inspection and fewer extraction-time surprises.
4. Keep `upx=False` unless there is a strong reason to compress binaries.
5. Build on a clean Windows machine or GitHub Actions Windows runner.
6. Publish SHA256 hashes for every uploaded asset.
7. Scan the final `.exe` or installer on VirusTotal and link the report in the release notes.
8. Keep the automatically generated GitHub `Source code (zip)` and `Source code (tar.gz)` archives visible so cautious users can inspect or build the code themselves.
9. Consider code signing later. A real code-signing certificate is the strongest long-term way to build reputation with Windows and antivirus vendors.

Useful commands:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller WexFlow_dist.spec --clean --noconfirm
.\.venv\Scripts\python.exe package_dist.py
Get-FileHash .\dist\WexFlow\WexFlow.exe -Algorithm SHA256
Get-FileHash .\dist\WexFlow-1.0.30.zip -Algorithm SHA256
```

For the next release text, use [docs/release-template.md](docs/release-template.md).

## Reducing PyInstaller False Positives

This does not guarantee that every antivirus product will be quiet, but it reduces common triggers.

### 1. Use A Dedicated Build Environment

Create a fresh virtual environment only for building releases:

```powershell
py -3.11 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install --upgrade pip
.\.venv-build\Scripts\python.exe -m pip install -r requirements.txt
```

Do not build releases from a daily development environment full of experiments and old packages.

### 2. Build On Clean Windows

Build on a clean Windows VM or GitHub Actions Windows runner. This makes releases easier to reproduce and easier to explain because each build starts from a fresh machine.

GitHub Actions workflows live in `.github/workflows`. A future workflow can install Python, install dependencies, run PyInstaller, package the zip, calculate hashes, and upload release artifacts.

### 3. Avoid Compression And Obfuscation

Use:

- `upx=False`;
- one-folder PyInstaller output;
- clear app name, icon, version metadata, and publisher information where possible;
- no unnecessary packed or encrypted resources.

Packed and compressed executables are more likely to trigger heuristic scanners.

### 4. Make Builds Traceable

Publish:

- release tag;
- exact version;
- asset names;
- SHA256 hashes;
- VirusTotal report links;
- short explanation that the GitHub source archives are generated from the repository tag.

For more reproducible builds, set deterministic build variables in the build script:

```powershell
$env:PYTHONHASHSEED = "1"
$env:SOURCE_DATE_EPOCH = "1781481600"
```

Use a real timestamp for `SOURCE_DATE_EPOCH` when you formalize the release process.

### 5. Scan The Final Artifact

Scan the exact file you upload to GitHub Releases. If you upload both an installer and a portable zip, scan the installer `.exe` and, if possible, the main app `.exe` from inside the zip.

Attach in the release notes:

- VirusTotal link;
- SHA256 hash;
- build date;
- statement that detections, if present, are expected false positives for an unsigned PyInstaller build unless the report shows broad consensus across major engines.

### 6. Submit False Positive Reports

If a major antivirus engine flags the release, submit the file to that vendor as a false positive. Include:

- GitHub repository URL;
- release URL;
- SHA256 hash;
- VirusTotal link;
- short explanation that it is a PyInstaller-packaged Python desktop app.

## References

- [PyInstaller: What PyInstaller does](https://pyinstaller.org/en/stable/operating-mode.html)
- [PyInstaller: Using PyInstaller](https://pyinstaller.org/en/stable/usage.html)
- [PyInstaller: Reproducible builds](https://pyinstaller.org/en/stable/advanced-topics.html#creating-a-reproducible-build)
- [GitHub Docs: About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub Docs: Renaming a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)
- [GitHub Docs: Understanding GitHub Actions](https://docs.github.com/en/actions/get-started/understand-github-actions)
- [VirusTotal Docs: How it works](https://docs.virustotal.com/docs/how-it-works)
- [VirusTotal API: Files](https://docs.virustotal.com/reference/files)
