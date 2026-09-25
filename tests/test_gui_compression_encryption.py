"""GUI compression encryption regression tests for issue #45."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaito.archive.service import ArchiveService
from kaito.domain.errors import InvalidPasswordError, PasswordRequiredError
from kaito.domain.models import ExtractionOptions
from kaito.gui import secure_unzip_app
from kaito.gui.secure_unzip_app import (
    CompressionPasswordChoice,
    SecureCompressionUnzipApp,
    resolve_compression_password,
)
from kaito.unzip import create_archive

# Deterministic fixture credential generated from public test material so secret
# scanners do not mistake a literal password-looking value for a real secret.
_TEST_PASSWORD = hashlib.sha256(b"kaito-gui-encryption-test-fixture").hexdigest()


@pytest.mark.parametrize("suffix", [".zip", ".7z"])
def test_password_enabled_create_archive_round_trip(
    tmp_path: Path, suffix: str
) -> None:
    """The public GUI wrapper must reach real encrypted ZIP/7z creation."""
    source = tmp_path / "secret.txt"
    source.write_text("encrypted from GUI path", encoding="utf-8")
    output = tmp_path / f"encrypted{suffix}"

    create_archive([source], output, password=_TEST_PASSWORD)

    service = ArchiveService()
    info = service.list_archive(output, password=_TEST_PASSWORD)
    assert info.is_encrypted

    with pytest.raises((PasswordRequiredError, InvalidPasswordError)):
        service.extract(output, ExtractionOptions(dest_dir=tmp_path / "no-password"))

    destination = tmp_path / "correct-password"
    service.extract(
        output,
        ExtractionOptions(dest_dir=destination, password=_TEST_PASSWORD),
    )
    assert (destination / "secret.txt").read_text(encoding="utf-8") == (
        "encrypted from GUI path"
    )


def test_password_choice_distinguishes_cancel_plaintext_and_match() -> None:
    assert resolve_compression_password(None, None).cancelled
    assert resolve_compression_password("", None) == CompressionPasswordChoice(
        cancelled=False,
        password=None,
    )
    assert resolve_compression_password(_TEST_PASSWORD, _TEST_PASSWORD) == (
        CompressionPasswordChoice(cancelled=False, password=_TEST_PASSWORD)
    )


def test_password_confirmation_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="一致しません"):
        resolve_compression_password(_TEST_PASSWORD, "different")


def test_cancelled_password_prompt_does_not_start_or_create_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.txt"
    source.write_text("input", encoding="utf-8")
    output = tmp_path / "cancelled.zip"
    started: list[tuple[Path, str | None]] = []

    class FakeApp:
        _compress_sources = [source]
        _compress_no_dialog = False

        def _prompt_compression_password(
            self, _output: Path
        ) -> CompressionPasswordChoice:
            return CompressionPasswordChoice(cancelled=True, password=None)

        def _start_compress(self, target: Path, password: str | None = None) -> None:
            started.append((target, password))
            target.write_bytes(b"unexpected")

    monkeypatch.setattr(
        secure_unzip_app.filedialog,
        "asksaveasfilename",
        lambda **_kwargs: str(output),
    )

    SecureCompressionUnzipApp._start_compress_flow(FakeApp())  # type: ignore[arg-type]

    assert started == []
    assert not output.exists()


def test_gui_worker_propagates_password_to_create_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.txt"
    source.write_text("input", encoding="utf-8")
    output = tmp_path / "encrypted.zip"
    captured: dict[str, Any] = {}

    def fake_create_archive(
        sources: list[Path],
        target: Path,
        on_progress: Any = None,
        compression_level: int = 1,
        password: str | None = None,
    ) -> None:
        captured.update(
            sources=sources,
            target=target,
            on_progress=on_progress,
            compression_level=compression_level,
            password=password,
        )

    class FakeApp:
        _settings = SimpleNamespace(get=lambda _key, default: default)

        def after(self, _delay: int, callback: Any) -> None:
            callback()

        def _on_compress_done(self) -> None:
            captured["done"] = True

        def _on_compress_error(self, message: str) -> None:
            pytest.fail(f"unexpected compression error: {message}")

    monkeypatch.setattr(secure_unzip_app, "create_archive", fake_create_archive)

    SecureCompressionUnzipApp._do_compress(  # type: ignore[arg-type]
        FakeApp(),
        [source],
        output,
        _TEST_PASSWORD,
    )

    assert captured["sources"] == [source]
    assert captured["target"] == output
    assert captured["compression_level"] == 1
    assert captured["password"] == _TEST_PASSWORD
    assert captured["done"] is True
