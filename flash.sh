#!/bin/sh
# Write new firmware to the Heltec V3 node.
# Run this script on the Pi.
# Download the zip file from github.com/meshtastic/firmware/releases.
# Usage: ./flash.sh firmware-esp32s3-<version>.zip

# The script stops if a command fails or if a variable is not set.
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 firmware-esp32s3-<version>.zip" >&2
    exit 1
fi

zip=$1
dir=$(cd "$(dirname "$0")" && pwd)
venv=$dir/venv/bin
service=meshtastic_log.service

# Get the serial port from config.toml.
# If config.toml does not set the port, use /dev/ttyUSB0.
port=$(sed -n 's/^device *= *"\(.*\)".*/\1/p' "$dir/config.toml" 2>/dev/null || true)
port=${port:-/dev/ttyUSB0}
backup=~/node-backup-$(date +%Y%m%d-%H%M).yaml

# Extract the zip file into a temporary directory.
# Delete the directory when the script stops.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
unzip -q "$zip" -d "$tmp"

# Find the update image for the Heltec V3.
# The update image keeps the node settings.
# The factory image deletes the node settings.
bin=$(find "$tmp" -name 'firmware-heltec-v3-*.bin' ! -name '*factory*')
if [ "$(echo "$bin" | grep -c .)" -ne 1 ]; then
    echo "expected one heltec-v3 update image in $zip, found:" >&2
    echo "$bin" >&2
    exit 1
fi
echo "image: $(basename "$bin")"
echo "port:  $port"

# If esptool is not in the venv, install esptool.
if ! "$venv/python" -m esptool version >/dev/null 2>&1; then
    "$venv/pip" install -q esptool
fi

# The relay service uses the serial port.
# Stop the service before you write the firmware.
# When the script stops, start the service again.
# This also occurs after an error.
sudo systemctl stop "$service"
trap 'rm -rf "$tmp"; sudo systemctl start "$service"' EXIT

# Save the node configuration to a backup file.
"$venv/meshtastic" --port "$port" --export-config > "$backup"
echo "config saved to $backup"

# Write the image to address 0x10000.
# The application starts at this address.
"$venv/python" -m esptool --chip esp32s3 --port "$port" write_flash 0x10000 "$bin"

# Wait 20 seconds. The node must start before the next step.
# Show the new firmware version.
sleep 20
"$venv/meshtastic" --port "$port" --info | grep -m1 firmwareVersion
