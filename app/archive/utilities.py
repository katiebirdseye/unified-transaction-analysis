from __future__ import annotations

from typing import Optional


def normalize_enter_exit(
    value: Optional[str],
) -> str:
    """
    Normalize Archive direction labels to the canonical
    'enters' or 'exits' representation.

    Supported examples:

        enter, enters, entry, in, inbound -> enters
        exit, exits, out, outbound        -> exits

    Unknown values are returned in cleaned lowercase form.
    Empty values return an empty string.
    """

    if not value:
        return ""

    direction = str(value).strip().lower()

    enters_values = {
        "enter",
        "enters",
        "entry",
        "in",
        "inbound",
        "in bound",
        "in-bound",
    }

    exits_values = {
        "exit",
        "exits",
        "out",
        "outbound",
        "out bound",
        "out-bound",
    }

    if direction in enters_values:
        return "enters"

    if direction in exits_values:
        return "exits"

    return direction
