from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from zipfile import BadZipFile, ZipFile

import requests

APP_NAME = "Citron Neo Updater"
REPO_API_RELEASES = "https://api.github.com/repos/citron-neo/CI/releases"
DEFAULT_TIMEOUT = 30
VERSION_MARKER_NAME = ".citron_updater_version.json"
KNOWN_PROCESS_NAMES = ("citron-neo.exe", "citron.exe", "yuzu.exe")
CONFIG_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "CitronNeoUpdater"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_INSTALL_PATH = Path(os.getenv("APPDATA", str(Path.home()))) / "citron"


class UpdaterError(Exception):
    """Base error for updater failures."""


class NetworkError(UpdaterError):
    """Raised when network/API calls fail."""


class UpdateApplyError(UpdaterError):
    """Raised when extracted files cannot be applied."""


@dataclass
class ReleaseInfo:
    name: str
    tag_name: str
    published_at: str
    release_id: int
    asset_name: str
    asset_url: str
    asset_size: int
    asset_updated_at: str


@dataclass
class CheckResult:
    current_version: str
    latest_version: str
    update_available: bool
    release: Optional[ReleaseInfo]


class ConfigStore:
    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "install_path": str(DEFAULT_INSTALL_PATH),
                "last_installed_version": "Unknown",
                "install_path_prompted": False,
            }
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {
                "install_path": str(DEFAULT_INSTALL_PATH),
                "last_installed_version": "Unknown",
                "install_path_prompted": False,
            }

        data.setdefault("install_path", str(DEFAULT_INSTALL_PATH))
        data.setdefault("last_installed_version", "Unknown")
        data.setdefault("install_path_prompted", False)
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


class UpdaterService:
    def __init__(
        self,
        config_store: Optional[ConfigStore] = None,
    ) -> None:
        self.config_store = config_store or ConfigStore()

    def get_install_path(self) -> Path:
        cfg = self.config_store.load()
        return Path(cfg.get("install_path", str(DEFAULT_INSTALL_PATH)))

    def set_install_path(self, install_path: str) -> None:
        cfg = self.config_store.load()
        cfg["install_path"] = install_path
        self.config_store.save(cfg)

    def has_completed_install_prompt(self) -> bool:
        cfg = self.config_store.load()
        return bool(cfg.get("install_path_prompted", False))

    def mark_install_prompt_completed(self) -> None:
        cfg = self.config_store.load()
        cfg["install_path_prompted"] = True
        self.config_store.save(cfg)

    def get_current_version(self, install_path: Path) -> str:
        marker = install_path / VERSION_MARKER_NAME
        if marker.exists():
            try:
                with marker.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                tag = str(data.get("tag_name") or data.get("version") or "Unknown")
                asset = str(data.get("asset_name") or "").strip()
                if asset:
                    return f"{tag} ({asset})"
                return tag
            except (OSError, json.JSONDecodeError):
                pass

        for candidate in ("version.txt", "VERSION", "citron-version.txt"):
            fpath = install_path / candidate
            if fpath.exists():
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    continue
                if text:
                    return text.splitlines()[0][:120]

        cfg = self.config_store.load()
        return str(cfg.get("last_installed_version", "Unknown"))

    def check_for_updates(self, install_path: Optional[Path] = None) -> CheckResult:
        install_path = install_path or self.get_install_path()
        current = self.get_current_version(install_path)
        release = self._fetch_latest_windows_release()
        latest = f"{release.tag_name or release.name or 'Unknown'} ({release.asset_name})"
        update_available = self._is_update_available(install_path=install_path, latest_release=release)
        return CheckResult(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
            release=release,
        )

    def _fetch_latest_windows_release(self) -> ReleaseInfo:
        try:
            resp = requests.get(REPO_API_RELEASES, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            releases = resp.json()
        except requests.RequestException as exc:
            raise NetworkError(f"Unable to fetch release data: {exc}") from exc
        except ValueError as exc:
            raise NetworkError("GitHub API returned invalid JSON.") from exc

        if not isinstance(releases, list) or not releases:
            raise NetworkError("No releases found for citron-neo/CI.")

        # Prefer releases that look like continuous/stable builds for Windows.
        for rel in releases:
            release_name = f"{rel.get('name', '')} {rel.get('tag_name', '')}".lower()
            if "continuous" not in release_name and "stable" not in release_name:
                # Keep scanning but do not discard if not labeled.
                pass

            assets = rel.get("assets", [])
            if not isinstance(assets, list):
                continue

            best_asset = self._pick_windows_stable_asset(assets)
            if best_asset:
                return ReleaseInfo(
                    name=str(rel.get("name", "Continuous Build")),
                    tag_name=str(rel.get("tag_name", "Unknown")),
                    published_at=str(rel.get("published_at", "")),
                    release_id=int(rel.get("id", 0)),
                    asset_name=str(best_asset.get("name", "")),
                    asset_url=str(best_asset.get("browser_download_url", "")),
                    asset_size=int(best_asset.get("size", 0)),
                    asset_updated_at=str(best_asset.get("updated_at", "")),
                )

        raise NetworkError("No suitable Windows Stable zip artifact was found.")

    def _pick_windows_stable_asset(self, assets: list[dict]) -> Optional[dict]:
        scored: list[tuple[int, dict]] = []
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if not name.endswith(".zip"):
                continue

            score = 0
            if "windows" in name or "win" in name:
                score += 5
            if "stable" in name:
                score += 4
            if "citron" in name:
                score += 2
            if "debug" in name or "symbols" in name:
                score -= 3

            scored.append((score, asset))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def download_release(
        self,
        release: ReleaseInfo,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Path:
        temp_dir = Path(tempfile.mkdtemp(prefix="citron_update_dl_"))
        zip_path = temp_dir / release.asset_name
        if progress_cb:
            progress_cb(0.0, f"Downloading {release.asset_name} ...")

        try:
            with requests.get(release.asset_url, stream=True, timeout=DEFAULT_TIMEOUT) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", release.asset_size or 0))
                downloaded = 0

                with zip_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total > 0:
                            ratio = min(downloaded / total, 1.0)
                            progress_cb(ratio, f"Downloading... {downloaded // 1024 // 1024} MB")
        except requests.RequestException as exc:
            raise NetworkError(f"Download failed: {exc}") from exc
        except OSError as exc:
            raise UpdateApplyError(f"Unable to write downloaded file: {exc}") from exc

        if progress_cb:
            progress_cb(1.0, "Download complete.")
        return zip_path

    def extract_release(
        self,
        zip_path: Path,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Path:
        target_dir = Path(tempfile.mkdtemp(prefix="citron_update_extract_"))
        if progress_cb:
            progress_cb(0.0, "Extracting update package ...")
        try:
            with ZipFile(zip_path, "r") as zf:
                members = zf.infolist()
                total = len(members) or 1
                for i, member in enumerate(members, start=1):
                    zf.extract(member, target_dir)
                    if progress_cb:
                        progress_cb(i / total, f"Extracting... {i}/{total}")
        except (BadZipFile, OSError) as exc:
            raise UpdateApplyError(f"Extraction failed: {exc}") from exc

        if progress_cb:
            progress_cb(1.0, "Extraction complete.")
        return target_dir

    def apply_update(
        self,
        extracted_dir: Path,
        install_path: Path,
        release: ReleaseInfo,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        install_path.mkdir(parents=True, exist_ok=True)
        source_root = self._resolve_extracted_root(extracted_dir)
        files = [p for p in source_root.rglob("*") if p.is_file()]
        total = len(files) or 1

        if progress_cb:
            progress_cb(0.0, "Applying update files ...")

        for idx, src in enumerate(files, start=1):
            rel = src.relative_to(source_root)
            dst = install_path / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except PermissionError as exc:
                raise UpdateApplyError(
                    f"Permission denied while writing '{dst}'. Close Citron Neo and retry."
                ) from exc
            except OSError as exc:
                raise UpdateApplyError(f"Failed to replace file '{dst}': {exc}") from exc

            if progress_cb:
                progress_cb(idx / total, f"Updating... {idx}/{total}")

        marker_path = install_path / VERSION_MARKER_NAME
        marker_payload = {
            "tag_name": release.tag_name,
            "name": release.name,
            "published_at": release.published_at,
            "release_id": release.release_id,
            "asset_name": release.asset_name,
            "asset_size": release.asset_size,
            "asset_updated_at": release.asset_updated_at,
        }
        try:
            marker_path.write_text(json.dumps(marker_payload, indent=2), encoding="utf-8")
            cfg = self.config_store.load()
            cfg["last_installed_version"] = release.tag_name
            self.config_store.save(cfg)
        except OSError as exc:
            raise UpdateApplyError(f"Updated files but failed to write version marker: {exc}") from exc

        if progress_cb:
            progress_cb(1.0, "Update applied successfully.")

    def launch_citron(self, install_path: Optional[Path] = None) -> None:
        install_path = install_path or self.get_install_path()
        candidates = [
            install_path / "citron-neo.exe",
            install_path / "Citron Neo.exe",
            install_path / "citron.exe",
            install_path / "yuzu.exe",
        ]
        exe_path = next((p for p in candidates if p.exists()), None)
        if not exe_path:
            raise UpdaterError(
                "Could not locate Citron Neo executable in install path. "
                "Update once or verify install path in Settings."
            )

        try:
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
        except OSError as exc:
            raise UpdaterError(f"Failed to launch Citron Neo: {exc}") from exc

    def import_portable_user_folder(self, source_folder: Path, install_path: Path) -> int:
        source_user = source_folder / "user"
        if not source_user.exists() or not source_user.is_dir():
            raise UpdaterError(
                f"Portable source does not contain a 'user' folder: {source_user}"
            )

        target_user = install_path / "user"
        target_user.mkdir(parents=True, exist_ok=True)

        copied_files = 0
        for src in source_user.rglob("*"):
            relative = src.relative_to(source_user)
            dst = target_user / relative
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except PermissionError as exc:
                raise UpdaterError(
                    f"Permission denied while importing '{src}'. Close Citron Neo and retry."
                ) from exc
            except OSError as exc:
                raise UpdaterError(f"Failed to import '{src}': {exc}") from exc
            copied_files += 1

        return copied_files

    def run_full_update(
        self,
        release: ReleaseInfo,
        install_path: Optional[Path] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        install_path = install_path or self.get_install_path()
        zip_path = self.download_release(release, progress_cb=progress_cb)
        extracted = self.extract_release(zip_path, progress_cb=progress_cb)
        if self.is_citron_running():
            raise UpdateApplyError(
                "Citron Neo appears to be running. Close it before applying the update."
            )
        self.apply_update(extracted, install_path, release, progress_cb=progress_cb)

    def _resolve_extracted_root(self, extracted_dir: Path) -> Path:
        entries = list(extracted_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extracted_dir

    def is_citron_running(self) -> bool:
        for proc_name in KNOWN_PROCESS_NAMES:
            if self._is_process_running(proc_name):
                return True
        return False

    def _is_process_running(self, process_name: str) -> bool:
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            # If tasklist is unavailable, do not block update flow here.
            return False

        output = (completed.stdout or "").lower()
        return process_name.lower() in output

    def _is_update_available(self, install_path: Path, latest_release: ReleaseInfo) -> bool:
        marker = install_path / VERSION_MARKER_NAME
        latest_signature = self._release_signature(latest_release)
        if marker.exists():
            try:
                with marker.open("r", encoding="utf-8") as f:
                    marker_data = json.load(f)
                installed_signature = self._marker_signature(marker_data)
                if installed_signature and latest_signature:
                    return installed_signature != latest_signature
            except (OSError, json.JSONDecodeError):
                pass

        current = self.get_current_version(install_path)
        latest_tag = latest_release.tag_name or latest_release.name or "Unknown"
        return current != latest_tag

    def _release_signature(self, release: ReleaseInfo) -> str:
        return "|".join(
            [
                str(release.asset_name or ""),
                str(release.asset_size or ""),
                str(release.asset_updated_at or ""),
            ]
        )

    def _marker_signature(self, marker_data: dict) -> str:
        return "|".join(
            [
                str(marker_data.get("asset_name") or ""),
                str(marker_data.get("asset_size") or ""),
                str(marker_data.get("asset_updated_at") or ""),
            ]
        )
