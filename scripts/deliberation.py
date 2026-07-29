"""Transitional source-checkout shim for reason_assembly.deliberation."""
import sys
from reason_assembly import deliberation as _impl

sys.modules[__name__] = _impl
