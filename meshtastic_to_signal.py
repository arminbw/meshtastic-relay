#!/usr/bin/env python3

import argparse
import logging
import subprocess
import sys
import time

import meshtastic
from meshtastic import serial_interface

LOG = logging.getLogger("meshtastic_signal_relay")


def send_signal(signal_user, signal_group, text, signal_cmd="signal-cli"):
    cmd = [
        signal_cmd,
        "-u",
        signal_user,
        "send",
        "-g",
        signal_group,
        text,
    ]
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        LOG.error(
            "signal-cli failed (%s): %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        raise RuntimeError("signal-cli send failed")
    LOG.info("forwarded to Signal group: %s", text)


def format_packet(packet):
    decoded = packet.get("decoded", {}) or {}
    text = decoded.get("text") or decoded.get("payload") or ""
    sender = packet.get("from", {}).get("user_alias") or packet.get("from", {}).get("userid") or "unknown"
    if not text:
        return None
    return f"[Meshtastic] {sender}: {text}"


def on_receive(packet, interface):
    message = format_packet(packet)
    if not message:
        LOG.debug("skipping non-text packet: %s", packet)
        return

    LOG.info("received packet from Meshtastic: %s", message)
    send_signal(
        interface.signal_user,
        interface.signal_group,
        message,
        signal_cmd=interface.signal_cmd,
    )


def open_interface(device, signal_user, signal_group, signal_cmd):
    LOG.info("opening Meshtastic device %s", device)
    interface = serial_interface.SerialInterface(device)
    interface.signal_user = signal_user
    interface.signal_group = signal_group
    interface.signal_cmd = signal_cmd
    interface.onReceive += on_receive
    return interface


def main():
    parser = argparse.ArgumentParser(
        description="Relay Meshtastic messages into a Signal group"
    )
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="Meshtastic serial device (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--signal-user",
        required=True,
        help="Signal number registered with signal-cli, e.g. +12345550123",
    )
    parser.add_argument(
        "--signal-group",
        required=True,
        help="Signal group ID to send messages into",
    )
    parser.add_argument(
        "--signal-cmd",
        default="signal-cli",
        help="signal-cli command path",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=10,
        help="Seconds to wait before reconnecting after an error",
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

    while True:
        try:
            iface = open_interface(
                args.device, args.signal_user, args.signal_group, args.signal_cmd
            )
            LOG.info("Meshtastic relay started; waiting for messages")
            iface.waitForConnection()
            iface.loop()
        except KeyboardInterrupt:
            LOG.info("shutting down relay")
            sys.exit(0)
        except Exception as exc:
            LOG.exception("relay error, retrying in %s seconds", args.retry_delay)
            time.sleep(args.retry_delay)


if __name__ == "__main__":
    main()