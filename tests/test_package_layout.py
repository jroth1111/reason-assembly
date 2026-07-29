import importlib
from pathlib import Path
from zipfile import ZipFile


def test_legacy_source_shim_aliases_package_module():
    legacy = importlib.import_module("routing")
    packaged = importlib.import_module("reason_assembly.routing")
    assert legacy is packaged


def test_built_wheel_has_no_top_level_python_modules():
    wheels = list((Path(__file__).parents[1] / "dist").glob("*.whl"))
    if not wheels:
        return
    with ZipFile(wheels[0]) as archive:
        bare = [name for name in archive.namelist() if name.endswith(".py") and "/" not in name]
    assert bare == []
