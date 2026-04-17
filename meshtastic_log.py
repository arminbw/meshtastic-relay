#!/usr/bin/env python3
"""Meshtastic logger.

This script uses the current Meshtastic Python API style:
- subscribe with `pub.subscribe(on_receive, "meshtastic.receive")`
- open the radio using `serial_interface.SerialInterface(device)`

That matches the installed package examples in `meshtastic/__init__.py`.
"""

import argparse
import logging
import time
from pathlib import Path

from pubsub import pub
from meshtastic import serial_interface

LOG = logging.getLogger("meshtastic_logger")


def format_packet(packet):
    decoded = packet.get("decoded") or {}

    def decode_text(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, list):
            return "".join(str(v) for v in value)
        return str(value) if value is not None else None

    text = decoded.get("text") or decoded.get("payload")
    if text is None and isinstance(decoded.get("data"), dict):
        data = decoded.get("data")
        text = data.get("text") or data.get("payload")

    text = decode_text(text)
    if not text:
        return None

    sender = packet.get("fromId") or packet.get("from")
    if isinstance(sender, dict):
        sender = (
            sender.get("user_alias")
            or sender.get("userid")
            or sender.get("name")
        )
    if sender is None:
        sender = "unknown"
    if isinstance(sender, int):
        sender = f"node-{sender}"

    return f"[Meshtastic] {sender}: {text}"


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message)


def make_on_receive(log_path: Path):
    def on_receive(packet, interface=None):
        LOG.debug("meshtastic receive callback invoked packet=%s interface=%s", packet, interface)
        try:
            text = format_packet(packet)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

            if text:
                line = f"{timestamp} {text}\n"
            else:
                sender = packet.get("fromId") or packet.get("from") or "unknown"
                if isinstance(sender, int):
                    sender = f"node-{sender}"
                port = packet.get("decoded", {}).get("portnum")
                line = f"{timestamp} [Meshtastic] received packet from {sender} port={port}\n"
                LOG.debug("wrote fallback packet line for non-text packet")

            append_log(log_path, line)
            LOG.info("wrote packet to log: %s", log_path)
        except Exception:
            LOG.exception("error processing received packet")

    return on_receive


def open_interface(device: str):
    LOG.info("opening Meshtastic device %s", device)
    return serial_interface.SerialInterface(device)


def main():
    parser = argparse.ArgumentParser(
        description="Log incoming Meshtastic messages to a file"
    )
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="Meshtastic serial device (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--log-file",
        default="meshtastic_messages.log",
        help="Path to the file where incoming messages will be appended",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    log_path = Path(args.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    LOG.info("logging incoming Meshtastic messages to %s", log_path)

    callback = make_on_receive(log_path)
    subscriber, success = pub.subscribe(callback, "meshtastic.receive")
    LOG.debug("subscribed to Meshtastic topic meshtastic.receive: success=%s", success)

    interface = None

    while True:
        try:
            interface = open_interface(args.device)
            LOG.info("Meshtastic logger started; waiting for messages")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            LOG.info("shutting down logger")
            break
        except Exception:
            LOG.exception("logger error, retrying in 10 seconds")
            time.sleep(10)
        finally:
            if interface is not None:
                close_method = getattr(interface, "close", None)
                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        LOG.debug("failed to close Meshtastic interface cleanly", exc_info=True)
                interface = None


if __name__ == "__main__":
    main()
