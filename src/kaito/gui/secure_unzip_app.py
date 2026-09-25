"""Secure compression GUI entrypoint.

This module subclasses the existing GUI so encrypted-archive creation can be
restored without duplicating or rewriting the rest of the large application
window implementation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

from kaito.archive.service import ArchiveService
from kaito.gui.unzip_app import (
    UnzipApp,
    install_context_menu,
    uninstall_context_menu,
)
from kaito.i18n import set_language, tr
from kaito.settings import SettingsManager
from kaito.unzip import create_archive, is_supported

_ENCRYPTABLE_SUFFIXES = frozenset({".zip", ".7z"})


@dataclass(frozen=True)
class CompressionPasswordChoice:
    """Result of the compression-password prompt."""

    cancelled: bool
    password: str | None


def resolve_compression_password(
    password: str | None,
    confirmation: str | None,
) -> CompressionPasswordChoice:
    """Validate a password/confirmation pair without retaining extra state.

    `None` means the dialog was cancelled. An empty password explicitly means
    "create without encryption". A non-empty password must be confirmed.
    """
    if password is None:
        return CompressionPasswordChoice(cancelled=True, password=None)
    if password == "":
        return CompressionPasswordChoice(cancelled=False, password=None)
    if confirmation is None:
        return CompressionPasswordChoice(cancelled=True, password=None)
    if password != confirmation:
        raise ValueError("圧縮パスワードが一致しません")
    return CompressionPasswordChoice(cancelled=False, password=password)


class SecureCompressionUnzipApp(UnzipApp):
    """Existing kaito GUI with opt-in encrypted ZIP/7z creation."""

    def _prompt_compression_password(self, output: Path) -> CompressionPasswordChoice:
        if output.suffix.lower() not in _ENCRYPTABLE_SUFFIXES:
            return CompressionPasswordChoice(cancelled=False, password=None)

        while True:
            password = simpledialog.askstring(
                "圧縮パスワード",
                "パスワードを入力してください（空欄で暗号化なし）",
                show="*",
                parent=self,
            )
            if password is None or password == "":
                return resolve_compression_password(password, None)

            confirmation = simpledialog.askstring(
                "圧縮パスワード確認",
                "同じパスワードをもう一度入力してください",
                show="*",
                parent=self,
            )
            try:
                return resolve_compression_password(password, confirmation)
            except ValueError as exc:
                messagebox.showerror("圧縮パスワード", str(exc), parent=self)

    def _start_compress_flow(self) -> None:
        """Choose output, optionally collect an encryption password, then compress."""
        if not self._compress_sources:
            return

        if self._compress_no_dialog:
            # Explorer/context-menu compression remains unattended and plaintext.
            # Password entry is available from the interactive GUI flow only.
            first = self._compress_sources[0]
            output = first.parent / (first.stem + ".zip")
            self._start_compress(output, password=None)
            return

        first = self._compress_sources[0]
        default_name = first.stem + ".zip"
        default_dir = str(first.parent) if first.parent != Path() else "."
        output_value = filedialog.asksaveasfilename(
            title=tr("dialog.save_archive"),
            initialdir=default_dir,
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[
                ("ZIP", "*.zip"),
                ("RAR", "*.rar"),
                ("7z", "*.7z"),
            ],
        )
        if not output_value:
            return

        output = Path(output_value)
        choice = self._prompt_compression_password(output)
        if choice.cancelled:
            return

        self._start_compress(output, password=choice.password)

    def _start_compress(self, output: Path, password: str | None = None) -> None:
        """Start compression after the existing self-containment safety check."""
        error = ArchiveService.check_self_contained(self._compress_sources, output)
        if error:
            self._set_status(tr("msg.error_prefix").format(msg=error), kind="error")
            return

        self._compressing = True
        self._set_ui_enabled(False)
        self._progress.set(0)
        self._progress.grid()

        Thread(
            target=self._do_compress,
            args=(list(self._compress_sources), output, password),
            daemon=True,
        ).start()

    def _do_compress(
        self,
        sources: list[Path],
        output: Path,
        password: str | None = None,
    ) -> None:
        try:

            def on_progress(cur: int, total_: int, name: str = "") -> None:
                pct = cur / total_
                self.after(0, lambda p=pct: self._progress.set(p))
                self.after(
                    0,
                    lambda: self._status_var.set(
                        tr("msg.compress_progress").format(
                            pct=f"{pct:.0%}",
                            cur=cur,
                            total=total_,
                            name=name,
                        )
                    ),
                )

            compression_level = self._settings.get("compression_level", 1)
            if (
                not isinstance(compression_level, int)
                or not 0 <= compression_level <= 9
            ):
                compression_level = 1
            create_archive(
                sources,
                output,
                on_progress=on_progress,
                compression_level=compression_level,
                password=password,
            )
            self.after(0, self._on_compress_done)
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self._on_compress_error(msg))


def main() -> None:
    """Run the normal GUI with secure compression controls enabled."""
    args = sys.argv[1:]

    if args and args[0] == "--install-context-menu":
        install_context_menu()
        return
    if args and args[0] == "--uninstall-context-menu":
        uninstall_context_menu()
        return

    settings = SettingsManager()
    set_language(settings.get("language", "ja"))
    ctk.set_appearance_mode(settings.get("theme", "system"))
    ctk.set_default_color_theme("blue")

    cli_path: Path | None = None
    cli_compress_path: Path | None = None

    if args and args[0] == "--compress" and len(args) > 1:
        candidate = Path(args[1])
        if candidate.exists():
            cli_compress_path = candidate
    elif args:
        candidate = Path(args[0])
        if is_supported(candidate) and candidate.exists():
            cli_path = candidate

    app = SecureCompressionUnzipApp(
        cli_path=cli_path,
        cli_compress_path=cli_compress_path,
    )
    app.mainloop()  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    main()
