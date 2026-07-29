"""Transitional source-checkout shim for reason_assembly.reason_assembly."""
import sys
from reason_assembly import reason_assembly as _impl

sys.modules[__name__] = _impl
