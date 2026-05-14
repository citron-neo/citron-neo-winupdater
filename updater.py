from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import requests

APP_NAME = "Citron Neo Updater"
DEFAULT_TIMEOUT = 30
VERSION_MARKER_NAME = ".citron_updater_version.json"
KNOWN_PROCESS_NAMES = ("citron-neo.exe", "citron.exe", "yuzu.exe")

# Release channels.
# Upstream no longer ships MSVC or MinGW Windows builds — the only Windows
# toolchain produced is Clangtron (Clang LTO cross-compiled from Linux),
# served from the citron-neo/CI nightly-windows release. PR builds are now
# discovered by scanning open PRs on citron-neo/emulator and reading the
# "Build Artifacts for PR #N" comment that links direct downloads via
# nightly.link.
CHANNEL_STABLE = "stable"
CHANNEL_NIGHTLY = "nightly"
CHANNEL_PR = "pr"
DEFAULT_CHANNEL = CHANNEL_NIGHTLY

CHANNEL_RELEASE_API = {
    CHANNEL_STABLE: "https://api.github.com/repos/citron-neo/emulator/releases",
    CHANNEL_NIGHTLY: "https://api.github.com/repos/citron-neo/CI/releases",
}

# Channels the user may select. PR is sourced from open PRs on
# citron-neo/emulator instead of a releases endpoint, so it isn't part of
# CHANNEL_RELEASE_API but is still a valid channel.
SUPPORTED_CHANNELS = frozenset({CHANNEL_STABLE, CHANNEL_NIGHTLY, CHANNEL_PR})

EMULATOR_PRS_API = "https://api.github.com/repos/citron-neo/emulator/pulls"
EMULATOR_PR_COMMENTS_API = (
    "https://api.github.com/repos/citron-neo/emulator/issues/{number}/comments"
)
EMULATOR_PR_HTML_URL = "https://github.com/citron-neo/emulator/pull/{number}"

GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CitronNeoUpdater",
}

# Cap how many PRs we look up so we don't hammer the GitHub API for every open
# PR (and run into anonymous rate limits).
PR_FETCH_LIMIT = 25

# Restrict PR artifact URLs to nightly.link to avoid being tricked into
# downloading from arbitrary hosts via crafted PR comments.
ALLOWED_PR_ARTIFACT_HOST = "nightly.link"

PR_ARTIFACT_HEADER_RE = re.compile(r"\*\*Build Artifacts for PR\b", re.IGNORECASE)
PR_WINDOWS_ROW_RE = re.compile(
    r"\|\s*\*\*Windows\*\*\s*\|"
    r"(?P<commit>[^|]*)\|"
    r"(?P<artifacts>[^|]*)\|"
    r"(?P<logs>[^|]*)\|",
    re.IGNORECASE,
)
PR_MD_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)")

PR_BUILD_STATUS_READY = "ready"
PR_BUILD_STATUS_BUILDING = "building"
PR_BUILD_STATUS_MISSING = "missing"

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
    channel: str


@dataclass
class PullRequestBuild:
    number: int
    title: str
    author: str
    head_sha: str
    short_sha: str
    pr_url: str
    updated_at: str
    status: str  # one of PR_BUILD_STATUS_*
    artifact_label: Optional[str] = None
    artifact_url: Optional[str] = None
    run_url: Optional[str] = None

    @property
    def display_label(self) -> str:
        title = " ".join(self.title.split()) or "Untitled PR"
        if len(title) > 70:
            title = title[:67] + "..."
        suffix = {
            PR_BUILD_STATUS_READY: "ready",
            PR_BUILD_STATUS_BUILDING: "building",
            PR_BUILD_STATUS_MISSING: "no Windows build",
        }.get(self.status, self.status)
        return f"#{self.number} ({suffix}) - {title}"


@dataclass
class CheckResult:
    current_version: str
    latest_version: str
    update_available: bool
    release: Optional[ReleaseInfo]
    pull_requests: list[PullRequestBuild] = field(default_factory=list)


def _default_config() -> dict:
    return {
        "install_path": str(DEFAULT_INSTALL_PATH),
        "last_installed_version": "Unknown",
        "install_path_prompted": False,
        "preferred_channel": DEFAULT_CHANNEL,
    }


def _normalize_channel(value: object) -> str:
    text = str(value or "").lower().strip()
    if text in SUPPORTED_CHANNELS:
        return text
    # Migrate legacy toolchain values from older configs. The MSVC and MinGW
    # toolchains were retired upstream; both now resolve to the nightly CI
    # channel (Clangtron-only).
    if text in {"msvc", "mingw", "clang", "clangtron"}:
        return CHANNEL_NIGHTLY
    return DEFAULT_CHANNEL


class ConfigStore:
    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return _default_config()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default_config()

        defaults = _default_config()
        for key, value in defaults.items():
            data.setdefault(key, value)

        # Migrate legacy preferred_toolchain key from earlier versions.
        if "preferred_toolchain" in data and "preferred_channel" not in data:
            data["preferred_channel"] = _normalize_channel(data.get("preferred_toolchain"))
        data["preferred_channel"] = _normalize_channel(data.get("preferred_channel"))
        data.pop("preferred_toolchain", None)
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

    def get_preferred_channel(self) -> str:
        cfg = self.config_store.load()
        return _normalize_channel(cfg.get("preferred_channel"))

    def set_preferred_channel(self, channel: str) -> None:
        normalized = str(channel).lower().strip()
        if normalized not in SUPPORTED_CHANNELS:
            raise UpdaterError(f"Unsupported release channel: {channel}")
        cfg = self.config_store.load()
        cfg["preferred_channel"] = normalized
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
                channel = str(data.get("channel") or "").strip().lower()
                channel_label = channel.upper() if channel else ""
                pieces = [p for p in (tag, asset, channel_label) if p]
                if asset and channel_label:
                    return f"{tag} ({asset}, {channel_label})"
                if asset:
                    return f"{tag} ({asset})"
                if channel_label:
                    return f"{tag} ({channel_label})"
                return tag or "Unknown"
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
        channel = self.get_preferred_channel()

        if channel == CHANNEL_PR:
            prs = self.fetch_open_pull_requests()
            ready_count = sum(1 for pr in prs if pr.status == PR_BUILD_STATUS_READY)
            if not prs:
                latest = "No open PRs found"
            elif ready_count:
                latest = f"{ready_count} PR build(s) ready"
            else:
                latest = "No PR Windows builds ready yet"
            return CheckResult(
                current_version=current,
                latest_version=latest,
                update_available=False,
                release=None,
                pull_requests=prs,
            )

        release = self._fetch_latest_windows_release(channel=channel)
        latest = (
            f"{release.tag_name or release.name or 'Unknown'} "
            f"({release.asset_name}, {release.channel.upper()})"
        )
        update_available = self._is_update_available(
            install_path=install_path, latest_release=release
        )
        return CheckResult(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
            release=release,
        )

    def _fetch_latest_windows_release(self, channel: str) -> ReleaseInfo:
        api_url = CHANNEL_RELEASE_API.get(channel)
        if not api_url:
            raise UpdaterError(
                f"Channel {channel!r} does not expose GitHub releases."
            )
        try:
            resp = requests.get(
                api_url,
                timeout=DEFAULT_TIMEOUT,
                headers=GITHUB_API_HEADERS,
            )
            resp.raise_for_status()
            releases = resp.json()
        except requests.RequestException as exc:
            raise NetworkError(
                f"Unable to fetch release data for {channel} channel: {exc}"
            ) from exc
        except ValueError as exc:
            raise NetworkError("GitHub API returned invalid JSON.") from exc

        if not isinstance(releases, list) or not releases:
            raise NetworkError(f"No releases found on the {channel} channel.")

        for rel in releases:
            if rel.get("draft"):
                continue
            assets = rel.get("assets", [])
            if not isinstance(assets, list):
                continue

            best_asset = self._pick_windows_asset(assets)
            if best_asset:
                asset_name = str(best_asset.get("name", ""))
                return ReleaseInfo(
                    name=str(rel.get("name", "") or rel.get("tag_name", "Release")),
                    tag_name=str(rel.get("tag_name", "Unknown")),
                    published_at=str(rel.get("published_at", "")),
                    release_id=int(rel.get("id", 0)),
                    asset_name=asset_name,
                    asset_url=str(best_asset.get("browser_download_url", "")),
                    asset_size=int(best_asset.get("size", 0)),
                    asset_updated_at=str(best_asset.get("updated_at", "")),
                    channel=channel,
                )

        raise NetworkError(
            f"No suitable Windows Clangtron zip artifact was found on the "
            f"{channel} channel."
        )

    def fetch_open_pull_requests(self) -> list[PullRequestBuild]:
        try:
            resp = requests.get(
                EMULATOR_PRS_API,
                params={
                    "state": "open",
                    "per_page": PR_FETCH_LIMIT,
                    "sort": "updated",
                    "direction": "desc",
                },
                timeout=DEFAULT_TIMEOUT,
                headers=GITHUB_API_HEADERS,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise NetworkError(f"Unable to fetch open pull requests: {exc}") from exc
        except ValueError as exc:
            raise NetworkError("GitHub API returned invalid JSON for PR list.") from exc

        if not isinstance(payload, list):
            raise NetworkError("Unexpected PR list payload from GitHub.")

        results: list[PullRequestBuild] = []
        for pr in payload:
            if not isinstance(pr, dict):
                continue
            number = int(pr.get("number", 0) or 0)
            if not number:
                continue
            head = pr.get("head") or {}
            head_sha = str(head.get("sha", "") or "")
            user = pr.get("user") or {}
            artifact = self._fetch_pr_windows_artifact(number)
            results.append(
                PullRequestBuild(
                    number=number,
                    title=str(pr.get("title", "") or "Untitled PR"),
                    author=str(user.get("login", "") or "unknown"),
                    head_sha=head_sha,
                    short_sha=head_sha[:7],
                    pr_url=str(
                        pr.get("html_url", "")
                        or EMULATOR_PR_HTML_URL.format(number=number)
                    ),
                    updated_at=str(pr.get("updated_at", "") or ""),
                    status=artifact.get("status", PR_BUILD_STATUS_MISSING),
                    artifact_label=artifact.get("label"),
                    artifact_url=artifact.get("url"),
                    run_url=artifact.get("run"),
                )
            )

        return results

    def _fetch_pr_windows_artifact(self, pr_number: int) -> dict:
        try:
            resp = requests.get(
                EMULATOR_PR_COMMENTS_API.format(number=pr_number),
                timeout=DEFAULT_TIMEOUT,
                headers=GITHUB_API_HEADERS,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            comments = resp.json()
        except (requests.RequestException, ValueError):
            return {"status": PR_BUILD_STATUS_MISSING}

        if not isinstance(comments, list):
            return {"status": PR_BUILD_STATUS_MISSING}

        # Walk newest -> oldest so the freshest build comment wins, but fall
        # back to "building" if older comments only show in-progress state.
        building_seen = False
        for comment in reversed(comments):
            if not isinstance(comment, dict):
                continue
            body = str(comment.get("body", "") or "")
            if not PR_ARTIFACT_HEADER_RE.search(body):
                continue
            parsed = self._parse_pr_artifact_comment(body)
            if not parsed:
                continue
            if parsed["status"] == PR_BUILD_STATUS_READY:
                return parsed
            if parsed["status"] == PR_BUILD_STATUS_BUILDING:
                building_seen = True
        if building_seen:
            return {"status": PR_BUILD_STATUS_BUILDING}
        return {"status": PR_BUILD_STATUS_MISSING}

    def _parse_pr_artifact_comment(self, body: str) -> Optional[dict]:
        match = PR_WINDOWS_ROW_RE.search(body)
        if not match:
            return None

        artifacts_cell = match.group("artifacts").strip()
        logs_cell = match.group("logs").strip()

        if not artifacts_cell or "building" in artifacts_cell.lower():
            return {"status": PR_BUILD_STATUS_BUILDING}

        links = PR_MD_LINK_RE.findall(artifacts_cell)
        chosen_label: Optional[str] = None
        chosen_url: Optional[str] = None

        # Prefer Clangtron — the only Windows toolchain produced upstream.
        for label, url in links:
            if not self._is_acceptable_pr_artifact_url(url):
                continue
            blob = (label + " " + url).lower()
            if "clangtron" in blob:
                chosen_label, chosen_url = label, url
                break

        # Fall back to any other Windows artifact that isn't a discontinued
        # MSVC/MinGW toolchain build.
        if not chosen_url:
            for label, url in links:
                if not self._is_acceptable_pr_artifact_url(url):
                    continue
                blob = (label + " " + url).lower()
                if "msvc" in blob or "mingw" in blob:
                    continue
                chosen_label, chosen_url = label, url
                break

        if not chosen_url:
            # Build is up but only MSVC/MinGW artifacts are present, which we
            # treat as no usable Windows build.
            return {"status": PR_BUILD_STATUS_MISSING}

        run_match = PR_MD_LINK_RE.search(logs_cell)
        run_url = run_match.group("url") if run_match else None

        return {
            "status": PR_BUILD_STATUS_READY,
            "label": chosen_label or "Windows",
            "url": chosen_url,
            "run": run_url,
        }

    def _is_acceptable_pr_artifact_url(self, url: str) -> bool:
        if not url.lower().endswith(".zip"):
            return False
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return False
        return host.lower() == ALLOWED_PR_ARTIFACT_HOST

    def pr_build_to_release_info(self, pr: PullRequestBuild) -> ReleaseInfo:
        if pr.status != PR_BUILD_STATUS_READY or not pr.artifact_url:
            raise UpdaterError(
                f"PR #{pr.number} does not have a ready Windows Clangtron build yet."
            )
        label_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", pr.artifact_label or "Windows").strip("-") or "Windows"
        asset_name = f"Citron-PR-{pr.number}-{pr.short_sha or 'unknown'}-{label_slug}.zip"
        return ReleaseInfo(
            name=f"PR #{pr.number}: {pr.title}",
            tag_name=f"pr-{pr.number}-{pr.short_sha}" if pr.short_sha else f"pr-{pr.number}",
            published_at=pr.updated_at,
            release_id=pr.number,
            asset_name=asset_name,
            asset_url=pr.artifact_url,
            asset_size=0,
            asset_updated_at=pr.updated_at,
            channel=CHANNEL_PR,
        )

    def _pick_windows_asset(self, assets: list[dict]) -> Optional[dict]:
        scored: list[tuple[int, dict]] = []
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if not name.endswith(".zip"):
                continue

            # Hard reject discontinued toolchains. Upstream stopped producing
            # MSVC and MinGW Windows builds; only Clangtron (Clang LTO) is
            # shipped, so we never select these even if older releases still
            # have the artifacts attached.
            if "msvc" in name or "mingw" in name:
                continue

            score = 0
            if "windows" in name or "win64" in name or "win-" in name:
                score += 6
            elif "win" in name:
                score += 3

            # Clangtron is the only Windows toolchain produced upstream now.
            if "clangtron" in name:
                score += 12
            elif "clang" in name:
                score += 6

            if "x86_64" in name or "x64" in name or "amd64" in name:
                score += 3
            if "citron" in name:
                score += 2
            if "stable" in name:
                score += 2
            if "nightly" in name:
                score += 2

            if "debug" in name or "symbols" in name or "pdb" in name:
                score -= 4
            if "source" in name or name.endswith(("-src.zip", "_src.zip")):
                score -= 10

            if score <= 0:
                continue
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
            "channel": release.channel,
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
                str(release.channel or ""),
                str(release.asset_name or ""),
                str(release.asset_size or ""),
                str(release.asset_updated_at or ""),
            ]
        )

    def _marker_signature(self, marker_data: dict) -> str:
        return "|".join(
            [
                str(marker_data.get("channel") or ""),
                str(marker_data.get("asset_name") or ""),
                str(marker_data.get("asset_size") or ""),
                str(marker_data.get("asset_updated_at") or ""),
            ]
        )
