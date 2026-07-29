"""Transitional source-checkout shim for reason_assembly.artifacts."""
import sys
from reason_assembly import artifacts as _impl

sys.modules[__name__] = _impl
