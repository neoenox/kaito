"""Windowsコンテキストメニュー登録/削除のwinregモック単体テスト。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kaito.context_menu import (
    _delete_key_recursive,
    _get_exe_path,
    install_context_menu,
    uninstall_context_menu,
)

_ROOT = (
    0x80000001  # HKEY_CURRENT_USER の数値（実レジストリへ触れないよう生の定数を渡す）
)
_EXE = Path("C:/kaito/kaito.exe")
# str(Path) は Windows でバックスラッシュへ正規化されるため、期待値は Path から導出する。
_QUOTED_EXE = f'"{_EXE}"'


def _key_paths() -> set[str]:
    """install_context_menu が登録する全レジストリキーを列挙する。"""
    base = r"Software\Classes"
    paths: set[str] = set()
    for extension in (".zip", ".rar", ".7z"):
        association = f"{base}\\SystemFileAssociations\\{extension}\\shell"
        for action in ("kaito_extract", "kaito_test"):
            paths.add(f"{association}\\{action}")
            paths.add(f"{association}\\{action}\\command")
    for root in (f"{base}\\*\\shell", f"{base}\\Directory\\shell"):
        paths.add(f"{root}\\kaito_compress")
        paths.add(f"{root}\\kaito_compress\\command")
    return paths


def _expected_written_pairs() -> set[tuple[str, object]]:
    """install_context_menu が書き込む (キーパス, 値) の全組み合わせ。"""
    expected: set[tuple[str, object]] = set()
    for extension in (".zip", ".rar", ".7z"):
        association = f"Software\\Classes\\SystemFileAssociations\\{extension}\\shell"
        expected.add((f"{association}\\kaito_extract", "kaitoで解凍"))
        expected.add((f"{association}\\kaito_extract\\command", f'{_QUOTED_EXE} "%1"'))
        expected.add((f"{association}\\kaito_test", "kaitoで整合性を検査"))
        expected.add(
            (
                f"{association}\\kaito_test\\command",
                f'{_QUOTED_EXE} --test-archive "%1"',
            )
        )
    for root in (r"Software\Classes\*\shell", r"Software\Classes\Directory\shell"):
        expected.add((f"{root}\\kaito_compress", "kaitoで圧縮"))
        expected.add(
            (f"{root}\\kaito_compress\\command", f'{_QUOTED_EXE} --compress "%1"')
        )
    return expected


class _FakeKey:
    """create_key の返り値。開いたキーのパスを保持して set_value で記録する。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self) -> _FakeKey:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
@patch("kaito.context_menu._get_exe_path", return_value=_EXE)
@patch("winreg.SetValueEx")
@patch("winreg.CreateKeyEx")
def test_install_registers_every_action_on_expected_keys(
    create_key: object,
    set_value: object,
    _get_exe_path: object,
) -> None:
    install_context_menu()

    created = {call.args[1] for call in create_key.call_args_list}  # type: ignore[attr-defined]
    assert created == _key_paths()
    assert create_key.call_count == 16  # type: ignore[attr-defined]

    expected_values = {
        "kaitoで解凍": 3,
        "kaitoで整合性を検査": 3,
        "kaitoで圧縮": 2,
        f'{_QUOTED_EXE} "%1"': 3,
        f'{_QUOTED_EXE} --test-archive "%1"': 3,
        f'{_QUOTED_EXE} --compress "%1"': 2,
    }
    actual: dict[object, int] = {}
    for call in set_value.call_args_list:  # type: ignore[attr-defined]
        value = call.args[4]
        actual[value] = actual.get(value, 0) + 1
    assert actual == expected_values
    assert set_value.call_count == 16  # type: ignore[attr-defined]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
def test_install_writes_label_and_command_to_correct_keys() -> None:
    """ラベルはアクションキーへ、コマンドは command サブキーへ書かれる。"""
    written: list[tuple[str, object]] = []

    def fake_create_key(
        root: object, sub_key: str, reserved: int, access: int
    ) -> _FakeKey:
        return _FakeKey(sub_key)

    def fake_set_value(
        key: _FakeKey, name: object, reserved: int, type_: object, value: object
    ) -> None:
        written.append((key.path, value))

    with (
        patch("winreg.CreateKeyEx", side_effect=fake_create_key),
        patch("winreg.SetValueEx", side_effect=fake_set_value),
        patch("kaito.context_menu._get_exe_path", return_value=_EXE),
    ):
        install_context_menu()

    assert set(written) == _expected_written_pairs()
    assert len(written) == 16


def test_install_returns_silently_without_winreg() -> None:
    """winreg が利用できない環境では何もせず返る。"""
    with patch.dict(sys.modules, {"winreg": None}):
        install_context_menu()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
def test_uninstall_removes_every_registered_action() -> None:
    with patch("kaito.context_menu._delete_key_recursive") as delete:
        uninstall_context_menu()

    removed = {call.args[1] for call in delete.call_args_list}
    assert len(removed) == 8
    for extension in (".zip", ".rar", ".7z"):
        association = f"Software\\Classes\\SystemFileAssociations\\{extension}\\shell"
        assert f"{association}\\kaito_extract" in removed
        assert f"{association}\\kaito_test" in removed
    for root in (r"Software\Classes\*\shell", r"Software\Classes\Directory\shell"):
        assert f"{root}\\kaito_compress" in removed


def test_uninstall_returns_silently_without_winreg() -> None:
    with patch.dict(sys.modules, {"winreg": None}):
        uninstall_context_menu()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
def test_delete_key_recursive_removes_children_before_parent() -> None:
    with (
        patch("winreg.OpenKey"),
        patch("winreg.EnumKey", return_value="child"),
        patch("winreg.QueryInfoKey", side_effect=[(1, 0), (0, 0)]),
        patch("winreg.DeleteKey") as delete,
    ):
        _delete_key_recursive(_ROOT, "Software\\Classes\\parent")

    assert [call.args[1] for call in delete.call_args_list] == [
        "child",
        "Software\\Classes\\parent",
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
def test_delete_key_recursive_deletes_empty_key() -> None:
    with (
        patch("winreg.OpenKey"),
        patch("winreg.QueryInfoKey", return_value=(0, 0)),
        patch("winreg.DeleteKey") as delete,
    ):
        _delete_key_recursive(_ROOT, "Software\\Classes\\empty")

    assert delete.call_args_list == [((_ROOT, "Software\\Classes\\empty"), {})]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry API")
@pytest.mark.parametrize("error", [FileNotFoundError, OSError])
def test_delete_key_recursive_swallows_missing_and_os_errors(
    error: type[OSError],
) -> None:
    with (
        patch("winreg.OpenKey", side_effect=error),
        patch("winreg.DeleteKey") as delete,
    ):
        _delete_key_recursive(_ROOT, "Software\\Classes\\missing")

    delete.assert_not_called()


def test_get_exe_path_returns_project_dist_exe_in_development(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    module_path = repo / "src" / "kaito" / "context_menu.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    development_exe = repo / "dist" / "kaito.exe"
    development_exe.parent.mkdir()
    development_exe.touch()
    fake_sys = SimpleNamespace(
        frozen=False, executable=str(repo / ".venv" / "Scripts" / "python.exe")
    )
    with (
        patch("kaito.context_menu.sys", fake_sys),
        patch("kaito.context_menu.__file__", str(module_path)),
    ):
        result = _get_exe_path()

    assert result == development_exe


def test_get_exe_path_refuses_broken_python_fallback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    module_path = repo / "src" / "kaito" / "context_menu.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    fake_sys = SimpleNamespace(
        frozen=False, executable=str(repo / ".venv" / "Scripts" / "python.exe")
    )
    with (
        patch("kaito.context_menu.sys", fake_sys),
        patch("kaito.context_menu.__file__", str(module_path)),
        pytest.raises(FileNotFoundError, match="dist/kaito.exe"),
    ):
        _get_exe_path()


def test_get_exe_path_uses_executable_when_frozen() -> None:
    fake_sys = SimpleNamespace(
        frozen=True, executable="C:/Program Files/kaito/kaito.exe"
    )
    with patch("kaito.context_menu.sys", fake_sys):
        result = _get_exe_path()

    assert result == Path("C:/Program Files/kaito/kaito.exe")
