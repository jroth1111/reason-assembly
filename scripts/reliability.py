"""Transitional source-checkout shim for reason_assembly.reliability."""
import sys
from reason_assembly import reliability as _impl

sys.modules[__name__] = _impl
