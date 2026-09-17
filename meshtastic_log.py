#!/usr/bin/env python3
"""Log Meshtastic text messages and send email for direct messages.

The relay writes all text messages to the main log.
It copies the important messages to a second log.
It sends email for the messages you choose.
The classification is in labels.py.
"""

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

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
HEARTBEAT_SECONDS = 3600


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
    notify_labels: set[str] = field(default_factory=lambda: {"dm", "mention"})
    mention_substrings: list[str] = field(default_factory=list)
    mention_words: list[str] = field(default_factory=list)
    channels_include: set[int] = field(default_factory=set)  # empty: all channels
    watchdog_seconds: int = 1800
    retry_seconds: int = 10
    email: EmailConfig = field(default_factory=EmailConfig)


def load_config(path: Path) -> Config:
    """Read the TOML config. Stop if the file does not exist.

    Missing values get the defaults from the dataclasses.
    """
    if not path.exists():
        raise SystemExit(f"no config at {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    LOG.info("loaded config from %s", path)

    defaults = Config()
    logs = raw.get("logs", {})
    label_cfg = raw.get("labels", {})
    email = raw.get("notify", {}).get("email", {})
    conn = raw.get("connection", {})

    return Config(
        device=raw.get("device", defaults.device),
        log_all=Path(logs.get("all", defaults.log_all)).expanduser(),
        log_important=Path(logs.get("important", defaults.log_important)).expanduser(),
        important_labels=set(raw.get("important", {}).get("labels", defaults.important_labels)),
        notify_labels=set(raw.get("notify", {}).get("labels", defaults.notify_labels)),
        mention_substrings=list(label_cfg.get("mention_substrings", [])),
        mention_words=list(label_cfg.get("mention_words", [])),
        channels_include=set(raw.get("channels", {}).get("include", [])),
        watchdog_seconds=int(conn.get("watchdog_seconds", defaults.watchdog_seconds)),
        retry_seconds=int(conn.get("retry_seconds", defaults.retry_seconds)),
        email=EmailConfig(
            enabled=bool(email.get("enabled", defaults.email.enabled)),
            to=email.get("to", defaults.email.to),
            user=email.get("user", defaults.email.user),
            host=email.get("host", defaults.email.host),
            port=int(email.get("port", defaults.email.port)),
            timeout=int(email.get("timeout", defaults.email.timeout)),
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

    BATCH_SECONDS = 5

    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg
        self.password = os.environ.get(ENV_PASSWORD, "")
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=200)
        missing = [name for name in ("to", "user", "host") if not getattr(cfg, name)]
        if not self.password:
            missing.append(ENV_PASSWORD)
        if missing:
            raise SystemExit(f"email is on, but these are not set: {', '.join(missing)}")
        threading.Thread(target=self._run, name="notifier", daemon=True).start()

    def send(self, subject: str, body: str) -> None:
        try:
            self.queue.put_nowait((subject, body))
        except queue.Full:
            LOG.error("the notify queue is full. dropped: %s", subject)

    def _run(self) -> None:
        while True:
            batch = [self.queue.get()]
            time.sleep(self.BATCH_SECONDS)  # put a burst into one email
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                self._deliver(batch)
            except Exception:
                LOG.exception("cannot send the email for %d message(s)", len(batch))

    def _deliver(self, batch: list[tuple[str, str]]) -> None:
        msg = EmailMessage()
        msg["Subject"] = batch[0][0] if len(batch) == 1 else f"Meshtastic: {len(batch)} messages"
        msg["From"] = self.cfg.user
        msg["To"] = self.cfg.to
        msg.set_content("\n".join(body for _, body in batch))
        with smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout) as smtp:
            smtp.login(self.cfg.user, self.password)
            smtp.send_message(msg)
        LOG.info("sent %d message(s) by email", len(batch))


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
        # Counts for the hourly heartbeat log.
        self.count_packets = 0
        self.count_text = 0
        self.count_important = 0
        self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

    # -- node data. Read it live. A change on the device needs no restart.

    def nodes(self) -> dict:
        """Get the node list of the device. It is empty before the first connection."""
        if self.iface is None or not self.iface.nodesByNum:
            return {}
        return self.iface.nodesByNum

    def name_of(self, num) -> str:
        """Get the name of a node."""
        if num == labels.BROADCAST_NUM:
            return "all"
        node = self.nodes().get(num, {})
        user = node.get("user", {})
        if user.get("longName"):
            return user["longName"]
        if user.get("shortName"):
            return user["shortName"]
        return f"!{num:08x}" if isinstance(num, int) else "unknown"

    def favorite_nums(self) -> set[int]:
        return {num for num, node in self.nodes().items() if node.get("isFavorite")}

    # -- handlers. The library calls them on its own thread.

    def on_any_packet(self, packet, interface):
        """Record the time. All packet types show that the link is alive."""
        self.last_packet = time.monotonic()
        self.count_packets += 1

    def on_text(self, packet, interface):
        try:
            self._handle_text(packet)
        except Exception:
            LOG.exception("cannot process the packet. the relay continues")

    def on_connection_lost(self, interface):
        LOG.warning("the device reports a lost connection")
        self.reconnect.set()

    def _is_duplicate(self, packet) -> bool:
        """Find a repeated packet. The mesh can deliver a packet two times."""
        pid = packet.get("id")
        if pid is None:
            return False
        if pid in self.seen:
            return True
        self.seen.append(pid)
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

        sender = self.name_of(packet.get("from"))
        recipient = self.name_of(packet.get("to"))
        found = labels.labels_for(
            packet, text, self.my_num, self.favorite_nums(),
            self.cfg.mention_substrings, self.cfg.mention_words,
        )
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self.count_text += 1

        # Queue the email first. A failed file write must not stop it.
        if self.notifier and found & self.cfg.notify_labels:
            self.notifier.send(
                labels.format_subject(sender, recipient),
                labels.format_email(timestamp, sender, recipient, found, text, packet),
            )
        line = labels.format_line(timestamp, sender, recipient, found, text)
        append(self.cfg.log_all, line)
        if found & self.cfg.important_labels:
            self.count_important += 1
            append(self.cfg.log_important, line)

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
            self._heartbeat()
            idle = time.monotonic() - self.last_packet
            if idle > self.cfg.watchdog_seconds:
                LOG.warning("no packet for %.0f s. the relay reconnects", idle)
                return

    def _heartbeat(self) -> None:
        """Log the counts once an hour. This shows that the relay works."""
        if time.monotonic() < self.next_heartbeat:
            return
        LOG.info(
            "last hour: %d packets, %d text messages, %d important",
            self.count_packets, self.count_text, self.count_important,
        )
        self.count_packets = self.count_text = self.count_important = 0
        self.next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

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
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="TOML config file")
    parser.add_argument("--verbose", action="store_true", help="write debug messages")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(Path(args.config).expanduser())

    for path in (cfg.log_all, cfg.log_important):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    LOG.info("main log: %s. important log: %s", cfg.log_all, cfg.log_important)

    notifier = EmailNotifier(cfg.email) if cfg.email.enabled else None
    Relay(cfg, notifier).run()


if __name__ == "__main__":
    main()
