# Immich → Google Photos dedupe

This command-line tool measures how much of an extracted Google Photos Takeout is already present for one Immich user. It writes an auditable JSON report and CSV review sheet. A separate, heavily guarded command can move **byte-for-byte proven** matches to Google Photos trash through a visible browser.

The scanner never deletes or modifies media. The trash command is opt-in, requires an exact confirmation phrase, only accepts exact SHA-1 matches with a Google Photos URL, and uses Google Photos' recoverable trash—not permanent deletion.

Before opening the browser, the trash command also re-hashes every candidate's Takeout file and cross-checks the Google and Immich hashes stored in the report. Missing, changed, unreliable, offline, or tampered evidence aborts the run.

## Why Takeout and a browser are required

Google removed broad Library API scopes after March 31, 2025. The supported Library API can now manage only media created by the calling app, while the Picker API is read-only and requires the user to select items. There is no supported Google Photos API operation for deleting arbitrary items in a user's existing library.

For that reason this tool does **not** ask for a Google password or pretend an API can do something it cannot:

1. Google Takeout supplies the original files, capture metadata, and—when present—the item's Google Photos URL.
2. The Immich API key limits the inventory to the intended Immich user.
3. SHA-1 is computed over Takeout bytes and compared with Immich's content checksum.
4. The optional trash phase opens those Takeout URLs in a visible browser using a dedicated profile that you first sign in to yourself.

Browser automation is inherently more fragile than an official API. Start with `--limit 1`, verify the result in Google Photos and in the generated log, then increase the batch size. If Google's accessible button labels change, the tool fails closed and saves a screenshot instead of guessing.

## Requirements

- Python 3.10+
- An extracted [Google Takeout](https://takeout.google.com/) export containing Google Photos
- Immich URL and an API key created for the specific user (asset read permission)
- Optional trash phase: installed Google Chrome and the `browser` package extra

Do not pass a Google password to this tool. Sign in only in the separate, regular Chrome/Edge window opened by the **Open sign-in browser** button or `auth` command. Google can reject sign-in in a browser controlled by Playwright.

## Install

PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For the optional browser-assisted trash command:

```powershell
python -m pip install -e ".[browser]"
```

The default `--browser-channel auto` uses an existing Chrome installation, or Microsoft Edge on Windows when Chrome is absent, so no Playwright browser download is required. You can select `chrome`, `msedge`, or `chromium` explicitly.

## 1. Scan and calculate duplication

### Local browser review UI

From this project folder on the configured Windows PC, run:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
py -3.14 -m immich_google_dedupe.cli web
```

The UI opens on `127.0.0.1` with the extracted Takeout folder, `http://localhost:2283`,
`/data=D:\immich-library`, and the JSON report path prefilled. An existing report loads automatically;
you do not need to scan again to review or trash its candidates. Review exact-hash, URL-backed
candidates and uncheck any Takeout folder or individual item you want to keep in both places.
The **Keep photos and videos at or below** rule is on by default at **2 MiB** (2,097,152 bytes). You can
turn it off or enter another MiB value and click **Apply**. It uses file sizes already stored in
the JSON report, so changing it does not rebuild the report or hash excluded media. It applies
to both images and videos. Items exactly at the threshold are kept in Google Photos. The cutoff
uses Takeout file bytes, which may differ from a size displayed by Google Photos for a processed item.
Before **Test one** or **Move selected to trash**, click **Open sign-in browser**, sign in to the
intended Google account in regular Chrome/Edge, and close the entire sign-in browser window.
Closing only its tab can leave the dedicated profile in use. The trash action reopens the
same dedicated profile under Playwright and asks you to verify the account before moving anything.
Both trash actions require the displayed confirmation phrase. The UI records each run in a
separate `reports/trash-run-*.json` file and skips items already recorded as moved.

Takeout may export one cloud item in several album folders. Excluding any of its listed folders
excludes that item from the UI's trash selection. Review choices are saved in
`reports/review-selection.json` for the current report. The UI runs only on the local computer and
uses Python's standard library; browser-assisted trash still requires the optional Playwright extra.
Saved UI exclusions and the media size rule apply to the UI's trash buttons. The CLI refuses
`trash --execute` for that report while these are active unless you explicitly pass
`--ignore-review-selection`.

The scan supports common image/video formats and RAW formats including DNG and CR2. File hashing
uses four workers by default; the evidence recheck before trashing can also hash in parallel.

### Command-line scan

Prefer the environment variable so the API key does not appear in shell history:

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

For normal Immich uploads, the API checksum is a content SHA-1. For Immich **external library** assets, the checksum can be derived from the path rather than file bytes, so this tool deliberately treats it as untrusted. Give the scanner local access to originals to calculate a real content hash:

```powershell
immich-gphotos-dedupe scan `
  --takeout "D:\Takeout" `
  --immich-url "http://localhost:2283" `
  --immich-storage "D:\immich\upload"
```

If Immich returns paths from a Docker container or an external mount, use repeatable explicit mappings:

```powershell
immich-gphotos-dedupe scan `
  --takeout "D:\Takeout" `
  --immich-url "http://localhost:2283" `
  --path-map "/usr/src/app/upload=D:\immich\upload" `
  --path-map "/photos=Z:\Photos"
```

The tool only reads mapped files. It never deletes from Immich or the Takeout folder.

For a trusted local Immich instance using a self-signed certificate, add `--insecure`.

## 2. Review before changing Google Photos

Open the CSV and spot-check exact matches. Confirm that:

- the intended Immich user's API key was used;
- `unresolved_external_assets` is zero (or you understand those assets were excluded from exact matching);
- the Google URL and Immich item identify the same photo/video;
- the Takeout export is current enough for your purpose.

Dry-run the trash command:

```powershell
immich-gphotos-dedupe trash --report ".\reports\duplication-report.json" --limit 1
```

## 3. Move a one-item test to Google Photos trash

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
