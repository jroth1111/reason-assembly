"""Transitional source-checkout shim for reason_assembly.v4_state."""
import sys
from reason_assembly import v4_state as _impl

sys.modules[__name__] = _impl
