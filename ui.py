from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

import customtkinter as ctk

from updater import (
    CHANNEL_NIGHTLY,
    CHANNEL_PR,
    CHANNEL_STABLE,
    PR_BUILD_STATUS_BUILDING,
    PR_BUILD_STATUS_MISSING,
    PR_BUILD_STATUS_READY,
    CheckResult,
    PullRequestBuild,
    ReleaseInfo,
    UpdaterError,
    UpdaterService,
)

CHANNEL_LABELS = {
    CHANNEL_STABLE: "Stable (citron-neo/emulator)",
    CHANNEL_NIGHTLY: "Nightly CI - Clangtron (citron-neo/CI)",
    CHANNEL_PR: "PR Builds (citron-neo/emulator)",
}
# Order matters for the dropdown: stable, nightly, PR.
CHANNEL_ORDER = (CHANNEL_STABLE, CHANNEL_NIGHTLY, CHANNEL_PR)
LABEL_TO_CHANNEL = {label: key for key, label in CHANNEL_LABELS.items()}

PR_PLACEHOLDER_LABEL = "(no PRs loaded)"
PR_STATUS_TEXT = {
    PR_BUILD_STATUS_READY: "Ready - Clangtron Windows build available",
    PR_BUILD_STATUS_BUILDING: "Building - check back once CI finishes",
    PR_BUILD_STATUS_MISSING: "No usable Windows build for this PR",
}


class UpdaterApp:
    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.service = UpdaterService()
        self.current_release: Optional[ReleaseInfo] = None
        self.busy = False
        self._startup_check_done = False
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.pr_builds: list[PullRequestBuild] = []
        self.pr_label_lookup: dict[str, PullRequestBuild] = {}
        self.selected_pr: Optional[PullRequestBuild] = None

        self.root = ctk.CTk()
        self.root.title("Citron Neo Updater")
        self.root.geometry("900x640")
        self.root.minsize(860, 600)

        self._build_ui()
        self._load_initial_values()
        self._schedule_queue_pump()
        self._maybe_show_first_run_setup()

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(4, weight=1)

        title = ctk.CTkLabel(
            self.root,
            text="Citron Neo Updater",
            font=ctk.CTkFont(size=28, weight="bold"),
        )
        title.grid(row=0, column=0, padx=20, pady=(18, 10), sticky="w")

        version_frame = ctk.CTkFrame(self.root)
        version_frame.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        version_frame.grid_columnconfigure((0, 1), weight=1)
        version_frame.grid_columnconfigure(2, weight=0)

        self.current_version_var = ctk.StringVar(value="Current: Unknown")
        self.latest_version_var = ctk.StringVar(value="Latest: Unknown")
        self.status_var = ctk.StringVar(value="Status: Ready")

        ctk.CTkLabel(
            version_frame,
            textvariable=self.current_version_var,
            font=ctk.CTkFont(size=15),
        ).grid(row=0, column=0, padx=12, pady=10, sticky="w")

        ctk.CTkLabel(
            version_frame,
            textvariable=self.latest_version_var,
            font=ctk.CTkFont(size=15),
        ).grid(row=0, column=1, padx=12, pady=10, sticky="w")

        self.channel_var = ctk.StringVar(value=CHANNEL_LABELS[CHANNEL_NIGHTLY])
        self.channel_menu = ctk.CTkOptionMenu(
            version_frame,
            variable=self.channel_var,
            values=[CHANNEL_LABELS[c] for c in CHANNEL_ORDER],
            command=self._on_channel_changed,
            width=260,
        )
        self.channel_menu.grid(row=0, column=2, padx=12, pady=10, sticky="e")

        self.pr_frame = ctk.CTkFrame(self.root)
        self.pr_frame.grid(row=2, column=0, padx=20, pady=8, sticky="ew")
        self.pr_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.pr_frame,
            text="PR Build:",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")

        self.pr_var = ctk.StringVar(value=PR_PLACEHOLDER_LABEL)
        self.pr_menu = ctk.CTkOptionMenu(
            self.pr_frame,
            variable=self.pr_var,
            values=[PR_PLACEHOLDER_LABEL],
            command=self._on_pr_selected,
        )
        self.pr_menu.grid(row=0, column=1, padx=8, pady=(10, 4), sticky="ew")
        self.pr_menu.configure(state="disabled")

        self.pr_open_btn = ctk.CTkButton(
            self.pr_frame,
            text="Open PR on GitHub",
            width=160,
            command=self._open_selected_pr,
        )
        self.pr_open_btn.grid(row=0, column=2, padx=(8, 12), pady=(10, 4), sticky="e")
        self.pr_open_btn.configure(state="disabled")

        self.pr_status_var = ctk.StringVar(value="Switch to PR Builds and check for updates to populate this list.")
        ctk.CTkLabel(
            self.pr_frame,
            textvariable=self.pr_status_var,
            text_color="#bdbdbd",
            font=ctk.CTkFont(size=13),
            anchor="w",
            justify="left",
            wraplength=820,
        ).grid(row=1, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew")

        # Hidden by default; toggled when the PR channel is selected.
        self.pr_frame.grid_remove()

        controls_frame = ctk.CTkFrame(self.root)
        controls_frame.grid(row=3, column=0, padx=20, pady=8, sticky="ew")
        controls_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        self.check_btn = ctk.CTkButton(
            controls_frame,
            text="Check for Updates",
            command=self.check_updates,
        )
        self.check_btn.grid(row=0, column=0, padx=8, pady=12, sticky="ew")

        self.update_btn = ctk.CTkButton(
            controls_frame,
            text="Update Now",
            command=self.update_now,
            state="disabled",
        )
        self.update_btn.grid(row=0, column=1, padx=8, pady=12, sticky="ew")

        self.launch_btn = ctk.CTkButton(
            controls_frame,
            text="Launch Citron Neo",
            command=self.launch_citron,
        )
        self.launch_btn.grid(row=0, column=2, padx=8, pady=12, sticky="ew")

        self.browse_btn = ctk.CTkButton(
            controls_frame,
            text="Change Install Path",
            command=self.change_install_path,
        )
        self.browse_btn.grid(row=0, column=3, padx=8, pady=12, sticky="ew")

        self.import_btn = ctk.CTkButton(
            controls_frame,
            text="Import Portable User Folder",
            command=self.import_portable_user_folder,
        )
        self.import_btn.grid(row=0, column=4, padx=8, pady=12, sticky="ew")

        progress_frame = ctk.CTkFrame(self.root)
        progress_frame.grid(row=4, column=0, padx=20, pady=(8, 6), sticky="nsew")
        progress_frame.grid_columnconfigure(0, weight=1)
        progress_frame.grid_rowconfigure(2, weight=1)

        self.install_path_var = ctk.StringVar(value="")
        ctk.CTkLabel(progress_frame, text="Install Path:").grid(
            row=0, column=0, padx=12, pady=(12, 2), sticky="w"
        )
        ctk.CTkLabel(
            progress_frame,
            textvariable=self.install_path_var,
            font=ctk.CTkFont(size=13),
            text_color="#bdbdbd",
        ).grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")

        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="ew")

        self.status_label = ctk.CTkLabel(
            progress_frame,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=14),
        )
        self.status_label.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(
            progress_frame,
            text="Logs",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=4, column=0, padx=12, pady=(6, 4), sticky="w")

        self.log_box = ctk.CTkTextbox(progress_frame, wrap="word")
        self.log_box.grid(row=5, column=0, padx=12, pady=(0, 12), sticky="nsew")
        progress_frame.grid_rowconfigure(5, weight=1)

    def _load_initial_values(self) -> None:
        install_path = self.service.get_install_path()
        self.install_path_var.set(str(install_path))
        preferred = self.service.get_preferred_channel()
        self.channel_var.set(CHANNEL_LABELS.get(preferred, CHANNEL_LABELS[CHANNEL_NIGHTLY]))
        self._set_pr_panel_visible(preferred == CHANNEL_PR)
        if preferred == CHANNEL_PR:
            self.pr_status_var.set("Loading open PRs from citron-neo/emulator...")
        self.log("Updater started.")

    def _maybe_show_first_run_setup(self) -> None:
        if self.service.has_completed_install_prompt():
            self._startup_check_done = True
            self.check_updates()
            return
        self._show_first_run_setup_popup()

    def _show_first_run_setup_popup(self) -> None:
        popup = ctk.CTkToplevel(self.root)
        popup.title("Initial Setup")
        popup.geometry("700x360")
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.grab_set()

        popup.grid_columnconfigure(0, weight=1)

        install_path_var = ctk.StringVar(value=str(self.service.get_install_path()))
        import_var = ctk.BooleanVar(value=False)
        source_var = ctk.StringVar(value="")

        ctk.CTkLabel(
            popup,
            text="Choose where Citron Neo should be installed/updated",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(18, 10), sticky="w")

        path_frame = ctk.CTkFrame(popup)
        path_frame.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        path_frame.grid_columnconfigure(0, weight=1)

        path_entry = ctk.CTkEntry(path_frame, textvariable=install_path_var)
        path_entry.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(
            path_frame,
            text="Browse",
            width=100,
            command=lambda: self._setup_pick_install_path(install_path_var),
        ).grid(row=0, column=1, padx=(0, 10), pady=10)

        import_frame = ctk.CTkFrame(popup)
        import_frame.grid(row=2, column=0, padx=20, pady=8, sticky="ew")
        import_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            import_frame,
            text="Import settings/saves from prior portable install (copy old 'user' folder)",
            variable=import_var,
            onvalue=True,
            offvalue=False,
        ).grid(row=0, column=0, padx=10, pady=(10, 6), sticky="w")

        source_entry = ctk.CTkEntry(import_frame, textvariable=source_var)
        source_entry.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")

        ctk.CTkButton(
            import_frame,
            text="Select Portable Source",
            width=180,
            command=lambda: self._setup_pick_source_path(source_var),
        ).grid(row=1, column=1, padx=(0, 10), pady=(0, 10))

        ctk.CTkLabel(
            popup,
            text="Tip: If you used portable mode, pick the old folder that contains 'user'.",
            text_color="#bdbdbd",
            font=ctk.CTkFont(size=13),
        ).grid(row=3, column=0, padx=20, pady=(2, 8), sticky="w")

        ctk.CTkButton(
            popup,
            text="Save and Continue",
            command=lambda: self._complete_setup(
                popup=popup,
                install_path=install_path_var.get().strip(),
                do_import=bool(import_var.get()),
                import_source=source_var.get().strip(),
            ),
        ).grid(row=4, column=0, padx=20, pady=(6, 16), sticky="e")

        popup.protocol("WM_DELETE_WINDOW", lambda: None)

    def _schedule_queue_pump(self) -> None:
        self._drain_ui_queue()
        self.root.after(60, self._schedule_queue_pump)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                cb = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            cb()

    def _run_background(self, task: Callable[[], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def runner() -> None:
            try:
                task()
            finally:
                self.ui_queue.put(lambda: self._set_busy(False))

        threading.Thread(target=runner, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        button_state = "disabled" if busy else "normal"
        self.check_btn.configure(state=button_state)
        self.browse_btn.configure(state=button_state)
        self.launch_btn.configure(state=button_state)
        self.import_btn.configure(state=button_state)
        self.channel_menu.configure(state=button_state)
        self.update_btn.configure(state=button_state if self.current_release else "disabled")
        pr_menu_state = button_state if self.pr_builds and not busy else "disabled"
        self.pr_menu.configure(state=pr_menu_state)
        self.pr_open_btn.configure(state=button_state if self.selected_pr else "disabled")

    def _progress_cb(self, value: float, status: str) -> None:
        self.ui_queue.put(lambda: self.progress_bar.set(max(0.0, min(1.0, value))))
        self.ui_queue.put(lambda: self.status_var.set(f"Status: {status}"))
        self.ui_queue.put(lambda: self.log(status))

    def log(self, message: str) -> None:
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")

    def check_updates(self) -> None:
        def task() -> None:
            self.ui_queue.put(lambda: self.status_var.set("Status: Checking for updates..."))
            channel = self.service.get_preferred_channel()
            channel_upper = channel.upper()
            if channel == CHANNEL_PR:
                msg = "Scanning open PRs on citron-neo/emulator for Windows Clangtron builds..."
            else:
                msg = f"Checking GitHub release (channel: {channel_upper})..."
            self.ui_queue.put(lambda m=msg: self.log(m))
            self.ui_queue.put(lambda: self.progress_bar.set(0))
            try:
                result = self.service.check_for_updates()
            except Exception as exc:
                self.ui_queue.put(lambda e=exc: self._handle_error("Update check failed", e))
                return

            self.ui_queue.put(lambda r=result: self._apply_check_result(r))

        self._run_background(task)

    def _apply_check_result(self, result: CheckResult) -> None:
        self.current_version_var.set(f"Current: {result.current_version}")
        self.latest_version_var.set(f"Latest: {result.latest_version}")

        channel = self.service.get_preferred_channel()

        if channel == CHANNEL_PR:
            self.current_release = None
            self._populate_pr_dropdown(result.pull_requests)
            return

        self.current_release = result.release
        if result.update_available and result.release:
            self.status_var.set("Status: Update available")
            self.update_btn.configure(state="normal")
            self.log(
                f"Update available: {result.current_version} -> {result.latest_version} "
                f"({result.release.asset_name})"
            )
        else:
            self.status_var.set("Status: You are up to date")
            self.update_btn.configure(state="disabled")
            self.log("No update needed.")

    def _populate_pr_dropdown(self, prs: list[PullRequestBuild]) -> None:
        self.pr_builds = list(prs or [])
        self.selected_pr = None
        self.current_release = None
        self.pr_label_lookup = {}
        labels: list[str] = []

        ready_count = 0
        building_count = 0
        for pr in self.pr_builds:
            label = pr.display_label
            # Disambiguate duplicates that share the same display string.
            base = label
            n = 2
            while label in self.pr_label_lookup:
                label = f"{base} #{n}"
                n += 1
            self.pr_label_lookup[label] = pr
            labels.append(label)
            if pr.status == PR_BUILD_STATUS_READY:
                ready_count += 1
            elif pr.status == PR_BUILD_STATUS_BUILDING:
                building_count += 1

        if not labels:
            labels = [PR_PLACEHOLDER_LABEL]
            self.pr_var.set(PR_PLACEHOLDER_LABEL)
            self.pr_menu.configure(values=labels, state="disabled")
            self.pr_status_var.set(
                "No open PRs were returned by GitHub. Check your network or try again later."
            )
            self.pr_open_btn.configure(state="disabled")
            self.update_btn.configure(state="disabled")
            self.status_var.set("Status: No PR builds available")
            self.log("No open PRs returned for the PR channel.")
            return

        self.pr_menu.configure(values=labels, state="normal")

        first_ready_label = next(
            (lbl for lbl, pr in self.pr_label_lookup.items() if pr.status == PR_BUILD_STATUS_READY),
            None,
        )
        default_label = first_ready_label or labels[0]
        self.pr_var.set(default_label)
        self._on_pr_selected(default_label)

        summary = (
            f"Loaded {len(self.pr_builds)} open PR(s) - "
            f"{ready_count} ready, {building_count} building, "
            f"{len(self.pr_builds) - ready_count - building_count} without Windows builds."
        )
        self.log(summary)
        self.status_var.set(
            f"Status: {ready_count} PR build(s) ready"
            if ready_count
            else "Status: No PR Windows builds ready yet"
        )

    def _on_pr_selected(self, selected_label: str) -> None:
        pr = self.pr_label_lookup.get(selected_label)
        self.selected_pr = pr
        if not pr:
            self.current_release = None
            self.update_btn.configure(state="disabled")
            self.pr_open_btn.configure(state="disabled")
            self.pr_status_var.set("Select a PR to see its build status.")
            return

        self.pr_open_btn.configure(state="disabled" if self.busy else "normal")
        commit_part = f"@ {pr.short_sha}" if pr.short_sha else ""
        author_part = f"by {pr.author}" if pr.author else ""
        meta = " ".join(part for part in (commit_part, author_part) if part)
        status_text = PR_STATUS_TEXT.get(pr.status, pr.status)
        self.pr_status_var.set(f"PR #{pr.number} {meta} - {status_text}")

        if pr.status != PR_BUILD_STATUS_READY:
            self.current_release = None
            self.update_btn.configure(state="disabled")
            return

        try:
            release = self.service.pr_build_to_release_info(pr)
        except UpdaterError as exc:
            self.current_release = None
            self.update_btn.configure(state="disabled")
            self.pr_status_var.set(f"PR #{pr.number}: {exc}")
            return

        self.current_release = release
        self.latest_version_var.set(f"Latest: PR #{pr.number} ({pr.short_sha or 'unknown'})")
        self.update_btn.configure(state="disabled" if self.busy else "normal")

    def _open_selected_pr(self) -> None:
        if not self.selected_pr:
            return
        url = self.selected_pr.pr_url
        if not url:
            return
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            self._handle_error("Could not open PR in browser", exc)

    def _set_pr_panel_visible(self, visible: bool) -> None:
        if visible:
            self.pr_frame.grid()
        else:
            self.pr_frame.grid_remove()
            self.pr_builds = []
            self.pr_label_lookup = {}
            self.selected_pr = None
            self.pr_var.set(PR_PLACEHOLDER_LABEL)
            self.pr_menu.configure(values=[PR_PLACEHOLDER_LABEL], state="disabled")
            self.pr_open_btn.configure(state="disabled")

    def _on_channel_changed(self, selected_label: str) -> None:
        channel = LABEL_TO_CHANNEL.get(selected_label, CHANNEL_NIGHTLY)
        try:
            self.service.set_preferred_channel(channel)
        except Exception as exc:
            self._handle_error("Channel setting failed", exc)
            return
        self._set_pr_panel_visible(channel == CHANNEL_PR)
        if channel == CHANNEL_PR:
            self.pr_status_var.set("Loading open PRs from citron-neo/emulator...")
        else:
            self.pr_status_var.set("Switch to PR Builds and check for updates to populate this list.")
        self.log(f"Release channel set to {channel.upper()}.")
        self.status_var.set(f"Status: Release channel: {channel.upper()}")
        # Refresh release lookup to switch source repository immediately.
        if not self.busy and self._startup_check_done:
            self.check_updates()

    def update_now(self) -> None:
        if not self.current_release:
            messagebox.showinfo("No Update", "Check for updates first.")
            return

        install_path = Path(self.install_path_var.get())

        def task() -> None:
            self.ui_queue.put(lambda: self.log("Starting update..."))
            self.ui_queue.put(lambda: self.progress_bar.set(0))
            self.ui_queue.put(lambda: self.status_var.set("Status: Updating..."))

            try:
                self.service.run_full_update(
                    release=self.current_release,
                    install_path=install_path,
                    progress_cb=self._progress_cb,
                )
            except Exception as exc:
                self.ui_queue.put(lambda e=exc: self._handle_error("Update failed", e))
                return

            def success() -> None:
                self.progress_bar.set(1.0)
                self.status_var.set("Status: Update complete")
                self.current_version_var.set(f"Current: {self.current_release.tag_name}")
                self.log("Update completed successfully.")
                messagebox.showinfo("Update Complete", "Citron Neo has been updated successfully.")

            self.ui_queue.put(success)

        self._run_background(task)

    def launch_citron(self) -> None:
        install_path = Path(self.install_path_var.get())
        try:
            self.service.launch_citron(install_path)
            self.status_var.set("Status: Citron Neo launched")
            self.log("Launched Citron Neo.")
        except Exception as exc:
            self._handle_error("Launch failed", exc)

    def change_install_path(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Citron Neo installation folder",
            initialdir=self.install_path_var.get() or str(Path.home()),
        )
        if not chosen:
            return

        self.service.set_install_path(chosen)
        self.install_path_var.set(chosen)
        self.log(f"Install path set to: {chosen}")
        self.status_var.set("Status: Install path updated")

    def import_portable_user_folder(self) -> None:
        source = filedialog.askdirectory(
            title="Select old portable Citron folder (contains 'user')",
            initialdir=self.install_path_var.get() or str(Path.home()),
        )
        if not source:
            return

        install_path = Path(self.install_path_var.get())
        try:
            copied = self.service.import_portable_user_folder(Path(source), install_path)
        except Exception as exc:
            self._handle_error("Import failed", exc)
            return

        self.log(f"Imported {copied} file(s) from portable user folder.")
        self.status_var.set("Status: Portable user data imported")
        messagebox.showinfo(
            "Import Complete",
            f"Imported {copied} file(s) from the portable 'user' folder.",
        )

    def _setup_pick_install_path(self, install_path_var: ctk.StringVar) -> None:
        selected = filedialog.askdirectory(
            title="Select Citron Neo install/update folder",
            initialdir=install_path_var.get() or str(Path.home()),
        )
        if selected:
            install_path_var.set(selected)

    def _setup_pick_source_path(self, source_var: ctk.StringVar) -> None:
        selected = filedialog.askdirectory(
            title="Select old portable folder (contains 'user')",
            initialdir=source_var.get() or str(Path.home()),
        )
        if selected:
            source_var.set(selected)

    def _complete_setup(
        self,
        popup: ctk.CTkToplevel,
        install_path: str,
        do_import: bool,
        import_source: str,
    ) -> None:
        if not install_path:
            messagebox.showerror("Setup", "Please choose an install path.")
            return

        self.service.set_install_path(install_path)
        self.service.mark_install_prompt_completed()
        self.install_path_var.set(install_path)
        self.log(f"Install path set to: {install_path}")

        if do_import:
            if not import_source:
                messagebox.showerror(
                    "Setup",
                    "Import is enabled but no source folder was selected.",
                )
                return
            try:
                copied = self.service.import_portable_user_folder(
                    Path(import_source), Path(install_path)
                )
            except Exception as exc:
                self._handle_error("Portable import failed", exc)
                return
            self.log(f"Imported {copied} file(s) from prior portable 'user' folder.")

        popup.grab_release()
        popup.destroy()

        if not self._startup_check_done:
            self._startup_check_done = True
            self.check_updates()

    def _handle_error(self, title: str, exc: Exception) -> None:
        msg = str(exc)
        if isinstance(exc, UpdaterError):
            detail = msg
        else:
            detail = f"{msg} (unexpected error)"
        self.status_var.set(f"Status: {title}")
        self.log(f"{title}: {detail}")
        messagebox.showerror(title, detail)
