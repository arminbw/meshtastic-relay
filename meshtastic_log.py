#!/usr/bin/env python3
"""A simple Meshtastic text logger.

Only direct text messages are written to the log.
"""

import argparse
import logging
import time
from pathlib import Path

from pubsub import pub
from meshtastic import serial_interface

LOG = logging.getLogger("meshtastic_logger")


def decode_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return str(value) if value is not None else None


def get_text(packet):
    decoded = packet.get("decoded") or {}
    text = decoded.get("text")
    if text is not None:
        return decode_text(text)
    payload = decoded.get("payload")
    return decode_text(payload)


def get_sender(packet):
    sender = packet.get("fromId") or packet.get("from")
    if isinstance(sender, dict):
        return (
            sender.get("user_alias")
            or sender.get("userid")
            or sender.get("name")
            or "unknown"
        )
    if isinstance(sender, int):
        return f"node-{sender}"
    return str(sender) if sender is not None else "unknown"


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message)


def on_receive(packet, interface=None):
    text = get_text(packet)
    if not text:
        return

    sender = get_sender(packet)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"{timestamp} [Meshtastic] {sender}: {text}\n"
    append_log(on_receive.log_path, line)
    LOG.info("wrote packet to log: %s", on_receive.log_path)


def open_interface(device: str):
    LOG.info("opening Meshtastic device %s", device)
    return serial_interface.SerialInterface(device)


def main():
    parser = argparse.ArgumentParser(description="Log incoming Meshtastic text messages to a file")
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
    LOG.info("logging incoming Meshtastic text messages to %s", log_path)

    on_receive.log_path = log_path
    pub.subscribe(on_receive, "meshtastic.receive.text")

    interface = None
    while True:
        try:
            interface = open_interface(args.device)
            LOG.info("Meshtastic logger started; waiting for text messages")
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
