#!/usr/bin/env python3
"""Message classification. Pure functions only.

This module has no hardware access and no third-party imports.
You can test it on any computer without a radio.
"""

from __future__ import annotations

import re

BROADCAST_NUM = 0xFFFFFFFF  # 4294967295 in meshtastic 2.7.8

# These labels give the reason. They come before the channel label.
LABEL_ORDER = ("dm", "mention", "favorite")


def decode_text(value) -> str | None:
    """Convert a payload to text. Return None if the data is not text."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return str(value)


def message_text(packet) -> str | None:
    """Get the text of a packet. Return None if the packet has no text."""
    decoded = packet.get("decoded") or {}
    text = decode_text(decoded.get("text"))
    if text is None:
        text = decode_text(decoded.get("payload"))
    if text is None:
        return None
    return text.strip() or None


def channel_of(packet) -> int:
    return packet.get("channel") or 0


def is_direct(packet, my_num) -> bool:
    """Return True if the packet goes only to us."""
    to = packet.get("to")
    return my_num is not None and to == my_num and to != BROADCAST_NUM


def mentions(text, substrings, words) -> bool:
    """Find a name in the text.

    A substring matches at any position.
    A word matches only if it stands alone.
    Use words for short names. This prevents a match inside a longer word.
    """
    low = text.lower()
    if any(s and s.lower() in low for s in substrings):
        return True
    return any(w and re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in words)


def labels_for(packet, text, sender_num, my_num, favorite_nums,
               mention_substrings=(), mention_words=()) -> set[str]:
    """Give a packet its labels."""
    labels = {f"ch{channel_of(packet)}"}
    if is_direct(packet, my_num):
        labels.add("dm")
    # The device can set isFavorite on your own node. Skip your own messages.
    if sender_num is not None and sender_num != my_num and sender_num in favorite_nums:
        labels.add("favorite")
    if mentions(text, mention_substrings, mention_words):
        labels.add("mention")
    return labels


def sort_labels(labels) -> list[str]:
    """Put the reason labels first. Keep the order stable."""
    leading = [x for x in LABEL_ORDER if x in labels]
    return leading + sorted(x for x in labels if x not in LABEL_ORDER)


def one_line(text) -> str:
    """Replace the line breaks. One message must use one log line."""
    return text.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def format_line(timestamp, sender, recipient, labels, text) -> str:
    return f"{timestamp}  {sender} -> {recipient}  [{' '.join(sort_labels(labels))}]  {one_line(text)}\n"
