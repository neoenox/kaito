"""Windows Explorer context-menu registration for kaito."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_CONTEXT_EXTENSIONS = (".zip", ".rar", ".7z")


def _get_exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)

    project_root = Path(__file__).resolve().parents[2]
    development_exe = project_root / "dist" / "kaito.exe"
    if development_exe.exists():
        return development_exe

    raise FileNotFoundError(
        "dist/kaito.exe が見つかりません。"
        "コンテキストメニュー登録前に kaito.exe をビルドしてください。"
    )


def install_context_menu() -> None:
    """Register extraction, integrity-test, and compression actions per user."""
    try:
        from winreg import (  # type: ignore[attr-defined]
            CreateKeyEx,
            HKEY_CURRENT_USER,
            KEY_SET_VALUE,
            REG_SZ,
            SetValueEx,
        )
    except ImportError:
        return

    executable = f'"{_get_exe_path()}"'
    base = r"Software\Classes"
    for extension in _CONTEXT_EXTENSIONS:
        association = f"{base}\\SystemFileAssociations\\{extension}\\shell"
        _write_action(
            HKEY_CURRENT_USER,
            CreateKeyEx,
            SetValueEx,
            KEY_SET_VALUE,
            REG_SZ,
            f"{association}\\kaito_extract",
            "kaitoで解凍",
            f'{executable} "%1"',
        )
        _write_action(
            HKEY_CURRENT_USER,
            CreateKeyEx,
            SetValueEx,
            KEY_SET_VALUE,
            REG_SZ,
            f"{association}\\kaito_test",
            "kaitoで整合性を検査",
            f'{executable} --test-archive "%1"',
        )

    for root in (f"{base}\\*\\shell", f"{base}\\Directory\\shell"):
        _write_action(
            HKEY_CURRENT_USER,
            CreateKeyEx,
            SetValueEx,
            KEY_SET_VALUE,
            REG_SZ,
            f"{root}\\kaito_compress",
            "kaitoで圧縮",
            f'{executable} --compress "%1"',
        )


def _write_action(
    root: Any,
    create_key: Any,
    set_value: Any,
    key_access: int,
    string_type: int,
    key_path: str,
    label: str,
    command: str,
) -> None:
    with create_key(root, key_path, 0, key_access) as key:
        set_value(key, None, 0, string_type, label)
    with create_key(root, f"{key_path}\\command", 0, key_access) as key:
        set_value(key, None, 0, string_type, command)


def uninstall_context_menu() -> None:
    """Remove every per-user Explorer action registered by kaito."""
    try:
        from winreg import HKEY_CURRENT_USER
    except ImportError:
        return

    base = r"Software\Classes"
    for extension in _CONTEXT_EXTENSIONS:
        association = f"{base}\\SystemFileAssociations\\{extension}\\shell"
        _delete_key_recursive(HKEY_CURRENT_USER, f"{association}\\kaito_extract")
        _delete_key_recursive(HKEY_CURRENT_USER, f"{association}\\kaito_test")
    for root in (f"{base}\\*\\shell", f"{base}\\Directory\\shell"):
        _delete_key_recursive(HKEY_CURRENT_USER, f"{root}\\kaito_compress")


def _delete_key_recursive(root_key: Any, sub_key: str) -> None:
    try:
        from winreg import (  # type: ignore[attr-defined]
            DeleteKey,
            EnumKey,
            KEY_ALL_ACCESS,
            OpenKey,
            QueryInfoKey,
        )
    except ImportError:
        return
    try:
        with OpenKey(root_key, sub_key, 0, KEY_ALL_ACCESS) as key:
            child_count = QueryInfoKey(key)[0]
            for _ in range(child_count):
                _delete_key_recursive(key, EnumKey(key, 0))
        DeleteKey(root_key, sub_key)
    except (FileNotFoundError, OSError):
        pass


__all__ = ["install_context_menu", "uninstall_context_menu"]
