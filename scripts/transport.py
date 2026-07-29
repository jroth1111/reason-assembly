"""Transitional source-checkout shim for reason_assembly.transport."""
import sys
from reason_assembly import transport as _impl

sys.modules[__name__] = _impl
