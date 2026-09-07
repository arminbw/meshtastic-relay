#!/usr/bin/env python3
"""Log Meshtastic text messages and send email for direct messages.

The relay writes all text messages to the main log.
It copies the important messages to a second log.
It sends email for the messages you choose.
The classification is in labels.py.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import smtplib
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from meshtastic import serial_interface
from pubsub import pub

import labels

LOG = logging.getLogger("meshtastic_relay")

# Only the password comes from the environment. config.toml is not in git.
ENV_PASSWORD = "MESH_SMTP_PASSWORD"


# --------------------------------------------------------------------------- config


@dataclass
class EmailConfig:
    enabled: bool = False
    to: str = ""
    user: str = ""
    host: str = ""
    port: int = 465
    timeout: int = 20


@dataclass
class Config:
    device: str = "/dev/ttyUSB0"
    log_all: Path = Path("~/meshtastic_messages.log")
    log_important: Path = Path("~/meshtastic_important.log")
    important_labels: set[str] = field(default_factory=lambda: {"dm", "mention", "favorite"})
    notify_labels: set[str] = field(default_factory=lambda: {"dm"})
    mention_substrings: list[str] = field(default_factory=list)
    mention_words: list[str] = field(default_factory=list)
    channels_include: set[int] = field(default_factory=set)  # empty: all channels
    watchdog_seconds: int = 1800
    retry_seconds: int = 10
    email: EmailConfig = field(default_factory=EmailConfig)


def load_config(path: Path) -> Config:
    """Read the TOML config. Use the defaults if the file does not exist."""
    raw = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        LOG.info("loaded config from %s", path)
    else:
        LOG.warning("no config at %s, using defaults", path)

    logs = raw.get("logs", {})
    marks = raw.get("labels", {})
    email = raw.get("notify", {}).get("email", {})
    conn = raw.get("connection", {})

    return Config(
        device=raw.get("device", Config.device),
        log_all=Path(logs.get("all", "~/meshtastic_messages.log")).expanduser(),
        log_important=Path(logs.get("important", "~/meshtastic_important.log")).expanduser(),
        important_labels=set(raw.get("important", {}).get("labels", ["dm", "mention", "favorite"])),
        notify_labels=set(raw.get("notify", {}).get("labels", ["dm"])),
        mention_substrings=list(marks.get("mention_substrings", [])),
        mention_words=list(marks.get("mention_words", [])),
        channels_include=set(raw.get("channels", {}).get("include", [])),
        watchdog_seconds=int(conn.get("watchdog_seconds", 1800)),
        retry_seconds=int(conn.get("retry_seconds", 10)),
        email=EmailConfig(
            enabled=bool(email.get("enabled", False)),
            to=email.get("to", ""),
            user=email.get("user", ""),
            host=email.get("host", ""),
            port=int(email.get("port", 465)),
            timeout=int(email.get("timeout", 20)),
        ),
    )


# --------------------------------------------------------------------------- sinks


def append(path: Path, line: str) -> None:
    """Add one line to the file. Open and close the file each time.

    This prevents a data loss if the power fails.
    Logrotate can also move the file. The relay needs no signal.
    """
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


class EmailNotifier:
    """Send email in a separate thread. SMTP must not block the radio."""

    COALESCE_SECONDS = 5

    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg
        self.password = os.environ.get(ENV_PASSWORD, "")
        self.queue: queue.Queue[str] = queue.Queue(maxsize=200)
        if not self.password:
            LOG.error("email is on, but %s is not set", ENV_PASSWORD)
        threading.Thread(target=self._run, name="notifier", daemon=True).start()

    def send(self, line: str) -> None:
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            LOG.error("the notify queue is full. dropped: %s", line.strip())

    def _run(self) -> None:
        while True:
            batch = [self.queue.get()]
            time.sleep(self.COALESCE_SECONDS)  # put a burst into one email
            while True:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._deliver(batch)
            except Exception:
                LOG.exception("cannot send the email for %d message(s)", len(batch))

    def _deliver(self, lines: list[str]) -> None:
        msg = EmailMessage()
        msg["Subject"] = "Meshtastic DM" if len(lines) == 1 else f"Meshtastic: {len(lines)} messages"
        msg["From"] = self.cfg.user
        msg["To"] = self.cfg.to
        msg.set_content("".join(lines))
        with smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout) as smtp:
            smtp.login(self.cfg.user, self.password)
            smtp.send_message(msg)
        LOG.info("sent %d message(s) by email", len(lines))


# --------------------------------------------------------------------------- relay


class Relay:
    def __init__(self, cfg: Config, notifier: EmailNotifier | None):
        self.cfg = cfg
        self.notifier = notifier
        self.iface = None
        self.my_num: int | None = None
        self.reconnect = threading.Event()
        self.last_packet = time.monotonic()
        self.seen: deque[int] = deque(maxlen=512)
        self.seen_ids: set[int] = set()

    # -- node data. Read it live. A change on the device needs no restart.

    def name_of(self, num, fallback_id=None) -> str:
        """Get the name of a node."""
        if num == labels.BROADCAST_NUM:
            return "all"
        node = (getattr(self.iface, "nodesByNum", None) or {}).get(num) or {}
        user = node.get("user") or {}
        if user.get("longName"):
            return user["longName"]
        if user.get("shortName"):
            return user["shortName"]
        if fallback_id:
            return str(fallback_id)
        return f"!{num:08x}" if isinstance(num, int) else "unknown"

    def favorite_nums(self) -> set[int]:
        nodes = getattr(self.iface, "nodesByNum", None) or {}
        return {num for num, node in nodes.items() if node.get("isFavorite")}

    # -- handlers. The library calls them on its own thread.

    def on_any_packet(self, packet, interface):
        """Record the time. All packet types show that the link is alive."""
        self.last_packet = time.monotonic()

    def on_text(self, packet, interface):
        try:
            self._handle_text(packet)
        except Exception:
            LOG.exception("cannot process the packet. the relay continues")

    def on_connection_lost(self, interface, topic=pub.AUTO_TOPIC):
        LOG.warning("the device reports a lost connection")
        self.reconnect.set()

    def _is_duplicate(self, packet) -> bool:
        """Find a repeated packet. The mesh can deliver a packet two times."""
        pid = packet.get("id")
        if pid is None:
            return False
        if pid in self.seen_ids:
            return True
        if len(self.seen) == self.seen.maxlen:
            self.seen_ids.discard(self.seen[0])
        self.seen.append(pid)
        self.seen_ids.add(pid)
        return False

    def _handle_text(self, packet) -> None:
        if self._is_duplicate(packet):
            LOG.debug("ignored the duplicate packet %s", packet.get("id"))
            return

        text = labels.message_text(packet)
        if not text:
            return

        channel = labels.channel_of(packet)
        if self.cfg.channels_include and channel not in self.cfg.channels_include:
            LOG.debug("ignored channel %s", channel)
            return

        sender_num = packet.get("from")
        found = labels.labels_for(
            packet, text, sender_num, self.my_num, self.favorite_nums(),
            self.cfg.mention_substrings, self.cfg.mention_words,
        )
        line = labels.format_line(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            self.name_of(sender_num, packet.get("fromId")),
            self.name_of(packet.get("to"), packet.get("toId")),
            found,
            text,
        )

        append(self.cfg.log_all, line)
        if found & self.cfg.important_labels:
            append(self.cfg.log_important, line)
        if self.notifier and found & self.cfg.notify_labels:
            self.notifier.send(line)

    # -- connection

    def _connect(self) -> None:
        LOG.info("opening the device %s", self.cfg.device)
        self.reconnect.clear()
        self.iface = serial_interface.SerialInterface(self.cfg.device)
        self.last_packet = time.monotonic()
        me = self.iface.getMyNodeInfo() or {}
        self.my_num = me.get("num")
        LOG.info("connected as %s (num %s)", (me.get("user") or {}).get("longName"), self.my_num)

    def _close(self) -> None:
        if self.iface is None:
            return
        try:
            self.iface.close()
        except Exception:
            LOG.debug("the device did not close correctly", exc_info=True)
        self.iface = None

    def _watch(self) -> None:
        """Wait until the link fails.

        Systemd restarts the relay if the process stops.
        Systemd cannot see a dead reader thread in a live process.
        This watchdog finds that condition.
        """
        while not self.reconnect.wait(5):
            idle = time.monotonic() - self.last_packet
            if idle > self.cfg.watchdog_seconds:
                LOG.warning("no packet for %.0f s. the relay reconnects", idle)
                return

    def run(self) -> None:
        pub.subscribe(self.on_text, "meshtastic.receive.text")
        pub.subscribe(self.on_any_packet, "meshtastic.receive")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")

        while True:
            try:
                self._connect()
                self._watch()
            except KeyboardInterrupt:
                LOG.info("the relay stops")
                self._close()
                return
            except Exception:
                LOG.exception("connection error. the relay retries in %s s", self.cfg.retry_seconds)
                time.sleep(self.cfg.retry_seconds)
            self._close()


# --------------------------------------------------------------------------- cli


def main() -> None:
    parser = argparse.ArgumentParser(description="Log Meshtastic text messages")
    parser.add_argument("--config", default="config.toml", help="TOML config file")
    parser.add_argument("--device", help="use this serial device")
    parser.add_argument("--log-file", help="use this main log file")
    parser.add_argument("--verbose", action="store_true", help="write debug messages")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(Path(args.config).expanduser())
    if args.device:
        cfg.device = args.device
    if args.log_file:
        cfg.log_all = Path(args.log_file).expanduser()

    for path in (cfg.log_all, cfg.log_important):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    LOG.info("main log: %s. important log: %s", cfg.log_all, cfg.log_important)

    notifier = EmailNotifier(cfg.email) if cfg.email.enabled else None
    Relay(cfg, notifier).run()


if __name__ == "__main__":
    main()
