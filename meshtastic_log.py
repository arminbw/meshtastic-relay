#!/usr/bin/env python3

import argparse
import logging
import time
from pathlib import Path

from meshtastic import serial_interface

LOG = logging.getLogger("meshtastic_logger")


def format_packet(packet):
    decoded = packet.get("decoded") or {}
    text = decoded.get("text") or decoded.get("payload")
    if not text:
        return None

    sender = (
        packet.get("from", {}).get("user_alias")
        or packet.get("from", {}).get("userid")
        or "unknown"
    )
    return f"[Meshtastic] {sender}: {text}"


def append_log(log_path: Path, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message)


def make_on_receive(log_path: Path):
    def on_receive(packet, interface):
        text = format_packet(packet)
        if not text:
            LOG.debug("skipping non-text packet: %s", packet)
            return

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        line = f"{timestamp} {text}\n"
        append_log(log_path, line)
        LOG.info("wrote packet to log: %s", log_path)

    return on_receive


def open_interface(device: str, on_receive_callback):
    LOG.info("opening Meshtastic device %s", device)
    interface = serial_interface.SerialInterface(device)
    interface.onReceive += on_receive_callback
    return interface


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
    on_receive_callback = make_on_receive(log_path)
    interface = None

    while True:
        try:
            interface = open_interface(args.device, on_receive_callback)
            LOG.info("Meshtastic logger started; waiting for messages")
            interface.waitForConnection()
            interface.loop()
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
