"""Transitional source-checkout shim for reason_assembly.git_worker."""
import sys
from reason_assembly import git_worker as _impl

sys.modules[__name__] = _impl
