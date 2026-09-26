"""
tests/test_unzip_app.py
unzip_app.py のテスト（GUIコンポーネントは全てmock）
"""

from contextlib import ExitStack
from datetime import datetime
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kaito.gui import theme
from kaito.gui.unzip_app import (
    _decode_text,
    _format_size,
    _read_archive_entry,
    _truncate_path,
    main as app_main,
)
from kaito.worker import ExtractResult, ExtractWorker, resolve_extract_dest


# ---- _format_size のテスト ----


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(0) == "0 B"
        assert _format_size(512) == "512 B"
        assert _format_size(1023) == "1023 B"

    def test_kilobytes(self) -> None:
        assert _format_size(1024) == "1.0 KB"
        assert _format_size(1536) == "1.5 KB"
        assert _format_size(1024 * 1024 - 1) == "1024.0 KB"

    def test_megabytes(self) -> None:
        assert _format_size(1024 * 1024) == "1.0 MB"
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"
        assert _format_size(1024 * 1024 * 1024 - 1) == "1024.0 MB"

    def test_gigabytes(self) -> None:
        assert _format_size(1024**3) == "1.0 GB"
        assert _format_size(3 * 1024**3) == "3.0 GB"


# ---- ツリー用アイコンのテスト ----


class TestEntryIcons:
    """フォルダ/ファイルアイコンの描画（ヘッドレスで検証）"""

    @pytest.fixture
    def app(self) -> MagicMock:
        return _make_app_mock()

    def test_draw_folder_icon_shape(self) -> None:
        from kaito.gui.unzip_app import _draw_folder_icon

        img = _draw_folder_icon(False)
        assert img.mode == "RGBA"
        assert img.size == (16, 16)
        # 透明ではないピクセル（描画内容）が存在する
        assert img.getchannel("A").getextrema()[1] > 0

    def test_draw_file_icon_shape(self) -> None:
        from kaito.gui.unzip_app import _draw_file_icon

        img = _draw_file_icon(True)
        assert img.mode == "RGBA"
        assert img.size == (16, 16)
        assert img.getchannel("A").getextrema()[1] > 0

    def test_icons_differ_by_theme(self) -> None:
        """ライト/ダークで配色が切り替わる"""
        from kaito.gui.unzip_app import _draw_file_icon, _draw_folder_icon

        assert _draw_folder_icon(False).tobytes() != _draw_folder_icon(True).tobytes()
        assert _draw_file_icon(False).tobytes() != _draw_file_icon(True).tobytes()

    def test_ensure_icons_fallback(self, app: MagicMock) -> None:
        """アイコン生成に失敗してもクラッシュせず None にフォールバック"""
        from kaito.gui.unzip_app import UnzipApp

        with patch(
            "kaito.gui.unzip_app._make_entry_icons",
            side_effect=RuntimeError("no display"),
        ):
            UnzipApp._ensure_icons(app, False)
        assert app._icon_folder is None
        assert app._icon_file is None

    def test_refresh_tree_uses_entry_icons(self, app: MagicMock) -> None:
        """フォルダ行とファイル行で異なるアイコンが #0 列に設定される"""
        from datetime import datetime
        from kaito.unzip import ZipEntry

        app._entries = [
            ZipEntry("a.txt", 100, 80, datetime(2026, 6, 2, 10, 0, 0), False),
            ZipEntry("dir/", 0, 0, datetime(2026, 1, 1, 0, 0, 0), True),
        ]
        app._tree.get_children.return_value = []
        app._icon_folder = "FOLDER"
        app._icon_file = "FILE"
        app._refresh_tree()
        images = [c.kwargs.get("image") for c in app._tree.insert.call_args_list]
        assert images == ["FILE", "FOLDER"]


# ---- main() のテスト ----


class TestMain:
    def test_main_no_args(self) -> None:
        with (
            patch("sys.argv", ["kaito"]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode") as mode,
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme") as theme,
            patch("kaito.gui.unzip_app.UnzipApp") as app,
            patch("kaito.gui.unzip_app.SettingsManager.get", return_value="system"),
        ):
            app_main()
            mode.assert_called_once_with("system")
            theme.assert_called_once_with("blue")
            app.assert_called_once_with(cli_path=None, cli_compress_path=None)

    def test_main_with_zip(self, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        z.write_text("dummy")
        with (
            patch("sys.argv", ["kaito", str(z)]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
        ):
            app_main()
            assert app.call_args.kwargs["cli_path"].name == "test.zip"

    def test_main_non_zip_arg(self) -> None:
        with (
            patch("sys.argv", ["kaito", "readme.txt"]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
        ):
            app_main()
            app.assert_called_once_with(cli_path=None, cli_compress_path=None)

    def test_main_nonexistent_arg(self) -> None:
        with (
            patch("sys.argv", ["kaito", "no.zip"]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
        ):
            app_main()
            app.assert_called_once_with(cli_path=None, cli_compress_path=None)

    def test_main_with_rar(self, tmp_path: Path) -> None:
        r = tmp_path / "test.rar"
        r.write_text("dummy")
        with (
            patch("sys.argv", ["kaito", str(r)]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
            patch("kaito.gui.unzip_app.SettingsManager.get", return_value="system"),
        ):
            app_main()
            assert app.call_args.kwargs["cli_path"].name == "test.rar"

    def test_main_with_7z(self, tmp_path: Path) -> None:
        s = tmp_path / "test.7z"
        s.write_text("dummy")
        with (
            patch("sys.argv", ["kaito", str(s)]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
            patch("kaito.gui.unzip_app.SettingsManager.get", return_value="system"),
        ):
            app_main()
            assert app.call_args.kwargs["cli_path"].name == "test.7z"


# ---- UnzipApp の全メソッドテスト ----


def _make_app_mock() -> MagicMock:
    """__init__ を呼ばずにモックしたUnzipAppインスタンスを作成"""
    from kaito.gui.unzip_app import UnzipApp

    app = UnzipApp.__new__(UnzipApp)

    # __init__ で設定されるインスタンス変数
    app._zip_path = None
    app._archive_queue = []  # list[tuple[Path, bool]]
    app._entries = []
    app._is_encrypted = False
    app._extracting = False
    app._path_var = MagicMock()
    app._dest_var = MagicMock()
    app._status_var = MagicMock()
    app._status_label = MagicMock()
    app._progress = MagicMock()
    app._tree = MagicMock()
    app._browse_btn = MagicMock()
    app._dest_btn = MagicMock()
    app._extract_btn = MagicMock()
    app._compress_btn = MagicMock()
    app._cancel_btn = MagicMock()
    app._drop_frame = MagicMock()
    app._list_frame = MagicMock()
    app._settings = MagicMock()
    app._settings.get_password.return_value = None
    app._open_on_done_var = MagicMock()
    app._open_on_done_var.get.return_value = False
    app._close_on_done_var = MagicMock()
    app._close_on_done_var.get.return_value = False
    app._theme_var = MagicMock()
    app._theme_menu = MagicMock()
    app._recent_var = MagicMock()
    app._recent_menu = MagicMock()
    app._preview_frame = MagicMock()
    app._preview_label = MagicMock()
    app._temp_dir = None
    app._tree_poll_id = None
    app._tree_last_dark = None
    app._search_var = MagicMock()
    app._search_var.get.return_value = ""
    app._search_entry = MagicMock()
    app._settings_btn = MagicMock()
    app._compress_sources = []
    app._compressing = False
    app._compress_no_dialog = False
    app._extracted_dests = []
    app._worker = None
    app._icon_folder = None  # ツリー用アイコン（実UIでは _ensure_icons が生成）
    app._icon_file = None
    app._archive_service = MagicMock()  # ArchiveService（プレビュー等）
    app._passwords = {}
    app._failed_passwords = set()
    app.after = MagicMock()
    app.drop_target_register = MagicMock()
    app.dnd_bind = MagicMock()
    app.title = MagicMock()
    app.geometry = MagicMock()
    app.minsize = MagicMock()
    app.grid_columnconfigure = MagicMock()
    app.grid_rowconfigure = MagicMock()
    return app


def _init_patches() -> list:
    """__init__ テスト用の共通パッチリストを返す"""
    from kaito.gui.unzip_app import UnzipApp

    return [
        patch.object(UnzipApp, "_build_ui"),
        patch.object(UnzipApp, "drop_target_register"),
        patch.object(UnzipApp, "dnd_bind"),
        patch.object(UnzipApp, "title"),
        patch.object(UnzipApp, "geometry"),
        patch.object(UnzipApp, "minsize"),
        patch.object(UnzipApp, "grid_columnconfigure"),
        patch.object(UnzipApp, "grid_rowconfigure"),
        patch.object(UnzipApp, "TkdndVersion", create=True),
        patch("kaito.gui.unzip_app.TkinterDnD._require"),
        patch.object(UnzipApp, "_open_on_done_var", create=True),
        patch.object(UnzipApp, "_close_on_done_var", create=True),
        patch.object(UnzipApp, "_recent_menu", create=True),
        patch.object(UnzipApp, "_recent_var", create=True),
        patch.object(UnzipApp, "_apply_tree_style"),
        patch.object(UnzipApp, "_start_theme_poll"),
        patch.object(UnzipApp, "_dest_var", create=True),
        patch.object(UnzipApp, "_search_var", create=True),
        patch.object(UnzipApp, "_settings_btn", create=True),
        patch("kaito.gui.unzip_app.ArchiveService"),
        patch("kaito.gui.unzip_app.SafetyLimits"),
    ]


class TestUnzipAppInit:
    """__init__ のテスト（cli_path の有無）"""

    def test_init_no_path(self) -> None:
        from kaito.gui.unzip_app import UnzipApp

        with ExitStack() as stack:
            for p in _init_patches():
                stack.enter_context(p)
            app = UnzipApp(cli_path=None)
            assert app._zip_path is None
            assert app._entries == []
            assert not app._is_encrypted
            assert not app._extracting

    def test_init_with_path(self, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")

        from kaito.gui.unzip_app import UnzipApp

        extra = [
            patch.object(UnzipApp, "_path_var", create=True),
            patch.object(UnzipApp, "_dest_var", create=True),
            patch.object(UnzipApp, "_status_var", create=True),
            patch.object(UnzipApp, "_tree", create=True),
            patch.object(UnzipApp, "_refresh_tree"),
            patch.object(UnzipApp, "_drop_frame", create=True),
            patch.object(UnzipApp, "_list_frame", create=True),
            patch.object(UnzipApp, "_extract_btn", create=True),
            patch.object(UnzipApp, "_compress_btn", create=True),
            patch("customtkinter.CTk.__init__", return_value=None),
        ]
        with ExitStack() as stack:
            for p in _init_patches() + extra:
                stack.enter_context(p)
            app = UnzipApp(cli_path=z)
            assert app._zip_path == z
            assert len(app._entries) == 1

    def test_init_restores_saved_dest(self) -> None:
        """過去の展開先は復元しない（常にアーカイブ名ベース）"""
        from kaito.gui.unzip_app import SettingsManager, UnzipApp

        with ExitStack() as stack:
            for p in _init_patches():
                stack.enter_context(p)
            stack.enter_context(patch.object(UnzipApp, "_path_var", create=True))
            dest_var = stack.enter_context(
                patch.object(UnzipApp, "_dest_var", create=True)
            )
            stack.enter_context(patch.object(UnzipApp, "_status_var", create=True))
            stack.enter_context(patch.object(UnzipApp, "_tree", create=True))
            stack.enter_context(patch.object(UnzipApp, "_refresh_tree"))
            stack.enter_context(patch.object(UnzipApp, "_drop_frame", create=True))
            stack.enter_context(patch.object(UnzipApp, "_list_frame", create=True))
            stack.enter_context(patch.object(UnzipApp, "_extract_btn", create=True))
            stack.enter_context(patch.object(UnzipApp, "_compress_btn", create=True))
            stack.enter_context(patch("customtkinter.CTk.__init__", return_value=None))
            stack.enter_context(
                patch.object(
                    SettingsManager,
                    "get",
                    side_effect=lambda key, default=None: (
                        "C:\\saved\\path" if key == "last_dest" else default
                    ),
                )
            )
            UnzipApp(cli_path=None)
            # 過去の展開先を復元しなくなった
            dest_var.set.assert_not_called()


class TestUnzipAppTheme:
    """テーマ関連メソッドのテスト"""

    def test_resolve_mode_light(self) -> None:
        from kaito.gui.unzip_app import UnzipApp

        with patch("kaito.gui.unzip_app.ctk.get_appearance_mode", return_value="Light"):
            assert not UnzipApp._resolve_mode()

    def test_resolve_mode_dark(self) -> None:
        from kaito.gui.unzip_app import UnzipApp

        with patch("kaito.gui.unzip_app.ctk.get_appearance_mode", return_value="Dark"):
            assert UnzipApp._resolve_mode()

    def test_resolve_mode_system_dark(self) -> None:
        from kaito.gui.unzip_app import UnzipApp

        with (
            patch("kaito.gui.unzip_app.ctk.get_appearance_mode", return_value="System"),
            patch("darkdetect.isDark", return_value=True),
        ):
            assert UnzipApp._resolve_mode()

    def test_apply_tree_style_dark(self) -> None:
        """暗黙のクラムテーマと色設定をmockで検証(dark)"""
        from kaito.gui.unzip_app import UnzipApp

        mock_style = MagicMock(name="mock_style_dark")
        with patch("kaito.gui.unzip_app.ttk.Style", return_value=mock_style):
            app = MagicMock()
            app._resolve_mode = MagicMock(return_value=True)
            UnzipApp._apply_tree_style(app)
            mock_style.theme_use.assert_called_with("clam")
            call = mock_style.configure.call_args_list[0]
            assert call.args[0] == "Treeview"
            assert call.kwargs["foreground"] == theme.TREE_DARK_FG
            assert call.kwargs["background"] == theme.TREE_DARK_BG
            assert call.kwargs["fieldbackground"] == theme.TREE_DARK_BG
            assert call.kwargs["borderwidth"] == 0
            assert call.kwargs["rowheight"] == theme.TREE_ROW_HEIGHT
            # ttk にはフォント指定タプル（正数=px）を渡す（CTkFont の GC 落ち防止）
            assert call.kwargs["font"] == (theme.TREE_FONT_FAMILY, theme.TREE_FONT_SIZE)
            heading = mock_style.configure.call_args_list[1]
            assert heading.args[0] == "Treeview.Heading"
            assert heading.kwargs["font"] == (
                theme.TREE_FONT_FAMILY,
                theme.TREE_FONT_SIZE,
                "bold",
            )

    def test_apply_tree_style_light(self) -> None:
        """暗黙のクラムテーマと色設定をmockで検証(light)"""
        from kaito.gui.unzip_app import UnzipApp

        mock_style = MagicMock(name="mock_style_light")
        with patch("kaito.gui.unzip_app.ttk.Style", return_value=mock_style):
            app = MagicMock()
            app._resolve_mode = MagicMock(return_value=False)
            UnzipApp._apply_tree_style(app)
            call = mock_style.configure.call_args_list[0]
            assert call.args[0] == "Treeview"
            assert call.kwargs["foreground"] == theme.TREE_LIGHT_FG
            assert call.kwargs["background"] == theme.TREE_LIGHT_BG
            assert call.kwargs["fieldbackground"] == theme.TREE_LIGHT_BG
            assert call.kwargs["borderwidth"] == 0
            assert call.kwargs["rowheight"] == theme.TREE_ROW_HEIGHT
            assert call.kwargs["font"] == (theme.TREE_FONT_FAMILY, theme.TREE_FONT_SIZE)


class TestUnzipAppMethods:
    """UnzipApp の個別メソッド（モックインスタンスでテスト）"""

    @pytest.fixture
    def app(self) -> MagicMock:
        return _make_app_mock()

    def test_on_drop_no_data(self, app: MagicMock) -> None:
        with patch.object(app, "_load_archive") as mock_load:
            event = MagicMock()
            type(event).data = ""
            app._on_drop(event)
            mock_load.assert_not_called()

    def test_on_drop_non_zip(self, app: MagicMock, tmp_path: Path) -> None:
        event = MagicMock()
        type(event).data = str(tmp_path / "readme.txt")
        with patch.object(app, "_load_archive") as mock_load:
            app._on_drop(event)
            mock_load.assert_not_called()

    def test_on_drop_zip(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        z.touch()
        event = MagicMock()
        type(event).data = str(z)
        with (
            patch.object(app, "_load_archive") as mock_load,
            patch.object(Path, "exists", return_value=True),
        ):
            app._on_drop(event)
            mock_load.assert_called_once()

    def test_on_drop_multiple(self, app: MagicMock, tmp_path: Path) -> None:
        z1 = tmp_path / "a.zip"
        z2 = tmp_path / "b.rar"
        z3 = tmp_path / "c.txt"
        z1.touch()
        z2.touch()
        z3.touch()
        event = MagicMock()
        type(event).data = f"{z1} {z2} {z3}"
        with (
            patch.object(app, "_load_archive") as mock_load,
            patch.object(app, "_add_to_queue") as mock_add,
            patch.object(app, "_start_compress_flow"),
            patch.object(Path, "exists", return_value=True),
        ):
            app._on_drop(event)
            mock_load.assert_called_once_with(z1)
            mock_add.assert_called_once_with(z2)

    def test_add_to_queue(self, app: MagicMock) -> None:
        app._archive_queue = [(Path("a.zip"), False)]
        with patch("kaito.gui.unzip_app.list_archive", return_value=([], False)):
            app._add_to_queue(Path("b.zip"))
        assert len(app._archive_queue) == 2

    def test_update_queue_status(self, app: MagicMock) -> None:
        app._archive_queue = [(Path("a.zip"), False), (Path("b.zip"), False)]
        app._status_var.get.return_value = "3エントリ"
        app._update_queue_status()
        app._status_var.set.assert_called_with("[2アーカイブ] 3エントリ")

    def test_drag_enter_highlights(self, app: MagicMock) -> None:
        app._resolve_mode = MagicMock(return_value=False)
        app._on_drag_enter()
        app._drop_frame.configure.assert_called_with(
            border_color=theme.DROP_HIGHLIGHT[0],
            fg_color=theme.ACCENT_SOFT[0],
        )

    def test_drag_leave_restores(self, app: MagicMock) -> None:
        app._resolve_mode = MagicMock(return_value=False)
        app._on_drag_leave()
        app._drop_frame.configure.assert_called_with(
            border_color=theme.DROP_BORDER[0],
            fg_color="transparent",
        )

    def test_highlight_drop_drop_frame_missing(self, app: MagicMock) -> None:
        app._drop_frame.configure.side_effect = AttributeError
        app._highlight_drop(True)  # should not raise

    def test_on_browse_no_file(self, app: MagicMock) -> None:
        with (
            patch("tkinter.filedialog.askopenfilename", return_value=""),
            patch.object(app, "_load_archive") as mock_load,
        ):
            app._on_browse()
            mock_load.assert_not_called()

    def test_on_browse_with_file(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        z.touch()
        with (
            patch("tkinter.filedialog.askopenfilename", return_value=str(z)),
            patch.object(app, "_load_archive") as mock_load,
        ):
            app._on_browse()
            mock_load.assert_called_once()

    def test_load_archive_success(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("hello.txt", "data")
            zf.writestr("sub/file.txt", "data2")
        app._dest_var.get.return_value = ""
        app._load_archive(z)
        assert app._zip_path == z
        assert len(app._entries) == 2
        assert app._path_var.set.called
        assert app._dest_var.set.called  # 常にアーカイブ名のパスに設定される

    def test_load_archive_error(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "bad.zip"
        z.write_text("not a zip")
        app._load_archive(z)
        app._status_var.set.assert_called()
        assert app._zip_path is None

    def test_load_archive_with_existing_dest(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        app._dest_var.get.return_value = "C:\\custom\\path"
        app._load_archive(z)
        # 以前の保存値に関わらずアーカイブ名のパスに上書きされる
        assert app._dest_var.set.called

    def _set_dest_settings(self, app: MagicMock, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "dest_mode": "archive",
            "last_dest": "",
            "fixed_dest": "",
        }
        defaults.update(overrides)
        app._settings.get.side_effect = lambda k, d=None: defaults.get(k, d)

    def test_load_archive_dest_mode_archive(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="archive")
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(z.parent / z.stem))

    def test_load_archive_dest_mode_last_valid(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        last_dir = tmp_path / "last"
        last_dir.mkdir()
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="last", last_dest=str(last_dir))
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(last_dir))

    def test_load_archive_dest_mode_last_invalid(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        """last_dest が存在しない場合はアーカイブ基準にフォールバック"""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="last", last_dest="C:\\gone_dir")
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(z.parent / z.stem))

    def test_load_archive_dest_mode_fixed_valid(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        fixed_dir = tmp_path / "fixed"
        fixed_dir.mkdir()
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="fixed", fixed_dest=str(fixed_dir))
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(fixed_dir))

    def test_load_archive_dest_mode_fixed_invalid(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        """固定フォルダーが存在しない場合はアーカイブ基準にフォールバック"""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="fixed", fixed_dest="C:\\gone_dir")
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(z.parent / z.stem))

    def test_load_archive_dest_mode_unknown(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        """未知のdest_modeはarchiveとして扱う"""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        self._set_dest_settings(app, dest_mode="unknown_mode", last_dest=str(tmp_path))
        app._load_archive(z)
        app._dest_var.set.assert_called_with(str(z.parent / z.stem))

    def test_refresh_tree(self, app: MagicMock) -> None:
        from datetime import datetime
        from kaito.unzip import ZipEntry

        app._entries = [
            ZipEntry("file.txt", 100, 80, datetime(2026, 6, 2, 10, 0, 0), False),
            ZipEntry("dir/", 0, 0, datetime(2026, 1, 1, 0, 0, 0), True),
        ]
        app._tree.get_children.return_value = ["old"]
        app._refresh_tree()
        app._tree.delete.assert_called_with("old")
        assert app._tree.insert.call_count == 2

    def test_refresh_tree_filtered(self, app: MagicMock) -> None:
        """検索絞り込みでエントリがフィルターされる"""
        from datetime import datetime
        from kaito.unzip import ZipEntry

        app._entries = [
            ZipEntry("hello.txt", 100, 80, datetime(2026, 6, 2, 10, 0, 0), False),
            ZipEntry("world.txt", 200, 160, datetime(2026, 1, 1, 0, 0, 0), False),
        ]
        app._tree.get_children.return_value = []
        app._search_var.get.return_value = "hello"
        app._refresh_tree()
        assert app._tree.insert.call_count == 1

    def test_refresh_tree_filter_empty(self, app: MagicMock) -> None:
        """該当なしの検索でもクラッシュしない"""
        from datetime import datetime
        from kaito.unzip import ZipEntry

        app._entries = [
            ZipEntry("hello.txt", 100, 80, datetime(2026, 6, 2, 10, 0, 0), False),
        ]
        app._tree.get_children.return_value = []
        app._search_var.get.return_value = "zzz"
        app._refresh_tree()
        assert app._tree.insert.call_count == 0

    def test_on_search_keyrelease(self, app: MagicMock) -> None:
        """検索キー入力がツリー再描画を呼ぶ"""
        with patch.object(app, "_refresh_tree") as mock_refresh:
            app._on_search_keyrelease()
            mock_refresh.assert_called_once()

    def test_on_dest_browse_with_path(self, app: MagicMock) -> None:
        with patch("tkinter.filedialog.askdirectory", return_value="C:\\out"):
            app._on_dest_browse()
            app._dest_var.set.assert_called_with("C:\\out")

    def test_on_dest_browse_cancel(self, app: MagicMock) -> None:
        with patch("tkinter.filedialog.askdirectory", return_value=""):
            app._on_dest_browse()
            app._dest_var.set.assert_not_called()

    def test_on_extract_no_queue(self, app: MagicMock) -> None:
        app._archive_queue = []
        app._on_extract()
        app._browse_btn.configure.assert_not_called()

    def test_on_extract_already_busy(self, app: MagicMock) -> None:
        app._archive_queue = [(Path("x.zip"), False)]
        app._extracting = True
        app._on_extract()
        app._browse_btn.configure.assert_not_called()

    def test_on_extract_normal(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        app._zip_path = z
        app._archive_queue = [(z, False)]
        app._is_encrypted = False
        app._dest_var.get.return_value = ""
        with patch("threading.Thread"):
            app._on_extract()
            assert app._extracting
            assert app._worker is not None
            assert app._worker.paths == [z]
            # キャンセルボタンが表示される
            app._cancel_btn.grid.assert_called_once()

    def test_on_extract_encrypted_manual(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        app._zip_path = z
        app._archive_queue = [(z, False)]
        app._is_encrypted = True
        app._dest_var.get.return_value = str(tmp_path / "out")
        with (
            patch("kaito.gui.unzip_app.ctk.CTkInputDialog") as dlg,
            patch.object(app, "_set_ui_enabled"),
        ):
            dlg.return_value.get_input.return_value = "mypass"
            with patch("threading.Thread"):
                app._on_extract()
                assert app._extracting

    def test_on_extract_encrypted_cancel(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        app._zip_path = z
        app._archive_queue = [(z, True)]  # 暗号化 → パスワード入力キャンセル経路を検証
        app._is_encrypted = True
        with (
            patch("kaito.gui.unzip_app.ctk.CTkInputDialog") as dlg,
        ):
            dlg.return_value.get_input.return_value = None
            app._on_extract()
            assert not app._extracting

    def test_run_worker_success(self, app: MagicMock, tmp_path: Path) -> None:
        """workerの結果が_on_extract_doneに渡される"""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        dest = tmp_path / "out"
        app._zip_path = z
        worker = ExtractWorker([z], dest, active_password=None, active_zip_path=z)
        app._worker = worker
        app._on_extract_done = MagicMock()
        # after() に登録されたコールバックを即時実行する
        app.after.side_effect = lambda _delay, callback, *args, **kwargs: callback(
            *args, **kwargs
        )
        app._run_worker()
        assert (dest / z.stem / "a.txt").read_text() == "data"
        app._on_extract_done.assert_called_once()
        result = app._on_extract_done.call_args[0][0]
        assert result.success_count == 1
        assert result.extracted_dests == [dest / z.stem]

    def test_run_worker_cancel(self, app: MagicMock, tmp_path: Path) -> None:
        """キャンセルされた場合はcanceledフラグが立つ"""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        dest = tmp_path / "out"
        app._zip_path = z
        worker = ExtractWorker([z], dest, active_password=None, active_zip_path=z)
        worker.cancel()
        app._worker = worker
        app._on_extract_done = MagicMock()
        app.after.side_effect = lambda _delay, callback, *args, **kwargs: callback(
            *args, **kwargs
        )
        app._run_worker()
        result = app._on_extract_done.call_args[0][0]
        assert result.canceled

    def test_on_cancel_extract(self, app: MagicMock) -> None:
        """キャンセルボタンでworker.cancel()が呼ばれる"""
        worker = MagicMock()
        app._worker = worker
        app._on_cancel_extract()
        worker.cancel.assert_called_once()

    def test_on_extract_done_no_open(self, app: MagicMock) -> None:
        app._extracting = True
        app._open_on_done_var.get.return_value = False
        app._archive_queue = [(Path("a.zip"), False)]
        app._on_extract_done(ExtractResult(success_count=1))
        assert not app._extracting
        app._progress.set.assert_called_with(1)
        app._status_var.set.assert_called_with("解凍完了 (1アーカイブ)")

    def test_on_extract_done_open_folder(self, app: MagicMock, tmp_path: Path) -> None:
        app._extracting = True
        app._open_on_done_var.get.return_value = True
        app._zip_path = Path("dummy.zip")
        out = tmp_path / "out"
        out.mkdir()
        app._dest_var.get.return_value = str(out)
        with (
            patch("subprocess.Popen") as mock_popen,
            patch("sys.platform", "win32"),
        ):
            app._on_extract_done(ExtractResult(success_count=1))
            mock_popen.assert_called_once_with(["explorer", str(out)])

    def test_on_extract_done_opens_actual_dest_single(
        self,
        app: MagicMock,
        tmp_path: Path,
    ) -> None:
        """単一アーカイブ時は実際の展開先（_extracted_dests[0]）を開く"""
        app._extracting = True
        app._open_on_done_var.get.return_value = True
        app._zip_path = Path("dummy.zip")
        real_dest = tmp_path / "actual"
        real_dest.mkdir()
        app._extracted_dests = [real_dest]
        app._dest_var.get.return_value = str(tmp_path / "base")
        with (
            patch("subprocess.Popen") as mock_popen,
            patch("sys.platform", "win32"),
        ):
            app._on_extract_done(
                ExtractResult(success_count=1, extracted_dests=[real_dest])
            )
            mock_popen.assert_called_once_with(["explorer", str(real_dest)])

    def test_on_extract_done_dest_missing_no_explorer(self, app: MagicMock) -> None:
        """展開先が存在しない場合はエクスプローラを起動しない"""
        app._extracting = True
        app._open_on_done_var.get.return_value = True
        app._zip_path = Path("dummy.zip")
        app._dest_var.get.return_value = "C:\\nonexistent_dir_xyz"
        with (
            patch("subprocess.Popen") as mock_popen,
            patch("sys.platform", "win32"),
        ):
            app._on_extract_done(ExtractResult(success_count=1))
            mock_popen.assert_not_called()

    def test_on_extract_done_fixed_mode_keeps_last_dest(self, app: MagicMock) -> None:
        """固定フォルダーモードでは last_dest を更新しない"""
        app._extracting = True
        app._open_on_done_var.get.return_value = False
        app._archive_queue = [(Path("a.zip"), False)]
        app._settings.get.side_effect = lambda k, d=None: {"dest_mode": "fixed"}.get(
            k, d
        )
        app._on_extract_done(ExtractResult(success_count=1))
        saved = app._settings.set_many.call_args[0][0]
        assert "last_dest" not in saved

    def test_on_extract_done_last_mode_saves_last_dest(self, app: MagicMock) -> None:
        """最後に使用したフォルダーモードでは last_dest を更新する"""
        app._extracting = True
        app._open_on_done_var.get.return_value = False
        app._archive_queue = [(Path("a.zip"), False)]
        app._dest_var.get.return_value = "C:\\out"
        app._settings.get.side_effect = lambda k, d=None: {"dest_mode": "last"}.get(
            k, d
        )
        app._on_extract_done(ExtractResult(success_count=1))
        saved = app._settings.set_many.call_args[0][0]
        assert saved["last_dest"] == "C:\\out"

    def test_on_extract_done_close(self, app: MagicMock) -> None:
        app._extracting = True
        app._close_on_done_var.get.return_value = True
        app._archive_queue = [(Path("a.zip"), False)]
        app.destroy = MagicMock()
        app._on_extract_done(ExtractResult(success_count=1))
        app.after.assert_called_once_with(500, app.destroy)

    def test_set_ui_enabled_disabled(self, app: MagicMock) -> None:
        app._set_ui_enabled(False)
        app._browse_btn.configure.assert_called_with(state="disabled")
        app._dest_btn.configure.assert_called_with(state="disabled")
        app._extract_btn.configure.assert_called_with(state="disabled")

    def test_set_ui_enabled_enabled(self, app: MagicMock) -> None:
        app._set_ui_enabled(True)
        app._browse_btn.configure.assert_called_with(state="normal")
        app._dest_btn.configure.assert_called_with(state="normal")
        app._extract_btn.configure.assert_called_with(state="normal")

    def test_on_theme_changed(self, app: MagicMock) -> None:
        with patch("kaito.gui.unzip_app.ctk.set_appearance_mode") as mock_set:
            app._on_theme_changed("dark")
            mock_set.assert_called_with("dark")
            app._settings.set.assert_called_with("theme", "dark")

    def test_on_language_changed(self, app: MagicMock) -> None:
        """言語切替で set_language と再表示が呼ばれる"""
        with (
            patch("kaito.gui.unzip_app.set_language") as mock_set,
            patch.object(app, "_retranslate") as mock_retranslate,
        ):
            app._on_language_changed("en")
            mock_set.assert_called_once_with("en")
            mock_retranslate.assert_called_once()

    def test_retranslate(self, app: MagicMock) -> None:
        """言語切替で全静的テキストが再設定される"""
        for attr in (
            "_header_subtitle",
            "_settings_btn",
            "_archive_label",
            "_browse_btn",
            "_drop_label",
            "_drop_sub_label",
            "_contents_label",
            "_search_entry",
            "_tree",
            "_dest_label",
            "_dest_btn",
            "_open_check",
            "_close_check",
            "_compress_btn",
            "_extract_btn",
            "_cancel_btn",
        ):
            setattr(app, attr, MagicMock())
        with patch.object(app, "_refresh_recent_menu") as mock_refresh:
            app._retranslate()
        app._header_subtitle.configure.assert_called_once()
        app._browse_btn.configure.assert_called_once()
        app._search_entry.configure.assert_called_once()
        assert app._tree.heading.call_count == 4
        mock_refresh.assert_called_once()

    def test_start_theme_poll_system(self, app: MagicMock) -> None:
        with (
            patch("kaito.gui.unzip_app.ctk.get_appearance_mode", return_value="System"),
            patch.object(app, "_resolve_mode", return_value=True),
        ):
            app._tree_poll_id = None
            app._start_theme_poll()
            assert app._tree_poll_id is not None
            app.after.assert_called_with(2000, app._poll_appearance_mode)

    def test_start_theme_poll_non_system(self, app: MagicMock) -> None:
        with patch("kaito.gui.unzip_app.ctk.get_appearance_mode", return_value="Light"):
            app._start_theme_poll()
            assert app._tree_poll_id is None

    def test_stop_theme_poll_cancels(self, app: MagicMock) -> None:
        app._tree_poll_id = "123"
        app.after_cancel = MagicMock()
        app._stop_theme_poll()
        app.after_cancel.assert_called_with("123")
        assert app._tree_poll_id is None

    def test_poll_appearance_mode_no_change(self, app: MagicMock) -> None:
        with patch.object(app, "_resolve_mode", return_value=True):
            app._tree_last_dark = True
            app._poll_appearance_mode()
            app.after.assert_called_with(2000, app._poll_appearance_mode)

    def test_poll_appearance_mode_changed(self, app: MagicMock) -> None:
        with (
            patch.object(app, "_resolve_mode", return_value=False),
            patch.object(app, "_apply_tree_style") as mock_style,
        ):
            app._tree_last_dark = True
            app._poll_appearance_mode()
            mock_style.assert_called_once()
            assert not app._tree_last_dark

    def test_on_open_settings(self, app: MagicMock) -> None:
        """設定ダイアログが開かれる"""
        with patch("kaito.gui.unzip_app.SettingsDialog") as mock_dlg:
            app._on_open_settings()
            mock_dlg.assert_called_once_with(
                parent=app,
                settings=app._settings,
                on_theme_changed=app._on_theme_changed,
                on_language_changed=app._on_language_changed,
            )

    def test_on_recent_selected_default(self, app: MagicMock) -> None:
        with patch.object(app, "_load_archive") as mock_load:
            app._on_recent_selected("最近のファイル")
            mock_load.assert_not_called()

    def test_on_recent_selected_loads(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        z.touch()
        with (
            patch.object(app, "_load_archive") as mock_load,
            patch.object(Path, "exists", return_value=True),
        ):
            app._on_recent_selected(str(z))
            mock_load.assert_called_once_with(z)

    def test_refresh_recent_menu_with_files(self, app: MagicMock) -> None:
        app._settings.get.return_value = ["a.zip", "b.zip"]
        app._refresh_recent_menu()
        app._recent_menu.configure.assert_called_with(values=["a.zip", "b.zip"])

    def test_refresh_recent_menu_empty(self, app: MagicMock) -> None:
        app._settings.get.return_value = []
        app._refresh_recent_menu()
        app._recent_menu.configure.assert_not_called()

    def test_on_tree_select_no_zip(self, app: MagicMock) -> None:
        app._zip_path = None
        app._on_tree_select()
        app._preview_label.configure.assert_not_called()

    def test_on_tree_select_no_selection(self, app: MagicMock) -> None:
        app._zip_path = Path("x.zip")
        app._tree.selection.return_value = ()
        app._on_tree_select()

    def test_on_tree_select_shows_preview(self, app: MagicMock) -> None:
        app._zip_path = Path("x.zip")
        app._tree.selection.return_value = ("item1",)
        app._tree.item.return_value = ("1", "hello.txt", "10", "8", "2025-01-01")
        mock_pv = MagicMock()
        app.__dict__["_show_preview"] = mock_pv
        try:
            app._on_tree_select()
            mock_pv.assert_called_once_with("hello.txt")
        finally:
            app.__dict__.pop("_show_preview", None)

    def test_show_preview_text(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        import zipfile

        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("hello.txt", "Hello World")
        app._zip_path = z
        with patch.object(app, "_preview_text") as mock_pt:
            app._show_preview("hello.txt")
            mock_pt.assert_called_once()

    def test_show_preview_image(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        import zipfile
        from PIL import Image
        import io

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, "PNG")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("img.png", buf.getvalue())
        app._zip_path = z
        with patch.object(app, "_preview_image") as mock_pi:
            app._show_preview("img.png")
            mock_pi.assert_called_once()

    def test_show_preview_unsupported(self, app: MagicMock) -> None:
        app._zip_path = Path("x.zip")
        app._show_preview("data.bin")
        app._preview_label.configure.assert_called()

    def test_preview_text_success(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        import zipfile

        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("hello.txt", "Hello World")
        app._zip_path = z
        app.__dict__["_preview_label"] = MagicMock()
        app.__dict__["_preview_frame"] = MagicMock()
        app._preview_text("hello.txt")
        app.__dict__["_preview_label"].configure.assert_called()

    def test_preview_text_error(self, app: MagicMock) -> None:
        app._zip_path = Path("bad.zip")
        app._preview_text("nonexistent.txt")
        app._preview_label.configure.assert_called()

    def test_show_preview_with_tempdir(self, app: MagicMock) -> None:
        import tempfile

        td = tempfile.TemporaryDirectory()
        app._temp_dir = td
        app.__dict__["_preview_label"] = MagicMock()
        app.__dict__["_preview_frame"] = MagicMock()
        app._zip_path = Path("x.zip")
        app._show_preview("data.bin")
        app.__dict__["_preview_label"].configure.assert_called()

    def test_on_tree_select_bad_values(self, app: MagicMock) -> None:
        app._zip_path = Path("x.zip")
        app._tree.selection.return_value = ("item1",)
        app._tree.item.return_value = ("just_name",)  # len < 2
        with patch.object(app, "_show_preview") as mock_pv:
            app._on_tree_select()
            mock_pv.assert_not_called()

    def test_preview_image_success(self, app: MagicMock, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        import zipfile
        from PIL import Image
        import io

        buf = io.BytesIO()
        Image.new("RGB", (50, 30), color="red").save(buf, "PNG")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("img.png", buf.getvalue())
        app._zip_path = z
        app.__dict__["_preview_label"] = MagicMock()
        app.__dict__["_preview_frame"] = MagicMock()
        app._archive_service.read_entry.return_value = buf.getvalue()
        app._preview_image("img.png")
        app.__dict__["_preview_label"].configure.assert_called()

    def test_preview_image_error(self, app: MagicMock) -> None:
        app._zip_path = Path("bad.zip")
        app.__dict__["_preview_label"] = MagicMock()
        app.__dict__["_preview_frame"] = MagicMock()
        app._archive_service.read_entry.side_effect = IOError("boom")
        app._preview_image("nonexistent.png")
        app.__dict__["_preview_label"].configure.assert_called()

    def test_read_archive_entry_zip(self, tmp_path: Path) -> None:
        z = tmp_path / "test.zip"
        import zipfile

        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("hello.txt", "data")
        assert _read_archive_entry(z, "hello.txt") == b"data"

    def test_read_archive_entry_non_zip(self, tmp_path: Path) -> None:
        z = tmp_path / "test.rar"
        z.touch()
        assert _read_archive_entry(z, "x.txt") == b""

    def test_read_archive_entry_rar_mocked(self, tmp_path: Path) -> None:
        """ArchiveService.read_entryをモックしてRARプレビューパスをテスト"""
        z = tmp_path / "test.rar"
        z.touch()
        with patch("kaito.gui.unzip_app.ArchiveService") as mock_svc:
            mock_svc.return_value.read_entry.return_value = b"RAR content"
            result = _read_archive_entry(z, "hello.txt")
            assert result == b"RAR content"
            mock_svc.return_value.read_entry.assert_called_once_with(z, "hello.txt")

    def test_load_archive_rar_mocked(self, app: MagicMock, tmp_path: Path) -> None:
        """RARもZIP同様に読み込める（事前展開はせず、プレビューはArchiveServiceへ委譲）"""
        z = tmp_path / "test.rar"
        z.touch()
        app._dest_var.get.return_value = ""
        mock_entries = []
        with patch(
            "kaito.gui.unzip_app.list_archive", return_value=(mock_entries, False)
        ):
            app._load_archive(z)
            assert app._zip_path == z
            assert app._temp_dir is None

    def test_load_archive_rar_extract_fail(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        """読み込み失敗時はエラーステータスを表示してクラッシュしない"""
        z = tmp_path / "test.rar"
        z.touch()
        app._dest_var.get.return_value = ""
        with patch(
            "kaito.gui.unzip_app.list_archive",
            side_effect=RuntimeError("extract failed"),
        ):
            app._load_archive(z)  # should not raise
            app._status_var.set.assert_called()
            assert app._zip_path is None

    def test_load_archive_cleanup_old_temp(
        self, app: MagicMock, tmp_path: Path
    ) -> None:
        """2つ目のZIPを開くとき前のRAR展開をクリーンアップ"""
        old_td = MagicMock()
        app._temp_dir = old_td
        app._dest_var.get.return_value = ""
        z = tmp_path / "test.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "data")
        app._load_archive(z)
        old_td.cleanup.assert_called_once()
        assert app._temp_dir is None

    def test_read_archive_entry_with_cache_dir(self, tmp_path: Path) -> None:
        """cache_dir引数はArchiveService委譲時も受理される"""
        z = tmp_path / "test.rar"
        z.touch()
        cache_dir = tmp_path / "cache"
        with patch("kaito.gui.unzip_app.ArchiveService") as mock_svc:
            mock_svc.return_value.read_entry.return_value = b"cached content"
            result = _read_archive_entry(z, "hello.txt", cache_dir=str(cache_dir))
            assert result == b"cached content"
            mock_svc.return_value.read_entry.assert_called_once_with(z, "hello.txt")

    def test_read_archive_entry_7z_mocked(self, tmp_path: Path) -> None:
        """ArchiveService.read_entryをモックして7zプレビューパスをテスト"""
        z = tmp_path / "test.7z"
        z.touch()
        with patch("kaito.gui.unzip_app.ArchiveService") as mock_svc:
            mock_svc.return_value.read_entry.return_value = b"PNG content"
            result = _read_archive_entry(z, "subdir/image.png")
            assert result == b"PNG content"

    def test_read_archive_entry_rar_not_found(self, tmp_path: Path) -> None:
        """エントリが見つからない場合は空バイトを返す（プレビュー用フォールバック）"""
        z = tmp_path / "test.rar"
        z.touch()
        with patch("kaito.gui.unzip_app.ArchiveService") as mock_svc:
            mock_svc.return_value.read_entry.return_value = None
            result = _read_archive_entry(z, "missing.txt")
            assert result == b""


# ---- SettingsDialog のテスト ----


class TestSettingsDialog:
    """SettingsDialog の各機能をモックでテスト"""

    def _make_dlg(self):
        """__init__ を呼ばずに実インスタンスを作成（_on_save が実メソッドを呼ぶように）"""
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        dlg._settings = MagicMock()
        dlg._on_theme_changed = MagicMock()
        dlg._on_language_changed = MagicMock()
        dlg._theme_var = MagicMock()
        dlg._theme_var.get.return_value = "dark"
        dlg._lang_var = MagicMock()
        dlg._lang_var.get.return_value = "English"
        dlg._dest_mode_var = MagicMock()
        dlg._dest_mode_var.get.return_value = "固定フォルダー"
        dlg._fixed_dest_var = MagicMock()
        dlg._fixed_dest_var.get.return_value = "C:\\fixed"
        dlg._compression_var = MagicMock()
        dlg._compression_var.get.return_value = "標準"
        dlg._preview_size_var = MagicMock()
        dlg._preview_size_var.get.return_value = "10"
        dlg._preview_pixels_var = MagicMock()
        dlg._preview_pixels_var.get.return_value = "1200"
        dlg.destroy = MagicMock()
        return dlg

    def test_save_applies_theme(self) -> None:
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = self._make_dlg()
        SettingsDialog._on_save(dlg)
        dlg._settings.set_many.assert_called_once()
        call_args = dlg._settings.set_many.call_args[0][0]
        assert call_args["theme"] == "dark"
        assert call_args["language"] == "en"  # 表示名ではなく言語コードで保存
        assert call_args["dest_mode"] == "fixed"
        assert call_args["fixed_dest"] == "C:\\fixed"
        assert call_args["compression_level"] == 6
        dlg._on_theme_changed.assert_called_once_with("dark")
        dlg._on_language_changed.assert_called_once_with("en")
        dlg.destroy.assert_called_once()

    def test_save_no_callback(self) -> None:
        """on_theme_changedがNoneでもクラッシュしない"""
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = self._make_dlg()
        dlg._on_theme_changed = None
        dlg._on_language_changed = None
        SettingsDialog._on_save(dlg)
        dlg.destroy.assert_called_once()

    def test_dest_mode_value_mapping(self) -> None:
        """ラベル→設定値の変換マッピング"""
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        dlg._dest_mode_var = MagicMock()
        mapping = {
            "アーカイブと同じフォルダー": "archive",
            "最後に使用したフォルダー": "last",
            "固定フォルダー": "fixed",
        }
        for label, value in mapping.items():
            dlg._dest_mode_var.get.return_value = label
            assert SettingsDialog._dest_mode_value(dlg) == value
        # 未知のラベルは archive にフォールバック
        dlg._dest_mode_var.get.return_value = "不明"
        assert SettingsDialog._dest_mode_value(dlg) == "archive"

    def test_dest_mode_label_mapping(self) -> None:
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        assert (
            SettingsDialog._dest_mode_label(dlg, "archive")
            == "アーカイブと同じフォルダー"
        )
        assert (
            SettingsDialog._dest_mode_label(dlg, "last") == "最後に使用したフォルダー"
        )
        assert SettingsDialog._dest_mode_label(dlg, "fixed") == "固定フォルダー"
        assert (
            SettingsDialog._dest_mode_label(dlg, "unknown")
            == "アーカイブと同じフォルダー"
        )

    def test_compression_label_mapping(self) -> None:
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        assert SettingsDialog._compression_label(dlg, 1) == "最速（サイズ大）"
        assert SettingsDialog._compression_label(dlg, 6) == "標準"
        assert SettingsDialog._compression_label(dlg, 9) == "高圧縮（時間長）"
        assert SettingsDialog._compression_label(dlg, "xyz") == "最速（サイズ大）"
        assert SettingsDialog._compression_label(dlg, "7") == "最速（サイズ大）"

    def test_compression_level_mapping(self) -> None:
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        dlg._compression_var = MagicMock()
        mapping = {"最速（サイズ大）": 1, "標準": 6, "高圧縮（時間長）": 9}
        for label, value in mapping.items():
            dlg._compression_var.get.return_value = label
            assert SettingsDialog._compression_level(dlg) == value
        dlg._compression_var.get.return_value = "不明"
        assert SettingsDialog._compression_level(dlg) == 1

    def test_lang_label_mapping(self) -> None:
        """言語コード→表示名の変換"""
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        assert SettingsDialog._lang_label(dlg, "ja") == "日本語"
        assert SettingsDialog._lang_label(dlg, "en") == "English"
        assert SettingsDialog._lang_label(dlg, "unknown") == "日本語"

    def test_lang_code_mapping(self) -> None:
        """表示名→言語コードの変換（未知の表示名はjaにフォールバック）"""
        from kaito.gui.settings_dialog import SettingsDialog

        dlg = SettingsDialog.__new__(SettingsDialog)
        assert SettingsDialog._lang_code(dlg, "日本語") == "ja"
        assert SettingsDialog._lang_code(dlg, "English") == "en"
        assert SettingsDialog._lang_code(dlg, "フランス語") == "ja"


# ---- _truncate_path のテスト ----


class TestCompressMethods:
    """圧縮機能メソッドのテスト"""

    @pytest.fixture
    def app(self) -> MagicMock:
        return _make_app_mock()

    def test_on_compress_cancel(self, app: MagicMock) -> None:
        with patch("tkinter.filedialog.askopenfilenames", return_value=()):
            app._on_compress()
            assert app._compress_sources == []

    def test_on_compress_selects_files(self, app: MagicMock, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f1.touch()
        f2 = tmp_path / "b.txt"
        f2.touch()
        with (
            patch(
                "tkinter.filedialog.askopenfilenames", return_value=(str(f1), str(f2))
            ),
            patch.object(app, "_start_compress_flow") as mock_flow,
        ):
            app._on_compress()
            assert len(app._compress_sources) == 2
            mock_flow.assert_called_once()

    def test_start_compress_flow_no_sources(self, app: MagicMock) -> None:
        app._compress_sources = []
        with patch.object(app, "_set_ui_enabled") as mock_set:
            app._start_compress_flow()
            mock_set.assert_not_called()

    def test_start_compress_flow_cancel_save(self, app: MagicMock) -> None:
        app._compress_sources = [Path("a.txt")]
        with (
            patch("tkinter.filedialog.asksaveasfilename", return_value=""),
            patch.object(app, "_set_ui_enabled") as mock_set,
        ):
            app._start_compress_flow()
            mock_set.assert_not_called()

    def test_start_compress_flow_starts_thread(self, app: MagicMock) -> None:
        app._compress_sources = [Path("a.txt")]
        with (
            patch("tkinter.filedialog.asksaveasfilename", return_value="C:\\out.zip"),
            patch("kaito.gui.unzip_app.Thread") as mock_thread,
        ):
            app._start_compress_flow()
            assert app._compressing
            mock_thread.assert_called_once()

    def test_do_compress_success(self, app: MagicMock, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("data")
        output = tmp_path / "out.zip"
        with patch("kaito.gui.unzip_app.create_archive") as mock_ca:
            app._do_compress([src], output)
            mock_ca.assert_called_once()
            args, kwargs = mock_ca.call_args
            assert args[0] == [src]
            assert args[1] == output
            assert callable(kwargs["on_progress"])

    def test_do_compress_error(self, app: MagicMock, tmp_path: Path) -> None:
        output = tmp_path / "out.zip"
        with patch(
            "kaito.gui.unzip_app.create_archive", side_effect=RuntimeError("fail")
        ):
            app._do_compress([], output)
            # _on_compress_error は after 経由で呼ばれる
            assert app.after.called

    def test_on_compress_done(self, app: MagicMock) -> None:
        app._compressing = True
        app._compress_sources = [Path("x.txt")]
        app._on_compress_done()
        assert not app._compressing
        assert app._compress_sources == []
        app._status_var.set.assert_called_with("圧縮完了")

    def test_on_compress_error(self, app: MagicMock) -> None:
        app._compressing = True
        app._on_compress_error("disk full")
        assert not app._compressing
        app._status_var.set.assert_called_with("エラー: disk full")

    def test_start_compress_flow_no_dialog_keeps_flag(self, app: MagicMock) -> None:
        """CLI圧縮（no-dialog）はフラグを消費せず、完了時の自動クローズ判定に残す"""
        app._compress_sources = [Path("a.txt")]
        app._compress_no_dialog = True
        with patch.object(app, "_start_compress") as mock_start:
            app._start_compress_flow()
            mock_start.assert_called_once()
        # 旧実装は開始時に False にリセットし、自動クローズが発火しなかった
        assert app._compress_no_dialog

    def test_on_compress_done_no_dialog_schedules_destroy(self, app: MagicMock) -> None:
        """CLI圧縮完了時は after(500, destroy) で自動クローズする（exeがハングしない）"""
        app._compress_no_dialog = True
        with patch.object(app, "_set_ui_enabled") as mock_set:
            app._on_compress_done()
        mock_set.assert_not_called()
        calls = app.after.call_args_list
        assert any(call.args[0] == 500 and callable(call.args[1]) for call in calls), (
            f"after(500, destroy) が呼ばれていません: {calls}"
        )

    def test_drop_starts_compress(self, app: MagicMock, tmp_path: Path) -> None:
        """非アーカイブのファイルをドロップ → 圧縮フロー開始"""
        f = tmp_path / "readme.txt"
        f.touch()
        event = MagicMock()
        type(event).data = str(f)
        with (
            patch.object(app, "_start_compress_flow") as mock_flow,
            patch.object(Path, "exists", return_value=True),
        ):
            app._on_drop(event)
            assert len(app._compress_sources) == 1
            mock_flow.assert_called_once()


class TestContextMenu:
    """install_context_menu / uninstall_context_menu のテスト"""


    def test_get_exe_path_uses_repo_dist_and_never_python_fallback(self) -> None:
        fake_sys = SimpleNamespace(
            frozen=False, executable="C:/repo/.venv/Scripts/python.exe"
        )
        with (
            patch("kaito.gui.unzip_app.sys", fake_sys),
            patch("kaito.gui.unzip_app.__file__", "C:/repo/src/kaito/gui/unzip_app.py"),
            patch("kaito.gui.unzip_app.Path.exists", return_value=True),
        ):
            from kaito.gui.unzip_app import _get_exe_path

            assert _get_exe_path() == Path("C:/repo/dist/kaito.exe")

        with (
            patch("kaito.gui.unzip_app.sys", fake_sys),
            patch("kaito.gui.unzip_app.__file__", "C:/repo/src/kaito/gui/unzip_app.py"),
            patch("kaito.gui.unzip_app.Path.exists", return_value=False),
            pytest.raises(FileNotFoundError, match="dist/kaito.exe"),
        ):
            _get_exe_path()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only test")
    def test_install_context_menu(self) -> None:
        mock_key = MagicMock()
        with (
            patch(
                "kaito.gui.unzip_app.CreateKeyEx", return_value=mock_key
            ) as mock_create,
            patch("kaito.gui.unzip_app.SetValueEx") as mock_set,
            patch(
                "kaito.gui.unzip_app._get_exe_path", return_value=Path("C:\\kaito.exe")
            ),
        ):
            from kaito.gui.unzip_app import install_context_menu

            install_context_menu()
            assert mock_create.call_count == 12  # SFA(3)*2 + *(1)*2 + *(1)*2 + Dir(1)*2
            assert mock_set.call_count == 12
            # 解凍メニュー名（valueに"解凍"）が4件（SFA 3 + * fallback 1）
            extract_names = [c for c in mock_set.mock_calls if "解凍" in str(c)]
            assert len(extract_names) == 4
            # 圧縮メニュー名（valueに"圧縮"）が2件
            compress_names = [c for c in mock_set.mock_calls if "圧縮" in str(c)]
            assert len(compress_names) == 2
            # command文字列（"%1"を含む）が6件（SFA 3 + *fallback 1 + *compress 1 + Dir 1）
            cmd_calls = [c for c in mock_set.mock_calls if "%1" in str(c)]
            assert len(cmd_calls) == 6

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only test")
    def test_uninstall_context_menu(self) -> None:
        """削除が呼ばれる（実際のレジストリは触らない）"""
        with (
            patch("kaito.gui.unzip_app.OpenKey"),
            patch("kaito.gui.unzip_app.DeleteKey"),
            patch("kaito.gui.unzip_app.QueryInfoKey", return_value=(0, 0)),
        ):
            from kaito.gui.unzip_app import uninstall_context_menu

            uninstall_context_menu()  # should not crash


class TestMainCLI:
    """main() のCLI引数テスト"""

    def test_install_context_menu_flag(self) -> None:
        with (
            patch("sys.argv", ["kaito", "--install-context-menu"]),
            patch("kaito.gui.unzip_app.install_context_menu") as mock_install,
        ):
            app_main()
            mock_install.assert_called_once()

    def test_uninstall_context_menu_flag(self) -> None:
        with (
            patch("sys.argv", ["kaito", "--uninstall-context-menu"]),
            patch("kaito.gui.unzip_app.uninstall_context_menu") as mock_uninstall,
        ):
            app_main()
            mock_uninstall.assert_called_once()

    def test_compress_flag(self, tmp_path: Path) -> None:
        f = tmp_path / "myfolder"
        f.mkdir()
        with (
            patch("sys.argv", ["kaito", "--compress", str(f)]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
        ):
            app_main()
            assert app.call_args.kwargs["cli_compress_path"] == f

    def test_compress_flag_nonexistent(self, tmp_path: Path) -> None:
        with (
            patch("sys.argv", ["kaito", "--compress", "C:\\nope"]),
            patch("kaito.gui.unzip_app.ctk.set_appearance_mode"),
            patch("kaito.gui.unzip_app.ctk.set_default_color_theme"),
            patch("kaito.gui.unzip_app.UnzipApp") as app,
        ):
            app_main()
            assert app.call_args.kwargs["cli_compress_path"] is None


class TestResolveExtractDest:
    """resolve_extract_dest (worker.py) のテスト"""

    def test_single_root_no_double_nesting(self) -> None:
        """全エントリが1つのトップレベルディレクトリを共有 → dest直下"""
        from kaito.unzip import ZipEntry

        dest = Path("C:\\out")
        archive = Path("C:\\myproject.zip")
        entries = [
            ZipEntry(
                name="myproject/file1.js",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
            ZipEntry(
                name="myproject/sub/file2.js",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
        ]
        result = resolve_extract_dest(dest, archive, entries)
        assert result == dest

    def test_root_files_creates_subfolder(self) -> None:
        """ルート直下にファイルがある → archive_stemサブフォルダを作成"""
        from kaito.unzip import ZipEntry

        dest = Path("C:\\out")
        archive = Path("C:\\archive.zip")
        entries = [
            ZipEntry(
                name="readme.txt",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
            ZipEntry(
                name="sub/file.txt",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
        ]
        result = resolve_extract_dest(dest, archive, entries)
        assert result == dest / "archive"

    def test_no_entries(self) -> None:
        """空のエントリ → archive_stemサブフォルダを作成"""
        dest = Path("C:\\out")
        archive = Path("C:\\empty.zip")
        result = resolve_extract_dest(dest, archive, [])
        assert result == dest / "empty"

    def test_multiple_roots(self) -> None:
        """複数のトップレベルディレクトリ → archive_stemサブフォルダ"""
        from kaito.unzip import ZipEntry

        dest = Path("C:\\out")
        archive = Path("C:\\multi.zip")
        entries = [
            ZipEntry(
                name="dir1/a.txt",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
            ZipEntry(
                name="dir2/b.txt",
                size=0,
                compressed_size=0,
                modified=datetime.now(),
                is_dir=False,
            ),
        ]
        result = resolve_extract_dest(dest, archive, entries)
        assert result == dest / "multi"


class TestTruncatePath:
    def test_short_path(self) -> None:
        assert _truncate_path("C:\\a.zip") == "C:\\a.zip"

    def test_long_path_with_ellipsis(self) -> None:
        long_path = "C:\\" + "very_long_directory_name\\" * 10 + "file.zip"
        result = _truncate_path(long_path, max_len=60)
        assert len(result) <= 60
        assert "..." in result or "\\" in result

    def test_filename_too_long(self) -> None:
        long_name = "a" * 70 + ".zip"
        path = f"C:\\Users\\test\\{long_name}"
        result = _truncate_path(path, max_len=60)
        assert result.endswith("...")

    def test_medium_path(self) -> None:
        path = "C:\\Users\\test\\file.zip"
        result = _truncate_path(path, max_len=60)
        assert "file.zip" in result

    def test_parent_fits_returns_full_path(self) -> None:
        """親ディレクトリが収まる場合は省略されない"""
        name = "file.zip"  # 8 chars
        parent = "C:\\" + "x" * 46  # 49 chars
        path = parent + "\\" + name  # 58 chars <= max_len 60
        result = _truncate_path(path, max_len=60)
        assert result == path

    def test_name_too_long_truncates_name(self) -> None:
        """ファイル名自体が長い場合は名前側を省略"""
        name = "a" * 70 + ".zip"
        path = f"C:\\Users\\test\\{name}"
        result = _truncate_path(path, max_len=60)
        assert len(result) <= 60

    def test_truncate_remain_one_skips_parent(self) -> None:
        """remain=1: セパレータ表示の余白がなく親パスを完全に省略する"""
        name = "a" * 16  # len(name)=16, max_len=20 → remain=1
        parent = "C:\\" + "x" * 30
        result = _truncate_path(parent + "\\" + name, max_len=20)
        assert result == "..." + name
        assert len(result) <= 20

    def test_truncate_remain_two_shows_last_parent_char(self) -> None:
        """remain=2: 親パスの末尾1文字とセパレータを表示"""
        name = "a" * 15  # len(name)=15, max_len=20 → remain=2
        parent = "C:\\" + "x" * 30
        result = _truncate_path(parent + "\\" + name, max_len=20)
        assert result == "..." + "x" + "\\" + name
        assert len(result) == 20

    def test_truncate_remain_three_shows_last_two_parent_chars(self) -> None:
        """remain=3: 親パスの末尾2文字を表示（従来は親全体が表示され max_len を超過）"""
        name = "a" * 14  # len(name)=14, max_len=20 → remain=3
        parent = "C:\\" + "x" * 30
        result = _truncate_path(parent + "\\" + name, max_len=20)
        assert result == "..." + "xx" + "\\" + name
        assert len(result) == 20

    def test_truncate_never_exceeds_max_len(self) -> None:
        """あらゆるmax_lenで結果がmax_len以下に収まり、収まる場合はファイル名が残る"""
        name = "file.zip"
        parent = "C:\\" + "long_dir\\" * 20
        path = parent + name
        for max_len in range(8, 61):
            result = _truncate_path(path, max_len)
            assert len(result) <= max_len
            if max_len >= len(name) + 3:
                assert name in result


# ---- プレビューのデコード・サイズ上限（統合ゲート: preview 上限） ----


class TestDecodeText:
    """_decode_text の文字数上限とエンコーディングフォールバック"""

    def test_truncates_to_max_chars(self) -> None:
        """2000文字を超えるテキストは切り詰められる"""
        data = ("あ" * 3000).encode("utf-8")
        result = _decode_text(data)
        assert len(result) <= 2000

    def test_utf8_decoded_first(self) -> None:
        assert _decode_text("こんにちは".encode("utf-8")) == "こんにちは"

    def test_cp932_fallback(self) -> None:
        """UTF-8でないバイト列はシステムエンコーディングで再試行"""
        data = "日本語テキスト".encode("cp932")
        with patch("kaito.gui.unzip_app.locale.getencoding", return_value="cp932"):
            assert _decode_text(data) == "日本語テキスト"

    def test_invalid_bytes_replaced(self) -> None:
        """どのエンコーディングでも失敗するバイト列は置換文字で返す"""
        data = b"\xff\xfe\x00\x01binary"
        with patch("kaito.gui.unzip_app.locale.getencoding", return_value="utf-8"):
            result = _decode_text(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_custom_max_chars(self) -> None:
        assert _decode_text(b"abcdef", max_chars=3) == "abc"


class TestPreviewSizeLimit:
    """_read_archive_entry のプレビューサイズ上限（統合ゲート: preview サイズ上限）"""

    def test_entry_within_limit_is_read(self, tmp_path: Path) -> None:
        z = tmp_path / "small.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hello")
        assert _read_archive_entry(z, "a.txt", max_bytes=1024) == b"hello"

    def test_entry_over_limit_returns_empty(self, tmp_path: Path) -> None:
        """上限超過エントリは読み込まず空を返す（メモリ保護）"""
        z = tmp_path / "big.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("big.txt", b"x" * 5000)
        assert _read_archive_entry(z, "big.txt", max_bytes=100) == b""

    def test_missing_entry_raises_keyerror(self, tmp_path: Path) -> None:
        z = tmp_path / "small.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hello")
        with pytest.raises(KeyError):
            _read_archive_entry(z, "missing.txt")

    def test_zip_preview_no_subprocess(self, tmp_path: Path) -> None:
        """ZIPのプレビュー読み取りも subprocess を起動しない"""
        z = tmp_path / "small.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hello")
        with patch("subprocess.Popen") as mock_popen:
            assert _read_archive_entry(z, "a.txt") == b"hello"
        mock_popen.assert_not_called()
