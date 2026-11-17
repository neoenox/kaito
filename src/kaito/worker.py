"""
src/kaito/worker.py
解凍・圧縮のワーカーロジック（UI非依存）
- バッチ解凍: 複数アーカイブを順に展開
- キャンセル対応: cancel_event で中断
- エラー集計: 失敗したアーカイブを記録して最後に報告
関連: unzip_app.py (GUI), unzip.py (コアロジック)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from kaito import unzip

# 進捗コールバック: (アーカイブ番号, 総数, アーカイブ名, 進捗率0.0-1.0, 現在/総数, 現在のファイル名)
ProgressCallback = Callable[[int, int, str, float, int, int, str], None]


@dataclass
class ExtractError:
    """1アーカイブの展開失敗を表す"""

    archive_name: str
    message: str


@dataclass
class ExtractResult:
    """バッチ解凍の結果"""

    success_count: int = 0
    canceled: bool = False
    errors: list[ExtractError] = field(default_factory=list)
    extracted_dests: list[Path] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)


class ExtractWorker:
    """バッチ解凍をワーカースレッドで実行する

    cancel_event がセットされると、現在のアーカイブの途中でも中断する。
    各アーカイブの失敗は errors に集計し、全体を最後まで継続する。
    """

    def __init__(
        self,
        paths: list[Path],
        dest: Path,
        passwords: dict[Path, str] | None = None,
        active_password: str | None = None,
        active_zip_path: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.paths = list(paths)
        self.dest = dest
        # 新しい方式: パスごとのパスワード辞書
        self.passwords = passwords or {}
        # 後方互換性: 単一のアクティブパスワード
        self.active_password = active_password
        self.active_zip_path = active_zip_path
        self.on_progress = on_progress
        self.cancel_event = threading.Event()
        self._last_progress_time = 0.0

    def cancel(self) -> None:
        """解凍を中断する（ワーカースレッドから安全に呼べる）"""
        self.cancel_event.set()

    def run(self) -> ExtractResult:
        """全アーカイブを順に展開する。結果を返す。"""
        result = ExtractResult()
        total = len(self.paths)

        for idx, archive_path in enumerate(self.paths):
            if self.cancel_event.is_set():
                result.canceled = True
                break

            # パスワード辞書を優先、なければ後方互換用の単一パスワードを使用
            password = self.passwords.get(archive_path)
            if password is None and archive_path == self.active_zip_path:
                password = self.active_password
            entry_archive_name = archive_path.name

            try:
                entries, _ = unzip.list_archive(archive_path)
                archive_dest = resolve_extract_dest(self.dest, archive_path, entries)
            except Exception:
                archive_dest = self.dest / archive_path.stem

            def on_progress(
                current: int,
                total_count: int,
                current_name: str = "",
                _a: str = entry_archive_name,
                _idx: int = idx,
            ) -> None:
                now = time.monotonic()
                if (
                    now - self._last_progress_time < 0.1 and current < total_count
                ):  # pragma: no cover
                    return
                self._last_progress_time = now
                pct = current / total_count
                if self.on_progress is not None:
                    self.on_progress(
                        _idx + 1,
                        total,
                        _a,
                        pct,
                        current,
                        total_count,
                        current_name,
                    )

            try:
                # Destination creation is part of this archive's operation. Keep
                # filesystem failures in the per-archive result so the GUI always
                # receives a completion result and later archives can continue.
                archive_dest.mkdir(parents=True, exist_ok=True)
                unzip.extract_archive(
                    archive_path,
                    archive_dest,
                    password=password,
                    on_progress=on_progress,
                    cancel_event=self.cancel_event,
                )
                # 完了したアーカイブは成功として数える
                result.success_count += 1
                result.extracted_dests.append(archive_dest)
                # キャンセルはアーカイブ単位の境界でも確認（完了済みは数える）
                if self.cancel_event.is_set():
                    result.canceled = True
                    break
            except Exception as exc:  # アーカイブ単位で失敗を集計して続行
                if self.cancel_event.is_set():
                    result.canceled = True
                    break
                result.errors.append(
                    ExtractError(archive_name=entry_archive_name, message=str(exc))
                )

        return result


def resolve_extract_dest(
    dest: Path,
    archive_path: Path,
    entries: list[unzip.ZipEntry],
) -> Path:
    """アーカイブの構成に応じて展開先を決定し、二重ネストを防ぐ

    全エントリが1つのトップレベルディレクトリを共有している場合
    （例: myproject/file1.js, myproject/file2.js）、そのディレクトリ自体が
    コンテナの役割を果たすため dest 直下に展開する。
    共通ルートがない／ルート直下にファイルがある場合は dest/archive_stem/ を作成する。
    """
    roots: set[str] = set()
    has_root_file = False
    for e in entries:
        if "/" in e.name:
            root = e.name.split("/")[0]
            roots.add(root)
        elif e.name:
            has_root_file = True

    if len(roots) == 1 and not has_root_file:
        return dest
    return dest / archive_path.stem
