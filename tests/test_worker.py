"""
tests/test_worker.py
worker.py（ExtractWorker）のテスト
- バッチ解凍の成功
- エラー集計（失敗しても続行）
- キャンセル
- 展開先の決定（二重ネスト防止）
"""

import zipfile
from pathlib import Path
from unittest.mock import patch

from kaito.worker import ExtractResult, ExtractWorker


def _make_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


class TestExtractWorker:
    def test_run_success(self, tmp_path: Path) -> None:
        """正常系: 全アーカイブが展開され、success_countが増える"""
        z1 = tmp_path / "a.zip"
        _make_zip(z1, {"dir1/file1.txt": "data1"})
        z2 = tmp_path / "b.zip"
        _make_zip(z2, {"file2.txt": "data2"})
        dest = tmp_path / "out"

        worker = ExtractWorker([z1, z2], dest)
        result = worker.run()

        assert result.success_count == 2
        assert result.error_count == 0
        assert not result.canceled
        # a.zipは単一ルート(dir1)なのでdest直下に展開される（二重ネスト防止）
        assert (dest / "dir1" / "file1.txt").read_text() == "data1"
        # b.zipはルート直下にファイルがあるので dest/b/ に展開される
        assert (dest / "b" / "file2.txt").read_text() == "data2"
        assert len(result.extracted_dests) == 2

    def test_run_single_root_no_nesting(self, tmp_path: Path) -> None:
        """単一トップレベルディレクトリの場合はdest直下に展開（二重ネスト防止）"""
        z = tmp_path / "proj.zip"
        _make_zip(z, {"myproject/file1.js": "x", "myproject/file2.js": "y"})
        dest = tmp_path / "out"

        worker = ExtractWorker([z], dest)
        result = worker.run()

        assert result.success_count == 1
        assert (dest / "myproject" / "file1.js").read_text() == "x"
        assert result.extracted_dests == [dest]

    def test_run_error_continues(self, tmp_path: Path) -> None:
        """壊れたアーカイブが混ざっても後続は続行し、エラーを集計する"""
        bad = tmp_path / "bad.zip"
        bad.write_text("not a zip")
        z = tmp_path / "ok.zip"
        _make_zip(z, {"file.txt": "data"})
        dest = tmp_path / "out"

        worker = ExtractWorker([bad, z], dest)
        result = worker.run()

        assert result.success_count == 1
        assert result.error_count == 1
        assert result.errors[0].archive_name == "bad.zip"
        assert (dest / "ok" / "file.txt").read_text() == "data"

    def test_run_destination_creation_error_continues(self, tmp_path: Path) -> None:
        """展開先を作れないアーカイブを記録し、後続アーカイブは続行する"""
        first = tmp_path / "first.zip"
        _make_zip(first, {"first.txt": "first"})
        second = tmp_path / "second.zip"
        _make_zip(second, {"second.txt": "second"})

        blocked_parent = tmp_path / "not-a-directory"
        blocked_parent.write_text("file, not a directory")
        dest = tmp_path / "out"

        worker = ExtractWorker([first, second], dest)
        with patch(
            "kaito.worker.resolve_extract_dest",
            side_effect=[blocked_parent / "first", dest / "second"],
        ):
            result = worker.run()

        assert result.success_count == 1
        assert result.error_count == 1
        assert result.errors[0].archive_name == "first.zip"
        assert "not-a-directory" in result.errors[0].message
        assert (dest / "second" / "second.txt").read_text() == "second"
        assert result.extracted_dests == [dest / "second"]

    def test_run_cancel_before_start(self, tmp_path: Path) -> None:
        """開始前にキャンセルされた場合は何も展開しない"""
        z = tmp_path / "a.zip"
        _make_zip(z, {"file.txt": "data"})
        dest = tmp_path / "out"

        worker = ExtractWorker([z], dest)
        worker.cancel()
        result = worker.run()

        assert result.canceled
        assert result.success_count == 0

    def test_run_cancel_mid_batch(self, tmp_path: Path) -> None:
        """展開完了後にキャンセルされた場合は完了分は成功として数える"""
        z1 = tmp_path / "a.zip"
        _make_zip(z1, {"file1.txt": "data1"})
        z2 = tmp_path / "b.zip"
        _make_zip(z2, {"file2.txt": "data2"})
        dest = tmp_path / "out"

        worker = ExtractWorker([z1, z2], dest)

        with patch(
            "kaito.worker.unzip.extract_archive",
            side_effect=lambda *a, **k: (worker.cancel(), None)[1],
        ):
            result = worker.run()

        assert result.canceled
        # 1つ目は展開完了後にキャンセルがセットされたため成功として数えられる
        assert result.success_count == 1

    def test_run_cancel_between_archives(self, tmp_path: Path) -> None:
        """1つ目のアーカイブ完了後、2つ目の開始前にキャンセルされる"""
        z1 = tmp_path / "a.zip"
        _make_zip(z1, {"file1.txt": "data1"})
        z2 = tmp_path / "b.zip"
        _make_zip(z2, {"file2.txt": "data2"})
        dest = tmp_path / "out"

        worker = ExtractWorker([z1, z2], dest)
        call_count = 0

        from kaito import unzip as unzip_module

        real_extract = unzip_module.extract_archive  # 実物を保持

        def real_extract_wrapper(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                real_extract(*args, **kwargs)  # 実物で展開
                worker.cancel()  # 2つ目開始前にキャンセル
            else:
                raise AssertionError("2つ目のアーカイブは処理されないはず")

        with patch(
            "kaito.worker.unzip.extract_archive", side_effect=real_extract_wrapper
        ):
            result = worker.run()

        assert result.canceled
        assert result.success_count == 1
        assert (dest / "a" / "file1.txt").read_text() == "data1"
        assert not (dest / "b").exists()

    def test_password_only_for_active_zip(self, tmp_path: Path) -> None:
        """パスワードはアクティブアーカイブにのみ渡される"""
        z1 = tmp_path / "a.zip"
        _make_zip(z1, {"file.txt": "data"})
        z2 = tmp_path / "b.zip"  # 別パス（active_zip_path ではない）
        _make_zip(z2, {"file2.txt": "data2"})
        dest = tmp_path / "out"

        with patch("kaito.worker.unzip.extract_archive") as mock_extract:
            worker = ExtractWorker(
                [z1, z2],
                dest,
                active_password="secret",
                active_zip_path=z1,
            )
            worker.run()

            # 1つ目(== active_zip_path)はパスワード付き、2つ目(!=)はNone
            first_call = mock_extract.call_args_list[0]
            second_call = mock_extract.call_args_list[1]
            assert first_call.kwargs["password"] == "secret"
            assert second_call.kwargs["password"] is None

    def test_progress_callback(self, tmp_path: Path) -> None:
        """進捗コールバックが(番号, 総数, 名前, 率, 現在, 総数, 名前)で呼ばれる"""
        z = tmp_path / "a.zip"
        _make_zip(z, {"file.txt": "data"})
        dest = tmp_path / "out"
        seen: list[tuple] = []

        worker = ExtractWorker([z], dest, on_progress=lambda *args: seen.append(args))
        worker.run()

        assert seen, "進捗コールバックが呼ばれていない"
        idx, total, name, pct, cur, total_count, cname = seen[-1]
        assert idx == 1
        assert total == 1
        assert name == "a.zip"
        assert pct == 1.0
        assert cur == total_count == 1


class TestExtractResult:
    def test_error_count_property(self) -> None:
        result = ExtractResult(success_count=2, errors=[])
        assert result.error_count == 0
        assert not result.canceled
