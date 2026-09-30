# Immich → Google Photos dedupe

This local web UI compares an extracted Google Photos Takeout with one Immich user's library. It writes an auditable JSON report and CSV review sheet, lets you review which matches to keep, and can move **byte-for-byte proven** matches to Google Photos trash through a visible browser. A command-line interface is also available.

The scanner never deletes or modifies media. The trash command is opt-in, requires an exact confirmation phrase, only accepts exact SHA-1 matches with a Google Photos URL, and uses Google Photos' recoverable trash—not permanent deletion.

Before opening the browser, the trash command also re-hashes every candidate's Takeout file and cross-checks the Google and Immich hashes stored in the report. Missing, changed, unreliable, offline, or tampered evidence aborts the run.

## Why Takeout and a browser are required

Google removed broad Library API scopes after March 31, 2025. The supported Library API can now manage only media created by the calling app, while the Picker API is read-only and requires the user to select items. There is no supported Google Photos API operation for deleting arbitrary items in a user's existing library.

For that reason this tool does **not** ask for a Google password or pretend an API can do something it cannot:

1. Google Takeout supplies the original files, capture metadata, and—when present—the item's Google Photos URL.
2. The Immich API key limits the inventory to the intended Immich user.
3. SHA-1 is computed over Takeout bytes and compared with Immich's content checksum.
4. The optional trash phase opens those Takeout URLs in a visible browser using a dedicated profile that you first sign in to yourself.

Browser automation is inherently more fragile than an official API. Use **Test 1 selected item** in the web UI first, verify the result in Google Photos and in the generated log, then consider a larger run. If Google's accessible button labels change, the tool fails closed and saves a screenshot instead of guessing.

## Requirements for the web UI

- **Run this utility on the computer that can read your extracted Takeout files.** Point it at the extracted `Takeout/Google Photos` directory, not a `.zip` file. Keep the originals available until you finish any trash runs, because the tool re-hashes selected Takeout files before opening Google Photos. [Google Takeout](https://takeout.google.com/)
- **Python 3.10 or newer**, with `venv` and `pip`. Installing this project also installs its `requests` dependency. Use a writable project directory for local settings and the `reports/` folder.
- **A running Immich server**, its URL reachable from the computer running this utility, and an API key from the Immich user whose assets you want to compare. In Immich, open the user menu → **Account Settings** → **API Keys**; give the key asset read access. This tool only reads Immich. [Immich user settings](https://docs.immich.app/features/user-settings/)
- **For the Google Photos trash buttons:** install the project's `[browser]` extra, plus regular Google Chrome or Microsoft Edge on the **same operating system as the Python process**. You need a graphical desktop where that browser can open. Playwright currently lists Windows 11+, Debian 12/13, and Ubuntu 22.04/24.04/26.04 among its supported systems; other platforms are not verified here. The separate sign-in browser uses a dedicated local profile. Do not enter your Google password into this utility. [Playwright browser channels](https://playwright.dev/python/docs/browsers/), [supported operating systems](https://playwright.dev/python/docs/intro/)
- **Docker is required only to run Immich, if that is how you deployed it.** The utility itself runs directly on Windows or Linux. Its **Find Immich in Docker** button additionally needs a working `docker` command with permission to inspect the local Immich container; you can enter the URL and mappings manually without it.

By default, the web UI listens only on `127.0.0.1:8765`. Any modern browser can display the UI. Chrome or Edge and Playwright are needed only for its trash buttons.

### Windows with Docker Desktop

Use **PowerShell on Windows** for the commands below. Install [Python 3.10+](https://www.python.org/downloads/windows/) and [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) if needed. Start Docker Desktop in Linux-container mode and confirm Immich is running. Docker Desktop can use WSL 2 as its backend; you do **not** need to install or run this utility inside WSL for the Windows instructions. [Docker Desktop WSL 2 guide](https://docs.docker.com/desktop/features/wsl/)

```powershell
py -3 --version
docker ps
```

To clone the repository, Git and access to this repository are required. If you already have the project folder or downloaded its ZIP, open PowerShell in that folder and start at the `py -3 -m venv` line instead.

```powershell
git clone https://github.com/adybhav/ImmichToGooglePhotosDelete.git
cd ImmichToGooglePhotosDelete
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\python.exe -m immich_google_dedupe.cli web
```

The final command opens `http://127.0.0.1:8765/`. Keep that terminal open while using the UI; press `Ctrl+C` to stop it. Using the virtual environment's Python directly avoids PowerShell script activation and global `PATH` issues. If `py` is unavailable, use a `python` command that reports version 3.10 or newer. If an existing `.venv` points to a removed Python installation, move that old environment aside and create a fresh one before running the install command.

Choose **Windows paths** in the UI, for example `D:\GoogleTakeout\Takeout\Google Photos`. If Docker reports a WSL mount such as `/mnt/d/immich-library` mounted at `/data`, the Windows mapping is `/data=D:\immich-library`; **Find Immich in Docker** suggests it when that folder is readable. Immich's default internal media directory is `/data`, while the host upload directory is set by `UPLOAD_LOCATION` in Immich's Compose `.env`. [Immich Compose setup](https://docs.immich.app/install/docker-compose/), [Immich environment variables](https://docs.immich.app/install/environment-variables/)

### Linux desktop with Docker Engine or Compose

Install Python 3.10+ with `venv` and `pip`, and install regular Chrome or Edge if you plan to use the trash buttons. Use a Linux graphical desktop; a headless server cannot complete this tool's visible Google sign-in and trash flow as documented. Playwright lists supported Debian and Ubuntu versions in its [system requirements](https://playwright.dev/python/docs/intro/). If you run Immich locally in Docker, `docker ps` should show its server container. [Immich Docker Compose setup](https://docs.immich.app/install/docker-compose/)

On Debian or Ubuntu, install the distribution's `python3-venv` package if `python3 -m venv` reports that `ensurepip` is unavailable. To clone the repository, Git and access to this repository are required. If you already have the project folder or downloaded its ZIP, open a terminal in that folder and start at the `python3 -m venv` line instead.

```bash
python3 --version
git clone https://github.com/adybhav/ImmichToGooglePhotosDelete.git
cd ImmichToGooglePhotosDelete
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[browser]'
.venv/bin/python -m immich_google_dedupe.cli web
```

If Immich runs in local Docker, `docker ps` should show its server container. If Immich runs on another machine or is not in Docker, enter its reachable URL manually. Open `http://127.0.0.1:8765/` on the same Linux desktop if it does not open automatically. Keep the terminal open; press `Ctrl+C` to stop the UI. Use **Linux paths**, for example `/home/alex/GoogleTakeout/Takeout/Google Photos` and `/data=/srv/immich/library`.

**Running the utility inside WSL instead of Windows:** use the Linux commands, Linux or `/mnt/<drive>/...` paths, and a **Linux** Chrome or Edge browser with working GUI support. Enable Docker Desktop's WSL integration for that distribution if you want Docker discovery there. The Windows Chrome installation and Windows `D:\...` paths do not apply to a Python process running inside WSL. [Docker Desktop WSL integration](https://docs.docker.com/desktop/features/wsl/)

Create a separate virtual environment for each operating system; a Windows `.venv` cannot be reused in Linux or WSL.

If you only want to scan and review, replace `-e ".[browser]"` or `-e '.[browser]'` above with `-e .`; the trash buttons then require installing the `[browser]` extra later. If a Linux browser fails to launch because system libraries are missing, follow [Playwright's Linux dependency instructions](https://playwright.dev/python/docs/browsers/#install-system-dependencies).

### First run in the web UI

1. Select the extracted Google Photos Takeout folder and enter the Immich URL. The usual local URL is `http://localhost:2283`; use the published host port or your remote URL if different. The tool also accepts a URL ending in `/api`. Immich's default server port is `2283`. [Immich environment variables](https://docs.immich.app/install/environment-variables/)
2. Click **Find Immich in Docker** to see local published ports and readable bind mounts. A mapping is `PATH_IN_IMMICH=PATH_THIS_APP_CAN_READ`, one per line. Docker's container path and host source are different paths. Normal Immich uploads can use reliable API content checksums without a mapping; external-library originals need to be readable to prove byte-for-byte matches. [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/), [Immich external libraries](https://docs.immich.app/guides/external-library/)
3. Enter the Immich API key and click **Check setup**. It tests the connection and up to 100 asset paths without hashing the library. A successful sample does not prove every external-library path is available. Click **Save settings** to keep the Takeout folder, URL, mappings, report path, and worker count in the ignored `.immich-gphotos-settings.json` file. The API key is not saved.
4. Click **Build duplication report**. This is read-only and may take time for a large library. The JSON and CSV are written to `reports/` by default. If that JSON already exists, the UI loads it on startup; you can review it without rescanning. The scan uses four file-hash workers by default; directory traversal, Immich API pagination, and report writing are sequential.
5. Review the exact matches. Uncheck a folder or item to keep it in Google Photos. A repeated Takeout item is excluded when any one of its folders is unchecked. The **Keep photos and videos at or below** rule defaults to **2 MiB**; change it and click **Apply** without rebuilding the report. It uses Takeout file bytes, which may differ from Google's displayed processed size. Review selections are saved in `reports/review-selection.json`.
6. If you choose to move items to trash, click **Open sign-in browser**, sign in to the intended Google account in the regular Chrome/Edge window, and close the **entire window**. Click **Test 1 selected item**, verify the account in the automated browser, and check the result in Google Photos trash. Then use **Move selected to trash** when ready. Both actions require the displayed confirmation phrase. Each run writes a `reports/trash-run-*.json` log and skips items already recorded as moved.

Google can reject sign-in in a browser controlled by Playwright, which is why this utility separates human sign-in from the automated trash run. For a report copied from another operating system, saved absolute Takeout paths still refer to the original computer; build a new report before trashing there. Saved UI exclusions and the media size rule apply to the UI's trash buttons; the CLI refuses `trash --execute` for that report while these are active unless explicitly given `--ignore-review-selection`.

## Optional command-line usage

The web UI is the recommended workflow. The scan also supports common image/video and RAW formats including DNG and CR2.

### Command-line scan

The PowerShell CLI examples below assume the project's virtual environment is active. Alternatively, run `.\.venv\Scripts\immich-gphotos-dedupe.exe` in place of `immich-gphotos-dedupe` on Windows, or `.venv/bin/immich-gphotos-dedupe` on Linux. Prefer an environment variable so the API key does not appear in shell history:

```powershell
$env:IMMICH_API_KEY = "your-user-api-key"
immich-gphotos-dedupe scan `
  --takeout "D:\Takeout" `
  --immich-url "http://immich.local:2283" `
  --workers 4 `
  --output ".\reports\duplication-report.json"
```

This produces:

- `duplication-report.json`: machine-readable evidence, match classifications, and summary
- `duplication-report.csv`: reviewable spreadsheet

The scanner hashes local Immich originals and Takeout media with four file workers by default. Use
`--workers 1` for sequential hashing or adjust the count for your storage. Results retain Takeout
path order regardless of worker completion order. Directory scanning, Immich API pagination, and
report writing remain sequential.

The summary separates:

- `exact_hash_matches`: byte-for-byte content proof; eligible for the guarded trash phase if a URL exists
- `strong_metadata_matches_review_only`: filename/type/timestamp evidence but no content proof
- `ambiguous_metadata_matches_review_only`: more than one equally strong metadata candidate
- `unmatched`: keep in Google Photos

### Immich external libraries and Docker paths

For normal Immich uploads, the API checksum is a content SHA-1. For Immich **external library** assets, the checksum can be derived from the path rather than file bytes, so this tool deliberately treats it as untrusted. Give the scanner local access to external originals to calculate a real content hash. The web UI's Docker discovery can suggest mappings and its setup check can verify a sample of paths. The CLI also accepts repeatable explicit mappings:

```powershell
immich-gphotos-dedupe scan `
  --takeout "D:\Takeout" `
  --immich-url "http://localhost:2283" `
  --path-map "/data=D:\immich-library" `
  --path-map "/photos=Z:\Photos"
```

On a Linux host, a mapping might be `/data=/srv/immich/library`. The right side must be a directory readable by this tool on the machine where it runs.

The tool only reads mapped files. It never deletes from Immich or the Takeout folder.

For a trusted local Immich instance using a self-signed certificate, add `--insecure`.

### Command-line review before changing Google Photos

Open the CSV and spot-check exact matches. Confirm that:

- the intended Immich user's API key was used;
- `unresolved_external_assets` is zero (or you understand those assets were excluded from exact matching);
- the Google URL and Immich item identify the same photo/video;
- the Takeout export is current enough for your purpose.

Dry-run the trash command:

```powershell
immich-gphotos-dedupe trash --report ".\reports\duplication-report.json" --limit 1
```

### Command-line one-item trash test

First sign in using regular Chrome/Edge with the same dedicated profile the trash command uses:

```powershell
immich-gphotos-dedupe auth
```

Verify the intended Google account in that browser and **close the entire sign-in window** before the
trash run. This sign-in step does not read or change the duplication report or move any photos.

```powershell
immich-gphotos-dedupe trash `
  --report ".\reports\duplication-report.json" `
  --limit 1 `
  --execute
```

You must type the displayed confirmation phrase. Chrome then opens with the already signed-in
profile. Visually verify the intended account and return to the terminal. If Google asks you to sign
in again, use `auth` in regular Chrome/Edge before retrying; the trash run stops if the Photos page
redirects to sign-in. The tool records each result in `reports/trash-results.json`. Check Google
Photos trash before continuing with a larger batch.

Resume in bounded batches with `--start-at` and `--limit`:

```powershell
immich-gphotos-dedupe trash `
  --report ".\reports\duplication-report.json" `
  --start-at 1 `
  --limit 25 `
  --execute
```

If Google Photos is not in English, pass regular expressions for the visible trash buttons:

```powershell
immich-gphotos-dedupe trash `
  --report ".\reports\duplication-report.json" `
  --trash-button-pattern "^(your localized label)$" `
  --confirm-button-pattern "^(your localized confirmation)$" `
  --limit 1 --execute
```

## Important safety properties and limitations

- No command permanently deletes Google media. Google Photos trash retention and behavior are controlled by Google.
- Only exact content matches are automated. Metadata matches remain review-only even if their score is high.
- A Takeout sidecar without a supported `photos.google.com` URL cannot be automated.
- Edited Google Photos items, motion-photo pairs, transcoded videos, or exports whose bytes differ may appear as metadata-only or unmatched.
- One exact Immich asset can prove that multiple byte-identical Google items are duplicated; each Google item remains a separate report row.
- If Takeout repeats one cloud item in multiple album folders, equal Google Photos URLs are collapsed into one report row and the additional export paths remain recorded in `repeated_takeout_paths`.
- The browser profile contains a login session and is git-ignored. Protect it like any browser profile.
- UI automation can break when Google changes Photos. Failures are logged and screenshotted; the tool does not fall back to keyboard shortcuts or broad selectors.

## Developer checks

```powershell
python -m pip install -e ".[dev]"
pytest
```
