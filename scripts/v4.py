"""Transitional source-checkout shim for reason_assembly.v4."""
import sys
from reason_assembly import v4 as _impl

sys.modules[__name__] = _impl
