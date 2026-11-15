#!/bin/sh
# Datenordner dem gewünschten Benutzer geben und Heuhaufen ohne Root-Rechte starten.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
mkdir -p /data
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" /data
  # --no-new-privs: Heuhaufen und alles, was es startet, kann danach keine Rechte mehr dazubekommen
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs "$@"
fi
exec "$@"
