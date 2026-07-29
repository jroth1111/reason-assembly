"""Transitional source-checkout shim for reason_assembly.state_compat."""
import sys
from reason_assembly import state_compat as _impl

sys.modules[__name__] = _impl
