"""
src/kaito/gui/unzip_app.py
CustomTkinterを使用したZIP/RAR/7z解凍・圧縮GUIアプリ
ドラッグ&ドロップ対応 (tkinterdnd2)
関連: archive/service.py (解凍/圧縮コアロジック), settings_dialog.py
"""

import io
import locale
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread
from tkinter import filedialog, ttk
from zipfile import BadZipFile

try:
    from winreg import (  # type: ignore[attr-defined]
        CreateKeyEx,
        DeleteKey,
        EnumKey,
        HKEY_CURRENT_USER,
        KEY_SET_VALUE,
        OpenKey,
        QueryInfoKey,
        REG_SZ,
        SetValueEx,
    )
except ImportError:  # pragma: no cover
    # Non-Windows: registry functions not available
    pass

from PIL import Image, ImageDraw, ImageTk

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD

from kaito.version import __version__

from kaito.archive.service import ArchiveService
from kaito.domain.errors import (
    ExtractionFailedError,
    InvalidPasswordError,
    PasswordRequiredError,
)
from kaito.domain.models import SafetyLimits
from kaito.i18n import set_language, tr
from kaito.settings import SettingsManager
from kaito.unzip import (
    ZipEntry,
    create_archive,
    is_supported,
    list_archive,
)
from kaito.worker import ExtractResult, ExtractWorker

from kaito.gui import theme
from kaito.gui.settings_dialog import SettingsDialog

_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".log",
    ".csv",
    ".toml",
}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico"}
_MAX_PREVIEW_CHARS = 2000
_MAX_IMAGE_DIMENSION = (400, 250)
# プレビューで1エントリを読み込む最大バイト数（超過時は読み込まず空を返す）
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024


class UnzipApp(ctk.CTk, TkinterDnD.DnDWrapper):
    """ZIP解凍GUIメインウィンドウ"""

    def __init__(
        self, cli_path: Path | None = None, cli_compress_path: Path | None = None
    ) -> None:
        super().__init__()

        self.TkdndVersion = TkinterDnD._require(self)

        self.title(f"kaito v{__version__}")
        self.geometry("900x600")
        self.minsize(680, 460)

        self._zip_path: Path | None = None
        # Queue of (archive_path, is_encrypted) for batch extraction
        self._archive_queue: list[tuple[Path, bool]] = []
        self._entries: list[ZipEntry] = []
        self._is_encrypted = False
        self._extracting = False
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self._current_image: ctk.CTkImage | None = None
        self._compress_sources: list[Path] = []
        self._compressing = False
        self._compress_no_dialog = False
        self._extracted_dests: list[Path] = []
        self._worker: ExtractWorker | None = None

        # 設定を先に読み込み、言語に合わせてUIを構築する
        self._settings = SettingsManager()
        set_language(self._settings.get("language", "ja"))

        # サービス層（読み取り・安全上限は設定から構築）
        self._archive_service = ArchiveService(
            safety_limits=SafetyLimits(
                max_entries=int(
                    self._settings.get("safety_max_entries", SafetyLimits.max_entries)
                ),
                max_total_size=int(
                    self._settings.get(
                        "safety_max_total_size", SafetyLimits.max_total_size
                    )
                ),
                max_single_file_size=int(
                    self._settings.get(
                        "safety_max_file_size", SafetyLimits.max_single_file_size
                    )
                ),
                max_compression_ratio=float(
                    self._settings.get(
                        "safety_max_compression_ratio",
                        SafetyLimits.max_compression_ratio,
                    )
                ),
                max_path_length=int(
                    self._settings.get(
                        "safety_max_path_length", SafetyLimits.max_path_length
                    )
                ),
                preview_max_size=int(
                    self._settings.get(
                        "preview_max_size", SafetyLimits.preview_max_size
                    )
                ),
                preview_max_image_pixels=int(
                    self._settings.get(
                        "preview_max_image_pixels",
                        SafetyLimits.preview_max_image_pixels,
                    )
                ),
            )
        )

        self._build_ui()

        self._tree_poll_id: str | None = None
        self._apply_tree_style()
        self._start_theme_poll()

        self._open_on_done_var.set(self._settings.get("open_on_done", True))
        self._close_on_done_var.set(self._settings.get("close_on_done", False))

        self._refresh_recent_menu()

        # ウィンドウ全体をドロップターゲットに設定
        self.drop_target_register("*")
        self.dnd_bind("<<Drop>>", self._on_drop)
        self.dnd_bind("<<DragEnter>>", self._on_drag_enter)
        self.dnd_bind("<<DragLeave>>", self._on_drag_leave)

        # 起動時にファイルが渡されたら読み込む
        if cli_path is not None:
            self._load_archive(cli_path)

        # 起動時に圧縮対象が渡されたら圧縮フロー開始
        if cli_compress_path is not None:
            self._compress_sources = [cli_compress_path]
            self._compress_no_dialog = True
            self.after_idle(self._start_compress_flow)

    def _build_ui(self) -> None:  # pragma: no cover
        is_dark = self._resolve_mode()
        self.configure(fg_color=theme.pick(theme.BG, is_dark))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ===== ヘッダー: アイコン + タイトル + 設定 =====
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=24, pady=(18, 12), sticky="ew")
        header_frame.grid_columnconfigure(2, weight=1)

        # アプリアイコン（アクセント角丸の "k"）
        icon_box = ctk.CTkFrame(
            header_frame,
            width=42,
            height=42,
            corner_radius=12,
            fg_color=theme.ACCENT,
        )
        icon_box.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="w")
        icon_box.grid_propagate(False)
        ctk.CTkLabel(
            icon_box,
            text="k",
            font=theme.font(22, "bold"),
            text_color=theme.ACCENT_ON,
        ).place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            header_frame,
            text="kaito",
            font=theme.font(22, "bold"),
            text_color=theme.pick(theme.TEXT, is_dark),
        ).grid(row=0, column=1, sticky="sw")
        self._header_subtitle = ctk.CTkLabel(
            header_frame,
            text=tr("app.subtitle"),
            font=theme.font(12),
            text_color=theme.pick(theme.SUBTEXT, is_dark),
        )
        self._header_subtitle.grid(row=1, column=1, sticky="nw", pady=(0, 1))

        self._settings_btn = theme.secondary_button(
            header_frame,
            tr("app.settings"),
            self._on_open_settings,
            is_dark=is_dark,
            width=96,
            height=36,
            font_size=13,
        )
        self._settings_btn.grid(row=0, column=3, rowspan=2, padx=(12, 0), sticky="e")

        # ===== アーカイブ選択カード =====
        file_frame = theme.card(self, is_dark)
        file_frame.grid(row=1, column=0, padx=24, pady=(0, 10), sticky="ew")
        file_frame.grid_columnconfigure(1, weight=1)

        self._archive_label = ctk.CTkLabel(
            file_frame,
            text=tr("app.archive_label"),
            font=theme.font(13, "bold"),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._archive_label.grid(row=0, column=0, padx=(16, 10), pady=14, sticky="w")
        self._path_var = ctk.StringVar()
        self._path_entry = ctk.CTkEntry(
            file_frame,
            textvariable=self._path_var,
            state="readonly",
            height=38,
            corner_radius=10,
            fg_color=theme.pick(theme.BG, is_dark),
            border_color=theme.pick(theme.BORDER, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
            font=theme.ui_font(12),
        )
        self._path_entry.grid(row=0, column=1, padx=4, pady=14, sticky="ew")
        self._browse_btn = theme.primary_button(
            file_frame,
            tr("app.open"),
            self._on_browse,
            width=88,
            height=38,
            bold=True,
        )
        self._browse_btn.grid(row=0, column=2, padx=(8, 4), pady=14)

        self._recent_var = ctk.StringVar(value=tr("app.recent_files"))
        self._recent_menu = ctk.CTkOptionMenu(
            file_frame,
            values=[tr("app.recent_files")],
            variable=self._recent_var,
            width=132,
            height=38,
            corner_radius=10,
            fg_color=theme.pick(theme.SURFACE_2, is_dark),
            button_color=theme.pick(theme.SURFACE_2, is_dark),
            button_hover_color=theme.pick(theme.BORDER, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
            dropdown_fg_color=theme.pick(theme.SURFACE, is_dark),
            dropdown_hover_color=theme.pick(theme.ACCENT_SOFT, is_dark),
            dropdown_text_color=theme.pick(theme.TEXT, is_dark),
            font=theme.font(13),
            dropdown_font=theme.font(13),
            command=self._on_recent_selected,
        )
        self._recent_menu.grid(row=0, column=3, padx=(4, 16), pady=14)

        # ===== ドロップゾーン (ZIP未選択時) =====
        self._drop_frame = ctk.CTkFrame(
            self,
            border_width=2,
            fg_color="transparent",
            border_color=theme.pick(theme.DROP_BORDER, is_dark),
            corner_radius=18,
        )
        self._drop_frame.grid(row=2, column=0, padx=24, pady=6, sticky="nsew")
        self._drop_frame.grid_rowconfigure(0, weight=1)
        self._drop_frame.grid_rowconfigure(5, weight=1)
        self._drop_frame.grid_columnconfigure(0, weight=1)

        # アイコンサークル（アクセント淡色の丸に下矢印）
        icon_circle = ctk.CTkFrame(
            self._drop_frame,
            width=72,
            height=72,
            corner_radius=36,
            fg_color=theme.pick(theme.ACCENT_SOFT, is_dark),
        )
        icon_circle.grid(row=1, column=0, pady=(34, 0))
        icon_circle.grid_propagate(False)
        ctk.CTkLabel(
            icon_circle,
            text="⇩",
            font=theme.font(30, "bold"),
            text_color=theme.ACCENT,
        ).place(relx=0.5, rely=0.5, anchor="center")

        self._drop_label = ctk.CTkLabel(
            self._drop_frame,
            text=tr("app.drop_hint"),
            font=theme.font(18, "bold"),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._drop_label.grid(row=2, column=0, pady=(16, 6))
        self._drop_sub_label = ctk.CTkLabel(
            self._drop_frame,
            text=tr("app.drop_sub"),
            font=theme.font(13),
            text_color=theme.pick(theme.SUBTEXT, is_dark),
        )
        self._drop_sub_label.grid(row=3, column=0, pady=(0, 34))

        # ===== ファイル一覧 (ZIP読込後) =====
        self._list_frame = theme.card(self, is_dark)
        self._list_frame.grid_rowconfigure(1, weight=1)
        self._list_frame.grid_columnconfigure(0, weight=0)
        self._list_frame.grid_columnconfigure(1, weight=1)

        self._contents_label = ctk.CTkLabel(
            self._list_frame,
            text=tr("app.contents"),
            font=theme.ui_font(13, "bold"),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._contents_label.grid(
            row=0, column=0, padx=(16, 8), pady=(14, 4), sticky="w"
        )

        self._search_var = ctk.StringVar()
        self._search_entry = ctk.CTkEntry(
            self._list_frame,
            textvariable=self._search_var,
            placeholder_text=tr("app.search_placeholder"),
            height=32,
            corner_radius=8,
            fg_color=theme.pick(theme.BG, is_dark),
            border_color=theme.pick(theme.BORDER, is_dark),
            placeholder_text_color=theme.pick(theme.SUBTEXT, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
            font=theme.ui_font(12),
        )
        self._search_entry.grid(
            row=0, column=1, padx=(2, 16), pady=(10, 2), sticky="ew"
        )
        self._search_entry.bind("<KeyRelease>", self._on_search_keyrelease)

        tree_frame = ctk.CTkFrame(self._list_frame, fg_color="transparent")
        tree_frame.grid(
            row=1, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="nsew"
        )
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        columns = ("#", "name", "size", "compressed", "date")
        self._tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", height=12
        )
        # #0 列はフォルダ/ファイルアイコン専用（見出しなし）
        self._tree.heading("#0", text="")
        self._tree.heading("#", text="#")
        self._tree.heading("name", text=tr("tree.name"))
        self._tree.heading("size", text=tr("tree.size"))
        self._tree.heading("compressed", text=tr("tree.compressed"))
        self._tree.heading("date", text=tr("tree.date"))
        self._tree.column("#0", width=30, minwidth=26, stretch=False, anchor="center")
        self._tree.column("#", width=35, minwidth=30, stretch=False, anchor="e")
        self._tree.column("name", width=280, minwidth=160, stretch=True)
        self._tree.column("size", width=80, minwidth=60, stretch=False)
        self._tree.column("compressed", width=80, minwidth=60, stretch=False)
        self._tree.column("date", width=130, minwidth=90, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._ensure_icons(is_dark)

        # --- プレビュー ---
        self._preview_frame = ctk.CTkFrame(
            self._list_frame,
            corner_radius=10,
            fg_color=theme.pick(theme.BG, is_dark),
            border_width=1,
            border_color=theme.pick(theme.BORDER, is_dark),
        )
        self._preview_label = ctk.CTkLabel(
            self._preview_frame,
            text="",
            anchor="w",
            justify="left",
            font=theme.ui_font(12),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._preview_label.pack(fill="both", expand=True, padx=10, pady=6)

        # ===== 展開先カード =====
        dest_frame = theme.card(self, is_dark)
        dest_frame.grid(row=3, column=0, padx=24, pady=10, sticky="ew")
        dest_frame.grid_columnconfigure(1, weight=1)

        self._dest_label = ctk.CTkLabel(
            dest_frame,
            text=tr("app.dest_label"),
            font=theme.font(13, "bold"),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._dest_label.grid(row=0, column=0, padx=(16, 10), pady=12, sticky="w")
        self._dest_var = ctk.StringVar()
        self._dest_entry = ctk.CTkEntry(
            dest_frame,
            textvariable=self._dest_var,
            state="readonly",
            height=36,
            corner_radius=10,
            fg_color=theme.pick(theme.BG, is_dark),
            border_color=theme.pick(theme.BORDER, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
            font=theme.ui_font(12),
        )
        self._dest_entry.grid(row=0, column=1, padx=4, pady=12, sticky="ew")
        self._dest_btn = theme.secondary_button(
            dest_frame,
            tr("app.browse"),
            self._on_dest_browse,
            is_dark=is_dark,
            width=88,
            height=36,
            font_size=13,
        )
        self._dest_btn.grid(row=0, column=2, padx=(8, 16), pady=12)

        # ===== アクションバー（進捗・チェック・状態・ボタン） =====
        bottom_frame = theme.card(self, is_dark)
        bottom_frame.grid(row=4, column=0, padx=24, pady=(0, 18), sticky="ew")
        bottom_frame.grid_columnconfigure(2, weight=1)

        self._progress = ctk.CTkProgressBar(
            bottom_frame,
            mode="determinate",
            height=8,
            fg_color=theme.pick(theme.BORDER, is_dark),
            progress_color=theme.ACCENT,
            corner_radius=4,
        )
        self._progress.grid(
            row=0, column=0, columnspan=6, padx=16, pady=(12, 4), sticky="ew"
        )
        self._progress.set(0)
        self._progress.grid_remove()

        self._open_on_done_var = ctk.BooleanVar(value=True)
        self._open_check = ctk.CTkCheckBox(
            bottom_frame,
            text=tr("app.open_folder_on_done"),
            font=theme.font(13),
            variable=self._open_on_done_var,
            onvalue=True,
            offvalue=False,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            checkmark_color=theme.ACCENT_ON,
            border_color=theme.pick(theme.BORDER, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._open_check.grid(row=1, column=0, padx=16, pady=(6, 2), sticky="w")

        self._close_on_done_var = ctk.BooleanVar(value=False)
        self._close_check = ctk.CTkCheckBox(
            bottom_frame,
            text=tr("app.close_on_done"),
            font=theme.font(13),
            variable=self._close_on_done_var,
            onvalue=True,
            offvalue=False,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            checkmark_color=theme.ACCENT_ON,
            border_color=theme.pick(theme.BORDER, is_dark),
            text_color=theme.pick(theme.TEXT, is_dark),
        )
        self._close_check.grid(row=1, column=1, padx=(0, 8), pady=(6, 2), sticky="w")

        self._status_var = ctk.StringVar(value=tr("app.status_ready"))
        self._status_label = ctk.CTkLabel(
            bottom_frame,
            textvariable=self._status_var,
            anchor="w",
            font=theme.ui_font(12),
            text_color=theme.pick(theme.SUBTEXT, is_dark),
        )
        self._status_label.grid(
            row=2, column=0, columnspan=3, padx=16, pady=(2, 14), sticky="w"
        )

        self._compress_btn = theme.secondary_button(
            bottom_frame,
            tr("app.compress"),
            self._on_compress,
            is_dark=is_dark,
            width=104,
            height=40,
            font_size=13,
        )
        self._compress_btn.grid(row=2, column=3, padx=(4, 4), pady=(2, 12), sticky="e")

        self._cancel_btn = theme.secondary_button(
            bottom_frame,
            tr("app.cancel"),
            self._on_cancel_extract,
            is_dark=is_dark,
            width=104,
            height=40,
            font_size=13,
            border_color=theme.pick(theme.TEXT_ERROR, is_dark),
            text_color=theme.pick(theme.TEXT_ERROR, is_dark),
        )
        self._cancel_btn.grid(row=2, column=4, padx=(4, 4), pady=(2, 12), sticky="e")
        self._cancel_btn.grid_remove()  # 解凍中のみ表示

        self._extract_btn = theme.primary_button(
            bottom_frame,
            tr("app.extract"),
            self._on_extract,
            width=104,
            height=40,
            font_size=14,
            bold=True,
        )
        self._extract_btn.configure(state="disabled")
        self._extract_btn.grid(row=2, column=5, padx=(4, 16), pady=(2, 12), sticky="e")

        # 初期状態: 圧縮表示、解凍非表示
        self._extract_btn.grid_remove()

    @staticmethod
    def _resolve_mode() -> bool:
        """現在の実際の外観モードがdarkかどうかを返す（systemを解決）"""
        return theme.is_dark()

    def _ensure_icons(self, is_dark: bool) -> None:
        """テーマに合わせてツリー用アイコンを生成する（失敗時はNoneでフォールバック）"""
        try:
            self._icon_folder, self._icon_file = _make_entry_icons(is_dark)
        except Exception:  # pragma: no cover - ヘッドレス環境等で画像生成不可
            self._icon_folder = None
            self._icon_file = None

    def _apply_tree_style(self) -> None:
        """外観モードに合わせてファイル一覧(Treeview)の色を設定"""
        is_dark = self._resolve_mode()
        self._ensure_icons(is_dark)
        style = ttk.Style()
        style.theme_use("clam")
        if is_dark:
            bg, fg, heading_bg = (
                theme.TREE_DARK_BG,
                theme.TREE_DARK_FG,
                theme.TREE_DARK_HEADER,
            )
            selected_bg, selected_fg = (
                theme.TREE_DARK_SELECT_BG,
                theme.TREE_DARK_SELECT_FG,
            )
            heading_active = theme.TREE_DARK_HEADER_ACTIVE
        else:
            bg, fg, heading_bg = (
                theme.TREE_LIGHT_BG,
                theme.TREE_LIGHT_FG,
                theme.TREE_LIGHT_HEADER,
            )
            selected_bg, selected_fg = (
                theme.TREE_LIGHT_SELECT_BG,
                theme.TREE_LIGHT_SELECT_FG,
            )
            heading_active = theme.TREE_LIGHT_HEADER_ACTIVE
        # ttk にはフォント指定タプルを渡す（CTkFont を渡すと一時オブジェクトの
        # GC で名前付きフォントが削除され Tk デフォルトへフォールバックするため）。
        # 正の size はピクセル単位（負はポイント）。
        style.configure(
            "Treeview",
            background=bg,
            foreground=fg,
            fieldbackground=bg,
            borderwidth=0,
            rowheight=theme.TREE_ROW_HEIGHT,
            font=(theme.TREE_FONT_FAMILY, theme.TREE_FONT_SIZE),
        )
        style.configure(
            "Treeview.Heading",
            background=heading_bg,
            foreground=fg,
            relief="flat",
            font=(theme.TREE_FONT_FAMILY, theme.TREE_FONT_SIZE, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", selected_bg)],
            foreground=[("selected", selected_fg)],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", heading_active)],
        )

    def _start_theme_poll(self) -> None:
        """systemモード時にOSテーマ変更を検出してスタイルを更新"""
        self._stop_theme_poll()
        if ctk.get_appearance_mode().lower() != "system":
            return
        self._tree_last_dark = self._resolve_mode()
        self._tree_poll_id = self.after(2000, self._poll_appearance_mode)

    def _stop_theme_poll(self) -> None:
        if self._tree_poll_id is not None:
            self.after_cancel(self._tree_poll_id)
            self._tree_poll_id = None

    def _poll_appearance_mode(self) -> None:
        is_dark = self._resolve_mode()
        if is_dark != self._tree_last_dark:
            self._tree_last_dark = is_dark
            self._apply_tree_style()
        self._tree_poll_id = self.after(2000, self._poll_appearance_mode)

    # ---- イベントハンドラ ----

    def _set_status(self, text: str, kind: str = "normal") -> None:
        """ステータス表示を更新し、種別に応じて文字色を変える（normal/error/warn/success）"""
        self._status_var.set(text)
        colors = {
            "error": theme.TEXT_ERROR,
            "warn": theme.TEXT_WARN,
            "success": theme.TEXT_SUCCESS,
        }
        if kind in colors:
            self._status_label.configure(
                text_color=theme.pick(colors[kind], self._resolve_mode())
            )

    def _on_theme_changed(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self._apply_tree_style()
        self._start_theme_poll()
        self._settings.set("theme", mode)

    def _on_language_changed(self, lang: str) -> None:
        """言語切替を適用してUIを再表示する（保存は設定ダイアログ側で実施）"""
        set_language(lang)
        self._retranslate()

    def _retranslate(self) -> None:
        """言語切替後に静的テキストを現在言語で再設定する"""
        self._header_subtitle.configure(text=tr("app.subtitle"))
        self._settings_btn.configure(text=tr("app.settings"))
        self._archive_label.configure(text=tr("app.archive_label"))
        self._browse_btn.configure(text=tr("app.open"))
        self._drop_label.configure(text=tr("app.drop_hint"))
        self._drop_sub_label.configure(text=tr("app.drop_sub"))
        self._contents_label.configure(text=tr("app.contents"))
        self._search_entry.configure(placeholder_text=tr("app.search_placeholder"))
        self._tree.heading("name", text=tr("tree.name"))
        self._tree.heading("size", text=tr("tree.size"))
        self._tree.heading("compressed", text=tr("tree.compressed"))
        self._tree.heading("date", text=tr("tree.date"))
        self._dest_label.configure(text=tr("app.dest_label"))
        self._dest_btn.configure(text=tr("app.browse"))
        self._open_check.configure(text=tr("app.open_folder_on_done"))
        self._close_check.configure(text=tr("app.close_on_done"))
        self._compress_btn.configure(text=tr("app.compress"))
        self._extract_btn.configure(text=tr("app.extract"))
        self._cancel_btn.configure(text=tr("app.cancel"))
        self._refresh_recent_menu()

    def _on_open_settings(self) -> None:
        """設定ダイアログを開く"""
        SettingsDialog(
            parent=self,
            settings=self._settings,
            on_theme_changed=self._on_theme_changed,
            on_language_changed=self._on_language_changed,
        )

    def _on_recent_selected(self, name: str) -> None:
        if name == tr("app.recent_files"):
            return
        path = Path(name)
        if path.exists():
            self._load_archive(path)

    def _refresh_recent_menu(self) -> None:
        files = self._settings.get("recent_files", [])
        if files:
            display_files = [_truncate_path(f) for f in files]
            self._recent_menu.configure(values=display_files)
            self._recent_var.set(
                display_files[0] if len(display_files) == 1 else tr("app.recent_files")
            )

    def _on_drag_enter(self, _event: object = None) -> None:
        self._highlight_drop(True)

    def _on_drag_leave(self, _event: object = None) -> None:
        self._highlight_drop(False)

    def _highlight_drop(self, highlight: bool) -> None:
        """ドロップゾーンをドラッグ中ハイライト表示（枠色＋背景塗り）"""
        is_dark = self._resolve_mode()
        try:
            if highlight:
                self._drop_frame.configure(
                    border_color=theme.pick(theme.DROP_HIGHLIGHT, is_dark),
                    fg_color=theme.pick(theme.ACCENT_SOFT, is_dark),
                )
            else:
                self._drop_frame.configure(
                    border_color=theme.pick(theme.DROP_BORDER, is_dark),
                    fg_color="transparent",
                )
        except AttributeError:
            pass

    def _on_drop(self, event: object) -> None:
        """ドラッグ&ドロップでファイルを受け取る（複数ファイル対応）

        アーカイブは読み込み、それ以外は圧縮候補として追加
        """
        raw = getattr(event, "data", "")
        if not raw:
            return
        paths = [p.strip("{}") for p in re.findall(r"\{[^}]+\}|\S+", raw)]
        loaded = False
        compress_candidates: list[Path] = []
        for p in paths:
            path = Path(p)
            if not path.exists():
                continue
            if is_supported(path) and self._zip_path is None:
                if loaded:
                    self._add_to_queue(path)
                else:
                    self._load_archive(path)
                    loaded = True
            elif not is_supported(path):
                # 非アーカイブ → 圧縮候補
                compress_candidates.append(path)
        if compress_candidates and self._zip_path is None:
            self._compress_sources = compress_candidates
            self._status_var.set(
                tr("msg.compress_candidates").format(n=len(compress_candidates))
            )
            self._start_compress_flow()
        if loaded:
            self._update_queue_status()

    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(
            title=tr("dialog.open_archive"),
            filetypes=[
                (tr("filetype.archive"), "*.zip *.rar *.7z"),
                ("ZIP", "*.zip"),
                ("RAR", "*.rar"),
                ("7z", "*.7z"),
                (tr("filetype.all"), "*.*"),
            ],
        )
        if not path:
            return
        self._load_archive(Path(path))

    def _add_to_queue(self, path: Path) -> None:
        """アーカイブをキューに追加し、暗号化状態を確認する"""
        try:
            _, is_encrypted = list_archive(path)
        except (BadZipFile, OSError, RuntimeError, ExtractionFailedError):
            is_encrypted = False
        self._archive_queue.append((path, is_encrypted))

    def _update_queue_status(self) -> None:
        q = len(self._archive_queue)
        if q > 0:
            current = self._status_var.get()
            self._status_var.set(tr("msg.queue_status").format(q=q, current=current))

    def _load_archive(self, path: Path) -> None:
        # ヘッダー暗号化（7z等）はオープン時にパスワードが必要
        password = self._settings.get_password(str(path))
        for _attempt in range(3):
            try:
                self._entries, self._is_encrypted = list_archive(
                    path, _password=password
                )
                break
            except (PasswordRequiredError, InvalidPasswordError):
                password = self._ask_password_for(path)
                if password is None:
                    self._set_status(tr("msg.load_canceled"), kind="warn")
                    return
                self._settings.set_password(str(path), password)
            except (BadZipFile, OSError, RuntimeError, ExtractionFailedError) as e:
                self._set_status(tr("msg.error_open").format(e=e), kind="error")
                self._entries = []
                self._refresh_tree()
                self._show_drop_zone()
                self._extract_btn.configure(state="disabled")
                return
        else:
            # 3回試行してもパスワードで開けなかった
            self._set_status(tr("msg.error_open").format(e="password"), kind="error")
            self._entries = []
            self._refresh_tree()
            self._show_drop_zone()
            self._extract_btn.configure(state="disabled")
            return

        # 新しいアーカイブを開くとき、前の一時展開をクリーンアップ
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

        self._zip_path = path
        # キューを初期化（最初のアーカイブの暗号化状態は既に判明している）
        self._archive_queue = [(path, self._is_encrypted)]
        self._search_var.set("")

        self._path_var.set(str(path))
        self._settings.add_recent_file(str(path))
        self._refresh_recent_menu()

        self._dest_var.set(str(self._default_dest(path)))

        total_size = sum(e.size for e in self._entries)
        self._refresh_tree()
        self._show_file_list()
        self._compress_btn.grid_remove()
        self._extract_btn.grid()
        self._extract_btn.configure(state="normal")
        self._status_var.set(
            tr("msg.entries").format(
                n=len(self._entries), size=_format_size(total_size)
            )
            + (tr("msg.password_protected") if self._is_encrypted else "")
        )

    def _default_dest(self, path: Path) -> Path:
        """dest_mode設定に応じた展開先のデフォルトを決める

        archive: アーカイブと同じフォルダー/ファイル名
        last:    最後に使用したフォルダー（無効ならアーカイブ基準にフォールバック）
        fixed:   固定フォルダー（無効ならアーカイブ基準にフォールバック）
        """
        dest_mode = self._settings.get("dest_mode", "archive")
        if dest_mode == "last":
            saved = self._settings.get("last_dest", "")
        elif dest_mode == "fixed":
            saved = self._settings.get("fixed_dest", "")
        else:
            saved = ""
        if saved and Path(saved).is_dir():
            return Path(saved)
        return path.parent / path.stem

    def _refresh_tree(self) -> None:
        for row in self._tree.get_children():
            self._tree.delete(row)
        query = self._search_var.get().strip().lower()
        filtered = (
            [e for e in self._entries if not query or query in e.name.lower()]
            if query
            else self._entries
        )
        folder_icon = self._icon_folder
        file_icon = self._icon_file
        for i, e in enumerate(filtered, start=1):
            values = (
                i,
                e.name,
                _format_size(e.size),
                _format_size(e.compressed_size),
                e.modified.strftime("%Y-%m-%d %H:%M"),
            )
            # フォルダ/ファイルのアイコンは #0 列に表示（未生成時は画像なし）
            image = (folder_icon if e.is_dir else file_icon) or ""
            self._tree.insert("", "end", values=values, image=image)

    def _on_tree_select(self, _event: object = None) -> None:
        if self._zip_path is None:
            return
        sel = self._tree.selection()
        if not sel:
            return
        values = self._tree.item(sel[0], "values")
        if not values or len(values) < 2:
            return
        entry_name: str = values[1]
        self._show_preview(entry_name)

    def _on_search_keyrelease(self, _event: object = None) -> None:
        """検索ボックスに入力があるたびにツリービューを絞り込む"""
        self._refresh_tree()

    def _show_preview(self, name: str) -> None:
        self._preview_frame.grid_forget()
        self._preview_label.configure(text="")
        self._current_image = None

        # サイズ上限チェック（設定の preview_max_size）
        entry = None
        for e in self._entries:
            if e.name == name:
                entry = e
                break
        if (
            entry is not None
            and entry.size > self._archive_service.safety_limits.preview_max_size
        ):
            self._preview_label.configure(
                text=tr("msg.preview_too_large").format(size=_format_size(entry.size))
            )
            self._preview_frame.grid(
                row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
            )
            return

        ext = Path(name).suffix.lower()
        if ext in _TEXT_EXTENSIONS:
            self._preview_text(name)
        elif ext in _IMAGE_EXTENSIONS:
            self._preview_image(name)
        else:
            self._preview_label.configure(
                text=tr("msg.preview_unavailable").format(ext=ext)
            )
            self._preview_frame.grid(
                row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
            )

    def _preview_text(self, name: str) -> None:
        assert self._zip_path is not None
        try:
            data = self._archive_service.read_entry(
                self._zip_path,
                name,
                password=self._settings.get_password(str(self._zip_path)),
            )
        except (IOError, OSError, KeyError, BadZipFile, ExtractionFailedError) as e:
            self._preview_label.configure(text=tr("msg.preview_read_error").format(e=e))
            self._preview_frame.grid(
                row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
            )
            return
        if data is None:
            self._preview_label.configure(
                text=tr("msg.preview_read_error").format(e="")
            )
            self._preview_frame.grid(
                row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
            )
            return
        # UTF-8優先、失敗時はシステムエンコーディング（日本語CP932など）で再試行
        text = _decode_text(data)
        self._preview_label.configure(text=text)
        self._preview_frame.grid(
            row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
        )

    def _preview_image(self, name: str) -> None:
        assert self._zip_path is not None
        try:
            data = self._archive_service.read_entry(
                self._zip_path,
                name,
                password=self._settings.get_password(str(self._zip_path)),
            )
            if data is None:
                raise IOError("no data")
            img = Image.open(io.BytesIO(data))
            img.thumbnail(_MAX_IMAGE_DIMENSION)
            ctk_img = ctk.CTkImage(img, size=img.size)
            self._preview_label.configure(image=ctk_img, text="")
            self._current_image = ctk_img
        except (IOError, OSError, KeyError, BadZipFile, ExtractionFailedError) as e:
            self._preview_label.configure(
                text=tr("msg.preview_image_error").format(e=e)
            )
            self._preview_frame.grid(
                row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
            )
            return
        self._preview_frame.grid(
            row=2, column=0, columnspan=2, padx=14, pady=(0, 14), sticky="ew"
        )

    def _show_drop_zone(self) -> None:
        self._list_frame.grid_forget()
        self._drop_frame.grid(row=2, column=0, padx=24, pady=6, sticky="nsew")
        self._compress_btn.grid()
        self._compress_btn.configure(state="normal")
        self._extract_btn.grid_remove()
        self._extract_btn.configure(state="disabled")

    def _show_file_list(self) -> None:
        self._drop_frame.grid_forget()
        self._list_frame.grid(row=2, column=0, padx=24, pady=6, sticky="nsew")

    def _on_dest_browse(self) -> None:
        path = filedialog.askdirectory(title=tr("dialog.choose_dest"))
        if path:
            self._dest_var.set(path)
            self._settings.set("last_dest", path)

    def _on_extract(self) -> None:
        if self._extracting or not self._archive_queue:
            return

        dest = Path(self._dest_var.get()) if self._dest_var.get() else Path.cwd()

        # メインスレッドで全暗号化アーカイブのパスワードを取得
        passwords: dict[Path, str] = {}
        for archive_path, is_encrypted in self._archive_queue:
            if not is_encrypted:
                continue
            pw = self._settings.get_password(str(archive_path))
            if pw is None:
                pw = self._ask_password_for(archive_path)
                if pw is None:
                    return  # ユーザーがキャンセル
                self._settings.set_password(str(archive_path), pw)
            passwords[archive_path] = pw

        self._extracting = True
        self._set_ui_enabled(False)
        self._progress.set(0)
        self._extracted_dests = []
        self._cancel_btn.grid()  # 解凍中はキャンセルボタンを表示

        # キューからパスのみ抽出してワーカーに渡す
        paths_copy = [p for p, _ in self._archive_queue]
        zip_path_copy = self._zip_path

        self._worker = ExtractWorker(
            paths_copy,
            dest,
            passwords=passwords,
            active_zip_path=zip_path_copy,
            on_progress=self._on_extract_progress,
        )
        Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        """ワーカースレッドでバッチ解凍を実行し、完了をUIスレッドに通知"""
        assert self._worker is not None
        self.after(0, self._progress.grid)
        self.after(0, lambda: self._progress.set(0))
        result = self._worker.run()
        self.after(0, lambda: self._on_extract_done(result))

    def _on_extract_progress(
        self,
        idx: int,
        total: int,
        name: str,
        pct: float,
        current: int,
        total_count: int,
        current_name: str,
    ) -> None:
        """ワーカースレッドからの進捗をUIスレッドに転送"""
        self.after(0, lambda p=pct: self._progress.set(p))
        name_part = f" - {current_name}" if current_name else ""
        message = (
            f"[{idx}/{total}] {name}: {pct:.0%} ({current}/{total_count}){name_part}"
        )
        self.after(0, lambda s=message: self._status_var.set(s))

    def _on_cancel_extract(self) -> None:
        """解凍を中断する"""
        if self._worker is not None:
            self._worker.cancel()
        self._set_status(tr("msg.canceling"), kind="warn")

    def _on_extract_done(self, result: ExtractResult) -> None:
        self._extracting = False
        self._set_ui_enabled(True)
        self._cancel_btn.grid_remove()  # 解凍終了でキャンセルボタンを隠す
        self._extracted_dests = list(result.extracted_dests)

        # キャンセル時
        if result.canceled:
            self._set_status(
                tr("msg.canceled").format(n=result.success_count), kind="warn"
            )
            self._progress.set(0)
            self._progress.grid_remove()
            self._show_drop_zone()
            return

        # エラーあり: 件数と先頭のエラーを表示
        if result.errors:
            first_error = result.errors[0]
            summary = tr("msg.error_summary").format(n=result.error_count)
            detail = f"{first_error.archive_name}: {first_error.message}"
            self._set_status(
                tr("msg.error_prefix").format(msg=f"{summary} - {detail}"), kind="error"
            )
            self._progress.set(1)
            self._progress.grid_remove()
            self._show_drop_zone()
            return

        n = len(self._archive_queue)
        self._set_status(tr("msg.extract_done").format(n=n), kind="success")
        last_zip = self._zip_path
        self._zip_path = None
        self._archive_queue = []
        saved_items: dict[str, object] = {
            "open_on_done": self._open_on_done_var.get(),
            "close_on_done": self._close_on_done_var.get(),
        }
        # 固定フォルダーモードでは last_dest を更新しない（固定先は設定ダイアログで変更）
        if self._settings.get("dest_mode", "archive") != "fixed":
            saved_items["last_dest"] = self._dest_var.get()
        self._settings.set_many(saved_items)
        self._settings.clear_passwords()
        self._progress.set(1)
        self._progress.grid_remove()
        if self._open_on_done_var.get() and last_zip is not None:
            # 実際の展開先を開く（1つの場合はそのフォルダ、複数・未確定は基準フォルダ）
            if len(self._extracted_dests) == 1:
                dest = self._extracted_dests[0]
            else:
                dest = (
                    Path(self._dest_var.get())
                    if self._dest_var.get()
                    else last_zip.parent
                )
            if sys.platform == "win32" and dest.is_dir():
                subprocess.Popen(["explorer", str(dest)])
        if self._close_on_done_var.get():
            self.after(500, self.destroy)
            return
        self._show_drop_zone()

    def _set_ui_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._browse_btn.configure(state=state)
        self._dest_btn.configure(state=state)
        self._extract_btn.configure(state=state)
        self._compress_btn.configure(state=state)

    def _ask_password_for(self, archive_path: Path | None) -> str | None:
        """指定アーカイブ用のパスワード入力ダイアログを表示"""
        name = archive_path.name if archive_path else tr("app.archive_label")
        dialog = ctk.CTkInputDialog(
            title=tr("dialog.password"),
            text=tr("msg.password_prompt").format(name=name),
        )
        result = dialog.get_input()
        return result if result else None

    # ---- 圧縮機能 ----

    def _on_compress(self) -> None:
        """ファイル/フォルダを圧縮"""
        paths = filedialog.askopenfilenames(title=tr("dialog.compress_files"))
        if not paths:
            return
        self._compress_sources = [Path(p) for p in paths]
        self._start_compress_flow()

    def _start_compress_flow(self) -> None:
        """圧縮ファイル保存ダイアログ＋実行"""
        if not self._compress_sources:
            return

        if self._compress_no_dialog:
            # フラグはここで消費せず、_on_compress_done / _on_compress_error が
            # 自動クローズ（destroy）の判定に使うまで保持する
            first = self._compress_sources[0]
            output = first.parent / (first.stem + ".zip")
            self._start_compress(output)
            return

        first = self._compress_sources[0]
        default_name = first.stem + ".zip"
        default_dir = str(first.parent) if first.parent != Path() else "."
        output = filedialog.asksaveasfilename(
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
        if not output:
            return

        self._start_compress(Path(output))

    def _start_compress(self, output: Path) -> None:
        """圧縮を開始（共通）"""
        # 自己包含チェック（出力先が圧縮対象に含まれていないか）
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
            args=(list(self._compress_sources), output),
            daemon=True,
        ).start()

    def _do_compress(self, sources: list[Path], output: Path) -> None:
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
            )
            self.after(0, self._on_compress_done)
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self._on_compress_error(msg))

    def _on_compress_done(self) -> None:
        self._compressing = False
        self._set_status(tr("msg.compress_done"), kind="success")
        self._progress.set(1)
        self._progress.grid_remove()
        self._compress_sources = []
        if self._compress_no_dialog:
            self.after(500, self.destroy)
        else:
            self._set_ui_enabled(True)

    def _on_compress_error(self, msg: str) -> None:
        self._compressing = False
        self._set_status(tr("msg.error_prefix").format(msg=msg), kind="error")
        self._progress.set(0)
        self._progress.grid_remove()
        if self._compress_no_dialog:
            self._compress_no_dialog = False
            self.after(2000, self.destroy)
        else:
            self._set_ui_enabled(True)


def _decode_text(data: bytes, max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    """バイトデータをテキストにデコードする（UTF-8優先→システムエンコーディング）

    日本語WindowsのCP932など、UTF-8以外のエンコーディングで書かれた
    テキストファイルにも対応するため2段階フォールバックする。
    """
    try:
        return data.decode("utf-8")[:max_chars]
    except UnicodeDecodeError:
        pass
    # システムエンコーディング（日本語ならCP932）で再試行
    try:
        return data.decode(locale.getencoding())[:max_chars]
    except (UnicodeDecodeError, LookupError):
        pass
    # 最終手段: 非ASCIIを置換
    return data.decode("utf-8", errors="replace")[:max_chars]


def _read_archive_entry(
    archive_path: Path | str,
    name: str,
    cache_dir: str | None = None,
    max_bytes: int = _MAX_PREVIEW_BYTES,
) -> bytes:
    """アーカイブ内の1エントリを読み込む (ArchiveServiceに委譲)。

    ZIPはサイズ上限チェック（メモリ保護）を先に行い、超過時は空を返す。
    未登録エントリは KeyError を送出する。
    ZIP/RAR/7z の実読み取りは ArchiveService（エンコーディング・安全上限・
    パスワード対応込み）に委譲し、読み込めない場合は空バイトを返す。
    """
    p = Path(archive_path)
    if p.suffix.lower() == ".zip":
        import zipfile

        try:
            with zipfile.ZipFile(p) as zf:
                info = zf.getinfo(name)
                if info.file_size > max_bytes:
                    return b""
        except (zipfile.BadZipFile, OSError):
            # 壊れたZIPは read_entry に委譲し、後段でエラー時は空を返す
            pass
    service = ArchiveService()
    try:
        result = service.read_entry(archive_path, name)
    except Exception:
        return b""
    return result if result is not None else b""


def _truncate_path(path: str, max_len: int = 60) -> str:
    """長いパスを省略表示

    収まらない場合はファイル名を優先し、親パスの末尾だけを残して
    先頭を "..." で省略する。戻り値の長さは max_len 以下を保証する。
    """
    if len(path) <= max_len:
        return path
    p = Path(path)
    name = p.name
    if len(name) >= max_len - 3:
        # ファイル名すら "..." と一緒に収まらない → 名前側を省略
        return name[: max_len - 3] + "..."
    # "..."(3) + name の残りが親パス表示に使える文字数
    remain = max_len - len(name) - 3
    parent = str(p.parent)
    if remain < 2:
        # セパレータ "\\"(1文字) を表示する余白がない → 親パスは省略
        return "..." + name
    # 親パスの末尾から remain-1 文字（"\\" の1文字分を確保）
    return "..." + parent[-(remain - 1) :] + "\\" + name


def _draw_folder_icon(is_dark: bool, size: int = 16) -> Image.Image:
    """フォルダアイコン（RGBA）を描画する

    高解像度で描画してから縮小することで、小さなサイズでも輪郭を滑らかに保つ。
    """
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#5b9bd5" if is_dark else "#3b82f6"
    edge = "#4a8ad9" if is_dark else "#2f6ee0"
    # タブ
    draw.rounded_rectangle(
        [s * 0.10, s * 0.22, s * 0.44, s * 0.40],
        radius=s * 0.06,
        fill=color,
    )
    # 本体
    draw.rounded_rectangle(
        [s * 0.06, s * 0.32, s * 0.94, s * 0.88],
        radius=s * 0.08,
        fill=color,
    )
    # 下部にわずかに濃い面を重ねて立体感を出す
    draw.rounded_rectangle(
        [s * 0.06, s * 0.72, s * 0.94, s * 0.88],
        radius=s * 0.08,
        fill=edge,
    )
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _draw_file_icon(is_dark: bool, size: int = 16) -> Image.Image:
    """ファイルアイコン（RGBA）を描画する"""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = "#f4f6fa" if not is_dark else "#2a303e"
    border = "#8a94a6" if not is_dark else "#9aa4b4"
    line = "#9aa4b4" if not is_dark else "#8f9bad"
    # ページ本体（右上に折り返し）
    draw.rounded_rectangle(
        [s * 0.12, s * 0.06, s * 0.88, s * 0.94],
        radius=s * 0.06,
        fill=fill,
        outline=border,
        width=max(1, s // 20),
    )
    # 折り返しの三角
    draw.polygon(
        [(s * 0.88, s * 0.28), (s * 0.72, s * 0.12), (s * 0.88, s * 0.12)],
        fill=border,
    )
    # テキスト行（幅を変えて文字列らしく）
    for y in (s * 0.30, s * 0.46, s * 0.62):
        draw.rounded_rectangle(
            [s * 0.28, y, s * 0.72, y + s * 0.06],
            radius=s * 0.02,
            fill=line,
        )
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _make_entry_icons(is_dark: bool) -> tuple[ImageTk.PhotoImage, ImageTk.PhotoImage]:
    """ツリー表示用のフォルダ/ファイルアイコンを Tk 画像として生成する"""
    return (
        ImageTk.PhotoImage(_draw_folder_icon(is_dark)),
        ImageTk.PhotoImage(_draw_file_icon(is_dark)),
    )


_CONTEXT_EXTENSIONS = [".zip", ".rar", ".7z"]


def _get_exe_path() -> Path:
    """kaito実行ファイルのパスを返す"""
    if getattr(sys, "frozen", False):  # PyInstallerビルド
        return Path(sys.executable)
    # 開発環境: リポジトリ直下の dist/kaito.exe を使う。
    dev_exe = Path(__file__).parents[3] / "dist" / "kaito.exe"
    if dev_exe.exists():
        return dev_exe
    raise FileNotFoundError(
        "dist/kaito.exe が見つかりません。コンテキストメニュー登録前に kaito.exe をビルドしてください。"
    )


def install_context_menu() -> None:
    """Windowsコンテキストメニューにkaitoを登録"""
    exe = _get_exe_path()
    exe_str = f'"{exe}"'
    base = r"Software\Classes"

    # 解凍: SystemFileAssociations (標準状態で有効) + * (カスタムProgID対策)
    for ext in _CONTEXT_EXTENSIONS:
        key_path = f"{base}\\SystemFileAssociations\\{ext}\\shell\\kaito_extract"
        with CreateKeyEx(HKEY_CURRENT_USER, key_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, tr("ctx.extract"))
        cmd_path = f"{key_path}\\command"
        with CreateKeyEx(HKEY_CURRENT_USER, cmd_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, f'{exe_str} "%1"')
    # 全ファイルにも登録（例: CubeICE などが ProgID を乗っ取っている場合の救済）
    for shell_root in [f"{base}\\*"]:
        key_path = f"{shell_root}\\shell\\kaito_extract"
        with CreateKeyEx(HKEY_CURRENT_USER, key_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, tr("ctx.extract"))
        cmd_path = f"{key_path}\\command"
        with CreateKeyEx(HKEY_CURRENT_USER, cmd_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, f'{exe_str} "%1"')

    # 圧縮: ファイル・フォルダ
    for shell_root in [f"{base}\\*", f"{base}\\Directory"]:
        key_path = f"{shell_root}\\shell\\kaito_compress"
        with CreateKeyEx(HKEY_CURRENT_USER, key_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, tr("ctx.compress"))
        cmd_path = f"{key_path}\\command"
        with CreateKeyEx(HKEY_CURRENT_USER, cmd_path, 0, KEY_SET_VALUE) as key:
            SetValueEx(key, None, 0, REG_SZ, f'{exe_str} --compress "%1"')

    print(tr("ctx.installed"))


def _delete_key_recursive(root_key: object, sub_key: str) -> None:  # type: ignore[type-arg]
    """レジストリキーをサブキーごと削除する"""
    try:
        with OpenKey(root_key, sub_key, 0, KEY_SET_VALUE) as key:  # type: ignore[arg-type]
            info = QueryInfoKey(key)
            for _ in range(info[0]):
                child = EnumKey(key, 0)
                _delete_key_recursive(key, child)
        DeleteKey(root_key, sub_key)  # type: ignore[arg-type]
    except FileNotFoundError:
        pass
    except OSError:
        pass


def uninstall_context_menu() -> None:
    """Windowsコンテキストメニューからkaitoを削除"""
    base = r"Software\Classes"

    for ext in _CONTEXT_EXTENSIONS:
        _delete_key_recursive(
            HKEY_CURRENT_USER,
            f"{base}\\SystemFileAssociations\\{ext}\\shell\\kaito_extract",
        )
    _delete_key_recursive(HKEY_CURRENT_USER, f"{base}\\*\\shell\\kaito_extract")

    for shell_root in [f"{base}\\*", f"{base}\\Directory"]:
        _delete_key_recursive(HKEY_CURRENT_USER, f"{shell_root}\\shell\\kaito_compress")

    print(tr("ctx.removed"))


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024**2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    else:
        return f"{size / 1024**3:.1f} GB"


def main() -> None:
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
        p = Path(args[1])
        if p.exists():
            cli_compress_path = p
    elif args:
        p = Path(args[0])
        if is_supported(p) and p.exists():
            cli_path = p

    app = UnzipApp(cli_path=cli_path, cli_compress_path=cli_compress_path)
    app.mainloop()  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    main()
