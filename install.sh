#!/bin/sh
# Install the GitHub -> Gitea mirror sync on this machine.
#
# Safe to re-run: an existing mirror.env is never overwritten and the cron
# entry is only added once.
#
#   TARGET=/opt/gitea-mirror ./install.sh   # override install directory
#   CRON_TIME="0 3" ./install.sh            # override schedule (minute hour)
set -eu

SRC="$(dirname "$(readlink -f "$0")")"
TARGET="${TARGET:-$HOME/github-to-gitea-mirror}"
CRON_TIME="${CRON_TIME:-30 4}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- prerequisites ---
command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import json, urllib.request' 2>/dev/null \
    || die "python3 is missing the standard library modules json/urllib"
command -v crontab >/dev/null 2>&1 || die "crontab not found (install cron)"
say "python3 $(python3 -c 'import platform; print(platform.python_version())') ok"

# --- files ---
mkdir -p "$TARGET"
for f in sync_mirrors.py run_sync.sh README.md; do
    [ -f "$SRC/$f" ] || die "missing source file: $f"
    cp "$SRC/$f" "$TARGET/$f"
    # Files may arrive from Windows; CRLF would break the shebang.
    sed -i 's/\r$//' "$TARGET/$f"
done
chmod 700 "$TARGET/sync_mirrors.py" "$TARGET/run_sync.sh"
say "installed scripts in $TARGET"

if [ -f "$TARGET/mirror.env" ]; then
    chmod 600 "$TARGET/mirror.env"
    say "kept existing mirror.env"
    CONFIGURED=1
else
    cp "$SRC/mirror.env.example" "$TARGET/mirror.env"
    sed -i 's/\r$//' "$TARGET/mirror.env"
    chmod 600 "$TARGET/mirror.env"
    say "created $TARGET/mirror.env from the example"
    CONFIGURED=0
fi

# --- cron ---
CRON_LINE="$CRON_TIME * * * $TARGET/run_sync.sh"
if crontab -l 2>/dev/null | grep -Fq "$TARGET/run_sync.sh"; then
    say "cron entry already present"
else
    { crontab -l 2>/dev/null || true; printf '%s\n' "$CRON_LINE"; } | crontab -
    say "cron entry added: $CRON_LINE"
fi

say ""
if [ "$CONFIGURED" = "0" ]; then
    say "Next: edit $TARGET/mirror.env (GITHUB_USER, GITEA_TOKEN, GITEA_OWNER),"
    say "then preview with:"
else
    say "Next: verify with"
fi
say "  cd $TARGET && DRY_RUN=true python3 sync_mirrors.py"
