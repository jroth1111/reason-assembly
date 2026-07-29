"""Transitional source-checkout shim for reason_assembly.protocols."""
import sys
from reason_assembly import protocols as _impl

sys.modules[__name__] = _impl
