# Citron Neo Windows Updater

Official external updater utility for **Citron Neo** (Windows only), built with Python + CustomTkinter.

It can pull builds from any of three official channels:

- **Stable** — <https://github.com/citron-neo/emulator/releases>
- **Nightly CI (MSVC)** — <https://github.com/citron-neo/CI/releases>
- **PR Builds** — <https://github.com/citron-neo/PR/tags> (release assets are pulled from `citron-neo/PR/releases`)

The Clangtron (MinGW-w64) toolchain is no longer produced upstream, so the
nightly CI channel only ships MSVC artifacts.

## Features

- Automatic update check on startup
- Manual **Check for Updates** and **Update Now**
- Current version vs latest version display
- Switchable release channel: Stable / Nightly CI / PR Builds (Nightly CI is the default)
- Modern dark-mode UI (CustomTkinter)
- Download + extraction progress bar
- Detailed log panel
- Configurable install path (default: `%APPDATA%\citron`)
- First-run setup popup to choose install path
- Optional import from older portable install (`user` folder copy)
- Launch Citron Neo directly from updater
- Error handling for network/download/extract/permission failures

## Project Structure

- `main.py` - entry point
- `ui.py` - CustomTkinter user interface
- `updater.py` - core update logic (GitHub check, download, extract, replace, launch)
- `requirements.txt` - Python dependencies

## Requirements

- Windows 10/11
- Python 3.10+ recommended

## Setup (Development)

1. Create and activate virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Run:

   ```powershell
   python main.py
   ```

## Usage

1. Launch updater.
2. On first run, choose the install/update folder in setup popup.
3. Optional: import data from an older portable install by selecting the folder containing `user`.
4. Choose a release channel (`Stable`, `Nightly CI - MSVC`, or `PR Builds`).
5. Click **Check for Updates**.
6. Click **Update Now** if an update is available.
7. Click **Launch Citron Neo** after success.

Switching the channel triggers a fresh check against the new repository.
Builds applied from different channels are tracked independently — moving from
nightly to stable (or vice versa) will be detected as an update.

The updater stores config in:

- `%APPDATA%\CitronNeoUpdater\config.json`

And stores installed release marker in your install directory:

- `.citron_updater_version.json`

## Building a Standalone EXE

Use PyInstaller:

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name "CitronNeoUpdater" main.py
```

Generated binary:

- `dist\CitronNeoUpdater.exe`

Distribute this single EXE as the standalone updater.

## Notes

- The updater is designed to run as a separate process from Citron Neo so files can be replaced safely.
- If update fails with permission errors, close Citron Neo and retry.
- GitHub API rate limiting can affect anonymous requests in high-traffic scenarios.
