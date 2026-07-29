"""Transitional source-checkout shim for reason_assembly.verification."""
import sys
from reason_assembly import verification as _impl

sys.modules[__name__] = _impl
