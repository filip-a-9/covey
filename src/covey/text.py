"""Make agent-written text safe to print for a human. Import-light: used by CLI, TUI and daemon.

Agents control `task`, `result`, `kind`, `workdir` and more. A terminal treats ESC, CR and
the C1 controls as commands (cursor moves, window titles, forged rows), so text that is
shown to the human goes through `safe()` first. `--json` output is data, and is left alone.
"""

import re

NEWLINE = chr(0x23CE)  # the return symbol: one visible mark in place of a line break
REPLACEMENT = chr(0xFFFD)
TRUNCATED = chr(0x2026)

# CR LF, CR, LF, NEL, line separator, paragraph separator.
_BREAKS = re.compile("\r\n|[\r\n\x85" + chr(0x2028) + chr(0x2029) + "]")
# C0 (incl. tab and ESC), DEL, C1, and the bidi overrides that reorder what is displayed.
_CONTROLS = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    + chr(0x202A)
    + "-"
    + chr(0x202E)
    + chr(0x2066)
    + "-"
    + chr(0x2069)
    + "]"
)


def safe(text: object, limit: int | None = None) -> str:
    """One printable line: breaks become a return symbol, other controls a replacement mark.

    With `limit`, anything longer is cut after `limit` characters and an ellipsis is added, so
    the reader can tell it was cut.
    """
    clean = _CONTROLS.sub(REPLACEMENT, _BREAKS.sub(NEWLINE, str(text)))
    if limit is not None and len(clean) > limit:
        return clean[:limit] + TRUNCATED
    return clean
