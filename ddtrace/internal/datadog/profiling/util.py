from sys import version_info
from typing import Any  # noqa:F401

from ddtrace.internal.compat import ensure_binary
from ddtrace.internal.logger import get_logger

from .types import StringType


LOG = get_logger(__name__)


def ensure_binary_or_empty(s: StringType) -> bytes:
    try:
        return ensure_binary(s)
    except Exception:
        # We don't alert on this situation, we just take it in stride
        return b""
    return b""


# 3.11 and above
def _sanitize_string_check(value):
    # type: (Any) -> str
    if isinstance(value, str):
        return value
    elif value is None:
        return ""
    try:
        return value.decode("utf-8", "ignore")
    except Exception:
        LOG.warning("Got object of type '%s' instead of str during profile serialization", type(value).__name__)
        return "[invalid string]%s" % type(value).__name__


# 3.10 and below (the noop version)
def _sanitize_string_identity(value):
    # type: (Any) -> str
    return value or ""


# Assign based on version
sanitize_string = _sanitize_string_check if version_info[:2] > (3, 10) else _sanitize_string_identity
