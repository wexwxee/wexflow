# WexFlow

WexFlow is a Windows desktop app for finding and applying to entry-level jobs in Denmark.

It combines two local tools in one app:

- **Salling Jobs**: job feed, filters, job states, routes, profile data, CV/cover letter handling, and assisted applications.
- **7-Eleven Apply Studio**: profile, store selection, address/location tools, and assisted Typeform applications.

The app runs locally on your PC. Your profile, documents, credentials, and job state are stored on your machine, not in this repository.

> WexFlow is an independent helper app. It is not affiliated with Salling Group, 7-Eleven, or their hiring platforms.

## Download

1. Open the latest release:
   [github.com/wexwxee/wexflow/releases/latest](https://github.com/wexwxee/wexflow/releases/latest)
2. Download the file named like:
   `WexFlow-1.0.10.zip`
3. Extract the zip anywhere you want, for example:
   `C:\Apps\WexFlow`
4. Open the extracted folder and run:
   `WexFlow.exe`

On first launch Windows may show a security warning because the app is not code-signed yet. Choose **More info** -> **Run anyway** if you trust this build.

## Requirements

- Windows 10 or Windows 11.
- Internet connection.
- Microsoft Edge WebView2 Runtime. It is already installed on most Windows PCs.
- For automatic applications, WexFlow may download a Playwright Chromium browser on first use. This is saved in your Windows user data folder.

You do **not** need Python, Node.js, Git, or any developer tools to use the release version.

## Updating

WexFlow checks GitHub Releases for a newer version.

When an update is available:

1. Open WexFlow.
2. Use the update button/banner on the main screen.
3. The app downloads the new zip.
4. WexFlow closes, replaces its files, and opens again.

You can also update manually:

1. Download the newest `WexFlow-*.zip` from [Releases](https://github.com/wexwxee/wexflow/releases).
2. Close WexFlow.
3. Extract the new zip over the old WexFlow folder, or extract it to a fresh folder.
4. Run `WexFlow.exe`.

Your personal data is kept separately in `%AppData%\WexFlow`, so updating the app should not delete your profile or documents.

## First Setup

After opening WexFlow:

1. Go to **Settings**.
2. Fill in your profile:
   - name
   - email
   - Danish phone number
   - address
   - postal code
   - city
   - country
3. Add your CV and optional cover letter.
4. Save the profile.
5. Open **Salling Jobs** or **7-Eleven Apply Studio**.

The app autosaves most profile changes, but important missing fields are still checked before running application flows.

## Salling Jobs

Salling Jobs helps you browse and manage vacancies from Salling Group brands.

Main features:

- Filter jobs by search text, city, brand, region, category, job type, level, status, and radius.
- Sort jobs by distance, date, title, or city.
- Show routes from your home address to the job location.
- Mark jobs as viewed, applied, hidden, interview, offer, or rejected.
- Hide already-applied jobs or show them together with all jobs.
- Use profile data and documents while preparing applications.

Job cards use calm visual states:

- new
- viewed
- applied
- hidden
- selected for batch action

## 7-Eleven Apply Studio

7-Eleven Apply Studio helps prepare applications for selected stores.

Main features:

- Fill and save the applicant profile.
- Select stores across Denmark.
- Search stores by city, address, region, or area.
- Use your location or a custom starting address.
- Sort stores by city or distance.
- Choose transport mode for routes.
- Prepare and run assisted Typeform applications.

Each selected store gets its own application run.

## Where Data Is Stored

User data is stored locally on your PC.

Common locations:

- `%AppData%\WexFlow` - app user data and downloaded browser files.
- WexFlow app folder - application files from the extracted zip.

Depending on the version and migration state, local project files may include:

- `jobs.db` - local job database.
- `profile.json` / settings files - saved profile values.
- `uploads` - selected CV and cover letter copies.
- log files - last run and troubleshooting information.

Do not share your local profile, credentials, documents, or logs publicly.

## Troubleshooting

### The app does not open

Try this:

1. Make sure the zip was extracted before running `WexFlow.exe`.
2. Close all old WexFlow processes in Task Manager.
3. Run `WexFlow.exe` again.
4. If Windows blocks it, use **More info** -> **Run anyway**.

### The screen is blank or old

Close WexFlow completely and reopen it.

WexFlow uses local background servers. If an old process is still running, the app may show an old cached interface. Newer versions try to clean these processes automatically at startup.

### Location does not work

Check Windows settings:

1. Open Windows **Settings**.
2. Go to **Privacy & security** -> **Location**.
3. Enable location services.
4. Reopen WexFlow and try again.

You can always enter an address manually if Windows location is unavailable.

### Auto-apply fails

Hiring websites can change their forms at any time. If an application flow fails:

1. Check that your profile fields are filled.
2. Check that CV and cover letter paths are valid.
3. Try a preparation/dry-run flow first if available.
4. Review the final form before submitting.

## Developer Notes

This repository contains the source code and build scripts.

For normal users, use the zip from GitHub Releases. Do not clone the repository unless you want to develop or debug the app.

Development build commands may use:

```powershell
cd C:\saling
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The distributable Windows app is produced with PyInstaller and packaged as:

```text
dist\WexFlow-<version>.zip
```

## Privacy And Safety

WexFlow is designed as a local personal assistant.

- Keep your documents and credentials private.
- Check application forms before submitting.
- Use the tool responsibly and follow the rules of the hiring websites you interact with.
