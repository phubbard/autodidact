#!/usr/bin/env bash
# Autodidact installer / updater for a Debian-family host running Caddy (the Pi 5).
#
# Idempotent: run it once to install, run it again to update. Everything lands
# under $PREFIX (default /srv/autodidact):
#
#   src/     git checkout of the repo (cloned from REPO_URL, or pulled if present)
#   venv/    python virtualenv with server/requirements.txt
#   data/    autodidact.db, freshrss_state.json, logs
#   env      secrets: AUTODIDACT_TOKEN (generated), FRESHRSS_*, AUTODIDACT_LLM_URL
#
# plus /etc/systemd/system/autodidact.service, /etc/caddy/conf.d/autodidact.caddy
# (LAN-only reverse proxy) and /etc/cron.d/autodidact (sync + enrichment, only
# the jobs that are configured in env).
#
# Usage (on the Pi):
#   curl -fsSL https://raw.githubusercontent.com/phubbard/autodidact/main/deploy/install.sh | sudo bash
#   # or, from a checkout:
#   sudo deploy/install.sh
#
# Knobs (environment variables, all optional):
#   PREFIX=/srv/autodidact   RUN_USER=<login>   REPO_URL=…   BRANCH=main
#   SITE_HOST=autodidact.<domain>   PORT=8765   LAN_CIDR=a.b.c.0/24,100.64.0.0/10
#   FRESHRSS_URL FRESHRSS_USER FRESHRSS_API_PASSWORD   (written to env if given)
#   AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1  (enables the embed cron)
#   DRY_RUN=1                print what would change, change nothing
set -euo pipefail

PREFIX=${PREFIX:-/srv/autodidact}
REPO_URL=${REPO_URL:-https://github.com/phubbard/autodidact.git}
BRANCH=${BRANCH:-main}
RUN_USER=${RUN_USER:-${SUDO_USER:-$(id -un)}}
PORT=${PORT:-8765}
DRY_RUN=${DRY_RUN:-0}
CADDYFILE=${CADDYFILE:-/etc/caddy/Caddyfile}
CADDY_CONF_DIR=${CADDY_CONF_DIR:-/etc/caddy/conf.d}
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
CRON_FILE=${CRON_FILE:-/etc/cron.d/autodidact}

SRC="$PREFIX/src"; VENV="$PREFIX/venv"; DATA="$PREFIX/data"; ENV_FILE="$PREFIX/env"

# ---------- helpers ----------

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

# write_file <path> <mode> <owner>  (content on stdin); only touches the file if it changed
write_file() {
  local path=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
    rm -f "$tmp"; return 1
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] write %s (%s %s)\n' "$path" "$mode" "$owner"
    diff -u "$path" "$tmp" 2>/dev/null | sed 's/^/        /' || true
    rm -f "$tmp"; return 0
  fi
  install -D -m "$mode" -o "${owner%%:*}" "$tmp" "$path"
  rm -f "$tmp"
  return 0
}

detect_domain() {
  local d
  d=$(hostname -d 2>/dev/null || true)
  [ -n "$d" ] || d=$(hostname -f 2>/dev/null | cut -s -d. -f2-)
  [ -n "$d" ] || d=phfactor.net
  echo "$d"
}

detect_lan_cidr() {
  # The /24 (or whatever) of the primary interface, plus the CGNAT range Tailscale uses.
  local addr
  addr=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | head -1)
  if [ -n "$addr" ] && command -v python3 >/dev/null; then
    python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$addr"
  else
    echo "192.168.0.0/16 10.0.0.0/8"
  fi
}

SITE_HOST=${SITE_HOST:-autodidact.$(detect_domain)}
LAN_CIDR=${LAN_CIDR:-"$(detect_lan_cidr) 100.64.0.0/10"}
LAN_CIDR=${LAN_CIDR//,/ }

# ---------- preflight ----------

[ "$(id -u)" = 0 ] || [ "$DRY_RUN" = 1 ] || die "run with sudo (needs to write systemd, caddy and cron files)"
id "$RUN_USER" >/dev/null 2>&1 || die "RUN_USER '$RUN_USER' does not exist"
command -v git >/dev/null || die "git is required (apt install git)"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is required (apt install python3-venv)"
python3 -c 'import sqlite3; c=sqlite3.connect(":memory:"); c.execute("create virtual table t using fts5(a)")' \
  2>/dev/null || die "this python's sqlite3 lacks FTS5"

say "Autodidact → $PREFIX  (user $RUN_USER, site https://$SITE_HOST, LAN $LAN_CIDR)"
[ "$DRY_RUN" = 1 ] && warn "DRY_RUN=1: nothing will be changed"

# ---------- code ----------

run install -d -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" "$PREFIX" "$DATA"
if [ -d "$SRC/.git" ]; then
  say "Updating checkout ($BRANCH)"
  run sudo -u "$RUN_USER" git -C "$SRC" fetch -q origin "$BRANCH"
  run sudo -u "$RUN_USER" git -C "$SRC" checkout -q "$BRANCH"
  run sudo -u "$RUN_USER" git -C "$SRC" merge -q --ff-only "origin/$BRANCH"
else
  say "Cloning $REPO_URL"
  run sudo -u "$RUN_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$SRC"
fi

# ---------- python ----------

if [ ! -x "$VENV/bin/python" ]; then
  say "Creating virtualenv"
  run sudo -u "$RUN_USER" python3 -m venv "$VENV"
fi
say "Installing requirements"
run sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
run sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -r "$SRC/server/requirements.txt"

# ---------- env (secrets) ----------

if [ ! -f "$ENV_FILE" ]; then
  say "Generating $ENV_FILE with a fresh AUTODIDACT_TOKEN"
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
  write_file "$ENV_FILE" 600 "$RUN_USER" <<EOF || true
# Autodidact secrets. Loaded by the systemd unit and the cron jobs. chmod 600.
AUTODIDACT_TOKEN=$TOKEN
AUTODIDACT_DB=$DATA/autodidact.db
AUTODIDACT_URL=http://127.0.0.1:$PORT
# FreshRSS (Google Reader API; enable API access in FreshRSS admin, set an API password on your profile):
#FRESHRSS_URL=https://rss.example.net
#FRESHRSS_USER=
#FRESHRSS_API_PASSWORD=
# LM Studio on Axiom, for embed.py (uncomment to enable the enrichment cron):
#AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1
EOF
fi

# Fold any FRESHRSS_* / AUTODIDACT_LLM_URL given on the command line into env.
set_env() {  # set_env KEY VALUE  — replace, un-comment or append
  local key=$1 val=$2
  [ -n "$val" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then printf '    [dry-run] set %s in %s\n' "$key" "$ENV_FILE"; return 0; fi
  if grep -qE "^#?$key=" "$ENV_FILE"; then
    sed -i -E "s|^#?$key=.*|$key=$val|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}
set_env FRESHRSS_URL "${FRESHRSS_URL:-}"
set_env FRESHRSS_USER "${FRESHRSS_USER:-}"
set_env FRESHRSS_API_PASSWORD "${FRESHRSS_API_PASSWORD:-}"
set_env AUTODIDACT_LLM_URL "${AUTODIDACT_LLM_URL:-}"
[ "$DRY_RUN" = 1 ] || { chmod 600 "$ENV_FILE"; chown "$RUN_USER" "$ENV_FILE"; }

# Read env back (for the cron decisions and the summary). Tolerate a missing file in dry-run.
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi

# ---------- systemd ----------

say "Installing systemd unit"
UNIT_CHANGED=0
write_file "$SYSTEMD_DIR/autodidact.service" 644 root <<EOF && UNIT_CHANGED=1
[Unit]
Description=Autodidact ingest and search server
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$SRC/server
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/gunicorn -w 2 --threads 4 -b 127.0.0.1:$PORT --access-logfile - 'app:create_app()'
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
run systemctl daemon-reload
run systemctl enable -q autodidact
if systemctl is-active -q autodidact 2>/dev/null; then
  say "Restarting autodidact"
  run systemctl restart autodidact
else
  say "Starting autodidact"
  run systemctl start autodidact
fi

# ---------- caddy ----------

LAN_MATCH=$(printf ' %s' $LAN_CIDR)
if [ -f "$CADDYFILE" ] && command -v caddy >/dev/null; then
  say "Installing Caddy site $SITE_HOST → 127.0.0.1:$PORT (LAN only)"
  write_file "$CADDY_CONF_DIR/autodidact.caddy" 644 root <<EOF || true
# Managed by autodidact/deploy/install.sh — edits will be overwritten on the next run.
$SITE_HOST {
	@lan remote_ip 127.0.0.1 ::1$LAN_MATCH
	handle @lan {
		reverse_proxy 127.0.0.1:$PORT
	}
	respond 403
}
EOF
  if ! grep -qF "import $CADDY_CONF_DIR/" "$CADDYFILE" && ! grep -qE "^\s*import\s+conf\.d/" "$CADDYFILE"; then
    say "Adding 'import $CADDY_CONF_DIR/*.caddy' to $CADDYFILE (backup at $CADDYFILE.bak)"
    if [ "$DRY_RUN" = 1 ]; then
      printf '    [dry-run] append import line to %s\n' "$CADDYFILE"
    else
      cp -p "$CADDYFILE" "$CADDYFILE.bak"
      printf '\nimport %s/*.caddy\n' "$CADDY_CONF_DIR" >> "$CADDYFILE"
    fi
  fi
  if [ "$DRY_RUN" != 1 ]; then
    if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      systemctl reload caddy
    else
      warn "caddy validate failed; not reloading. Check: caddy validate --config $CADDYFILE"
      caddy validate --config "$CADDYFILE" --adapter caddyfile 2>&1 | tail -5 >&2 || true
    fi
  fi
else
  warn "No $CADDYFILE or no caddy binary; skipping the reverse proxy. Site block to add by hand:"
  printf '    %s {\n      @lan remote_ip 127.0.0.1 ::1%s\n      handle @lan { reverse_proxy 127.0.0.1:%s }\n      respond 403\n    }\n' "$SITE_HOST" "$LAN_MATCH" "$PORT"
fi

# ---------- cron ----------

say "Installing cron jobs"
{
  echo "# Managed by autodidact/deploy/install.sh. Secrets come from $ENV_FILE."
  echo "SHELL=/bin/bash"
  echo "PATH=/usr/local/bin:/usr/bin:/bin"
  if [ -n "${FRESHRSS_URL:-}" ]; then
    echo "17 * * * * $RUN_USER cd $SRC/server && set -a && . $ENV_FILE && set +a && $VENV/bin/python freshrss_sync.py --state $DATA/freshrss_state.json --days 7 >> $DATA/freshrss.log 2>&1"
  else
    echo "# FreshRSS sync disabled: set FRESHRSS_URL/USER/API_PASSWORD in $ENV_FILE and re-run install.sh"
  fi
  if [ -n "${AUTODIDACT_LLM_URL:-}" ]; then
    echo "*/15 * * * * $RUN_USER cd $SRC/server && set -a && . $ENV_FILE && set +a && $VENV/bin/python embed.py --batch 50 >> $DATA/embed.log 2>&1"
  else
    echo "# enrichment disabled: set AUTODIDACT_LLM_URL in $ENV_FILE and re-run install.sh"
  fi
} | write_file "$CRON_FILE" 644 root || true

# ---------- smoke test ----------

if [ "$DRY_RUN" != 1 ]; then
  say "Smoke test"
  ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if out=$(curl -fsS "http://127.0.0.1:$PORT/stats" 2>/dev/null); then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" = 1 ]; then
    echo "    local:  $out"
  else
    warn "server did not answer on 127.0.0.1:$PORT; see: journalctl -u autodidact -n 50"
  fi
  if getent hosts "$SITE_HOST" >/dev/null; then
    if out=$(curl -fsS --max-time 10 "https://$SITE_HOST/stats" 2>&1); then
      echo "    https:  $out"
    else
      warn "https://$SITE_HOST/stats failed ($out). If this is the first run, Caddy may still be fetching a certificate; check: journalctl -u caddy -n 30"
    fi
  else
    warn "$SITE_HOST does not resolve. Add a DNS record pointing at this host (Pi-hole / UCG local DNS), then Caddy will serve it."
  fi
fi

# ---------- summary ----------

cat <<EOF

Done. Extension settings:
    Server URL:   https://$SITE_HOST
    Bearer token: ${AUTODIDACT_TOKEN:-<see $ENV_FILE>}

Useful:
    systemctl status autodidact          journalctl -u autodidact -f
    $ENV_FILE                            (secrets; re-run install.sh after editing to refresh cron)
    sudo -u $RUN_USER bash -c 'set -a; . $ENV_FILE; set +a; cd $SRC/server && $VENV/bin/python freshrss_sync.py --dry-run --days 3'
    sudo $SRC/deploy/install.sh          (update: pulls $BRANCH, reinstalls, restarts)
EOF
