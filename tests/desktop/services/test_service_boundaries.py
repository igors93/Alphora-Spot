from pathlib import Path


def test_service_layer_has_no_gui_framework_imports():
    services_dir = Path(__file__).resolve().parents[3] / "src" / "desktop" / "services"
    source = "\n".join(path.read_text(encoding="utf-8") for path in services_dir.glob("*.py"))

    assert "import tkinter" not in source
    assert "from tkinter" not in source
    assert "import PySide6" not in source
    assert "from PySide6" not in source
