"""Message classification and formatting. Pure functions only.

This module has no hardware access and no third-party imports.
You can test it on any computer without a radio.
"""

import re

BROADCAST_NUM = 0xFFFFFFFF  # 4294967295 in meshtastic 2.7.8

# These labels give the reason. They come before the channel label.
LABEL_ORDER = ("dm", "mention", "favorite")


def message_text(packet) -> str | None:
    """Get the text of a packet. Return None if the packet has no text.

    The library decodes the text. It leaves out text that is not valid UTF-8.
    """
    text = packet.get("decoded", {}).get("text")
    if not isinstance(text, str):
        return None
    return text.strip() or None


def channel_of(packet) -> int:
    return packet.get("channel") or 0


def is_direct(packet, my_num) -> bool:
    """Return True if the packet goes only to us."""
    to = packet.get("to")
    return my_num is not None and to == my_num


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


def labels_for(packet, text, my_num, favorite_nums,
               mention_substrings=(), mention_words=()) -> set[str]:
    """Give a packet its labels."""
    sender_num = packet.get("from")
    labels = {f"ch{channel_of(packet)}"}
    if is_direct(packet, my_num):
        labels.add("dm")
    # The device can set isFavorite on our own node. Skip our own messages.
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
    """Replace the line breaks. One message must use one log line.

    Double the backslashes first. Then a real line break and a typed "\\n" look different.
    """
    return text.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def format_line(timestamp, sender, recipient, labels, text) -> str:
    return f"{timestamp}  {sender} -> {recipient}  [{' '.join(sort_labels(labels))}]  {one_line(text)}\n"


def format_subject(sender, recipient) -> str:
    return f"{sender} -> {recipient}"


def format_metadata(packet) -> str:
    """List the radio details of a packet. Return an empty string if there are none."""
    details = []
    hop_start = packet.get("hopStart")
    hop_limit = packet.get("hopLimit")
    if isinstance(hop_start, int) and isinstance(hop_limit, int):
        details.append(f"hops={hop_start - hop_limit}")
    if packet.get("rxRssi") is not None:
        details.append(f"rssi={packet['rxRssi']}")
    if packet.get("rxSnr") is not None:
        details.append(f"snr={packet['rxSnr']}")
    if packet.get("viaMqtt"):
        details.append("via mqtt")
    return " ".join(details)


def format_email(timestamp, sender, recipient, labels, text, packet) -> str:
    """Write the email body. The text keeps its line breaks."""
    body = (
        f"From: {sender}\n"
        f"To: {recipient}\n"
        f"Time: {timestamp}\n"
        f"Labels: {' '.join(sort_labels(labels))}\n"
        f"\n{text}\n"
    )
    metadata = format_metadata(packet)
    if metadata:
        body += f"\n{metadata}\n"
    return body
