"""Reason Assembly public package."""

import sys
from types import ModuleType

from . import reason_assembly as _cli
from .identity import VERSION
from .reason_assembly import *  # noqa: F401,F403

__version__ = VERSION


class _FacadeModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        if not name.startswith("__") and hasattr(_cli, name):
            setattr(_cli, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule


def main() -> None:
    _cli.main()
