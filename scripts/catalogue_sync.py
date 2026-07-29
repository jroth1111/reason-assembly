"""Transitional source-checkout shim for reason_assembly.catalogue_sync."""
import sys
from reason_assembly import catalogue_sync as _impl

sys.modules[__name__] = _impl
