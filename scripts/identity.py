"""Transitional source-checkout shim for reason_assembly.identity."""
import sys
from reason_assembly import identity as _impl

sys.modules[__name__] = _impl
