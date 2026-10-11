#!/bin/sh
# Startet als root, sorgt dafür, dass das Datenverzeichnis beschreibbar ist
# (Bind-Mounts wie ./data legt Docker als root an) und läuft dann ohne Root-Rechte weiter.
set -e
DATA_DIR="${DATA_DIR:-/data}"
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
fi
exec "$@"
