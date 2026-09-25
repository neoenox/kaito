"""
src/kaito/unzip.py
ZIP/RAR/7zファイルの解凍・圧縮コアロジック (ArchiveServiceへの委譲)
読み取り (ZIP/RAR/7z): 同梱 7z.dll (archive/dll_backend.py)
作成 (平文 ZIP): zipfile / 作成 (暗号化 ZIP・7z): 7z.exe CLI
関連: archive/service.py, archive/dll_backend.py, archive/zip_backend.py, archive/sevenzip_backend.py
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from kaito.archive.service import ArchiveService
from kaito.archive.zip_encoding import get_zip_encodings  # noqa: F401 (再エクスポート)
from kaito.domain.models import (
    ArchiveEntry,
    ExtractionOptions,
    SafetyLimits,
)

# 単一インスタンス (遅延初期化)
_service: Optional[ArchiveService] = None


def _get_service() -> ArchiveService:
    global _service
    if _service is None:
        _service = ArchiveService()
    return _service


# 外部公開型
ZipEntry = ArchiveEntry
ProgressCallback = Callable[[int, int, str], None]

ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z"})


def is_supported(path: str | Path) -> bool:
    """対応アーカイブ形式かを判定"""
    return _get_service().is_supported(path)


def list_archive(
    path: str | Path, _password: Optional[str] = None
) -> tuple[list[ArchiveEntry], bool]:
    """アーカイブの内容一覧を返す

    Returns:
        (entries, is_encrypted)
    """
    info = _get_service().list_archive(path, password=_password)
    return info.entries, info.is_encrypted


def list_entries(zip_path: str | Path) -> tuple[list[ArchiveEntry], bool]:
    """ZIPファイルの内容一覧 (list_archiveのエイリアス)"""
    return list_archive(zip_path)


def extract_archive(
    path: str | Path,
    dest: str | Path,
    password: Optional[str] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """アーカイブを展開する。

    `cancel_event` が指定された場合は、その同一 Event を ArchiveService と
    backend へ渡し、進行中の展開処理までキャンセルを伝播する。
    """

    def cb(current: int, total: int, name: str) -> None:
        if on_progress:
            on_progress(current, total, name)

    limits = SafetyLimits()
    options = ExtractionOptions(
        dest_dir=Path(dest),
        password=password,
        on_progress=cb,
        max_total_size=limits.max_total_size,
        max_file_size=limits.max_single_file_size,
        max_entries=limits.max_entries,
        max_compression_ratio=limits.max_compression_ratio,
    )
    service = (
        ArchiveService(cancel_event=cancel_event)
        if cancel_event is not None
        else _get_service()
    )
    service.extract(path, options)


def extract(
    zip_path: str | Path,
    dest: str | Path,
    password: Optional[str] = None,
    on_progress: Optional[ProgressCallback] = None,
    members: Optional[list[str]] = None,
) -> None:
    """ZIPファイルを展開 (メンバー指定対応)"""
    limits = SafetyLimits()
    options = ExtractionOptions(
        dest_dir=Path(dest),
        password=password,
        members=members,
        on_progress=on_progress,
        max_total_size=limits.max_total_size,
        max_file_size=limits.max_single_file_size,
        max_entries=limits.max_entries,
        max_compression_ratio=limits.max_compression_ratio,
    )
    _get_service().extract(zip_path, options)


def extract_all(
    zip_path: str | Path,
    dest: str | Path,
    password: Optional[str] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> None:
    """全エントリを展開"""
    extract(zip_path, dest, password=password, on_progress=on_progress, members=None)


def create_archive(
    sources: list[Path],
    output: Path,
    on_progress: Optional[ProgressCallback] = None,
    compression_level: int = 1,
    password: Optional[str] = None,
) -> None:
    """アーカイブを作成する。

    `password` が指定された ZIP / 7z は ArchiveService の暗号化作成経路へ
    そのまま渡す。空文字は暗号化なしとして扱う。
    """
    from kaito.domain.models import CompressionOptions

    options = CompressionOptions(
        sources=sources,
        output_path=output,
        compression_level=compression_level,
        password=password or None,
        on_progress=on_progress,
    )
    _get_service().create(options)


def _validate_zip_member(name: str, dest: Path) -> Path:
    """ZIPエントリ名のパストラバーサルチェック (ArchiveServiceに委譲)"""
    from kaito.domain.models import validate_entry_path

    return validate_entry_path(name, dest)
