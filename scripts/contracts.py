"""Transitional source-checkout shim for reason_assembly.contracts."""
import sys
from reason_assembly import contracts as _impl

sys.modules[__name__] = _impl
