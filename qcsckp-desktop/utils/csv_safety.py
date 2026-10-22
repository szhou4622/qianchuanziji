"""Preserve source text while exporting spreadsheet-safe text cells."""
import re

_FORMULA_PREFIX = re.compile(r"^[\s\ufeff]*[=+@\-\t\r\n]")


def spreadsheet_cell(value):
    if isinstance(value, str) and _FORMULA_PREFIX.match(value):
        return "'" + value
    return value
