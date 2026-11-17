from pathlib import Path

from desktop.runtime_paths import prepare_workspace, resolve_workspace_root


def test_explicit_workspace_has_priority(tmp_path: Path) -> None:
    expected = tmp_path / "custom"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "main.py").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")

    result = resolve_workspace_root(
        source_hint=checkout,
        environ={"ALPHORA_PROJECT_ROOT": str(expected)},
    )
    assert result == expected.resolve()


def test_source_checkout_is_used_when_code_runs_from_it(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    module = tmp_path / "src" / "module.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    assert resolve_workspace_root(source_hint=module, environ={}) == tmp_path.resolve()


def test_installed_package_uses_xdg_data_home_on_linux(tmp_path: Path) -> None:
    result = resolve_workspace_root(
        source_hint=tmp_path / "site-packages" / "alphora" / "module.py",
        environ={"XDG_DATA_HOME": str(tmp_path)},
        platform_name="linux",
        home=tmp_path / "home",
    )
    assert result == (tmp_path / "Alphora").resolve()


def test_prepare_workspace_creates_runtime_directories(tmp_path: Path) -> None:
    root = prepare_workspace(tmp_path / "workspace")
    assert (root / "config").is_dir()
    assert (root / "data" / "market").is_dir()
    assert (root / "models" / "ai" / "global").is_dir()
    assert (root / "reports").is_dir()
    assert (root / "runtime").is_dir()
