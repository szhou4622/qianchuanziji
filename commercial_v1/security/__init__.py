"""商业版敏感信息保护。"""

from .dpapi import DPAPIUnavailableError, protect_text, unprotect_text
from .redaction import redact, sanitize_text

__all__ = ["DPAPIUnavailableError", "protect_text", "unprotect_text", "redact", "sanitize_text"]
