#!/bin/sh
# Build the image and (re)start the pod as rootless systemd services. Run on the Pi from the repo root.
set -eu

CONF="$HOME/.config/second-brain"
missing=0
for f in env tunnel.env; do
    if [ ! -f "$CONF/$f" ]; then
        mkdir -p "$CONF"
        install -m 600 "deploy/$f.example" "$CONF/$f"
        echo "Created $CONF/$f - fill it in."
        missing=1
    fi
done
[ "$missing" = 0 ] || { echo "Then run this script again."; exit 1; }

podman build -t localhost/second-brain:latest .

mkdir -p "$HOME/.config/containers/systemd"
cp deploy/second-brain.pod deploy/second-brain.container deploy/cloudflared.container \
    "$HOME/.config/containers/systemd/"
loginctl enable-linger "$USER"  # keep services running without a login session
systemctl --user daemon-reload
systemctl --user restart second-brain-pod.service
systemctl --user --no-pager status second-brain.service cloudflared.service | grep -E '●|Active'
