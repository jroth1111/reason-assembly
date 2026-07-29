"""Transitional source-checkout shim for reason_assembly.routing."""
import sys
from reason_assembly import routing as _impl

sys.modules[__name__] = _impl
