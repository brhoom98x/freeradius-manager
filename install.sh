#!/usr/bin/env bash
#
# freeradius-manager installer -- Debian / Ubuntu.
#
#   sudo ./install.sh
#
# Installs the web UI, creates the RADIUS database if it is not already there,
# points FreeRADIUS at it, and starts everything on boot. Re-running is safe:
# an existing database, its data and an existing .env are left alone.
#
# Everything can be preset from the environment for unattended installs:
#   APP_DIR SERVICE_USER DB_NAME DB_USER RADIUS_DB_USER BIND_ADDR
#   ADMIN_USER ADMIN_PASSWORD ENABLE_TLS SKIP_AD SKIP_RADIUS_CONF
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/freeradius-manager}"
SERVICE_USER="${SERVICE_USER:-radmgr}"
DB_NAME="${DB_NAME:-radius}"
DB_USER="${DB_USER:-radweb}"           # the web UI: narrow grants
RADIUS_DB_USER="${RADIUS_DB_USER:-radius}"  # FreeRADIUS itself: full on the DB
BIND_ADDR="${BIND_ADDR:-0.0.0.0:8443}"
ENABLE_TLS="${ENABLE_TLS:-yes}"
SKIP_AD="${SKIP_AD:-no}"
# Set to yes when FreeRADIUS is already pointed at a database you configured
# yourself and you only want the web UI installed.
SKIP_RADIUS_CONF="${SKIP_RADIUS_CONF:-no}"
SERVICE_NAME="freeradius-manager"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FR_DIR="/etc/freeradius/3.0"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
command -v apt-get >/dev/null || die "this installer supports Debian and Ubuntu"

rand() { tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c "${1:-32}" || true; }
# `head` closing the pipe raises SIGPIPE, which `set -o pipefail` treats as
# failure, so generate secrets without a pipe.
secret() { python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range($1)))"; }

# --- 1. packages -------------------------------------------------------------

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 python3-venv python3-pip mariadb-server freeradius freeradius-mysql openssl"
if [ "$SKIP_AD" != "yes" ]; then
    PKGS="$PKGS winbind libnss-winbind samba-common-bin krb5-user"
fi
apt-get update -qq
apt-get install -y -qq $PKGS
info "done"

systemctl enable --now mariadb >/dev/null 2>&1 || true

# --- 2. service account and files -------------------------------------------

say "Creating $SERVICE_USER and $APP_DIR"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    info "created system user $SERVICE_USER"
else
    info "user $SERVICE_USER already exists"
fi

mkdir -p "$APP_DIR"
# copy the application, but never clobber an existing .env
for item in app.py db.py ad.py smoke_db.py requirements.txt templates static deploy; do
    [ -e "$SRC_DIR/$item" ] && cp -r "$SRC_DIR/$item" "$APP_DIR/"
done
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
info "installed to $APP_DIR"

# --- 3. python environment ---------------------------------------------------

say "Building the Python environment"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/venv"
info "done"

# --- 4. database -------------------------------------------------------------

say "Preparing the $DB_NAME database"
mysql -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4;"

SCHEMA="$FR_DIR/mods-config/sql/main/mysql/schema.sql"
if ! mysql -N -e "SHOW TABLES FROM \`$DB_NAME\` LIKE 'radcheck';" | grep -q radcheck; then
    [ -f "$SCHEMA" ] || die "FreeRADIUS MySQL schema not found at $SCHEMA"
    mysql "$DB_NAME" < "$SCHEMA"
    info "loaded the FreeRADIUS schema"
else
    info "schema already present, leaving the data alone"
fi

# FreeRADIUS's own account: full rights on the RADIUS database.
#
# Its password is only rotated when we are also going to write the new value
# into the FreeRADIUS config below. Rotating it while SKIP_RADIUS_CONF=yes
# would leave the server holding a password that no longer works, and RADIUS
# would fail at its next restart rather than immediately -- long after anyone
# would connect the two events.
RADIUS_USER_EXISTS=$(mysql -N -e "SELECT 1 FROM mysql.user WHERE user='$RADIUS_DB_USER' AND host='localhost';")
mysql -e "CREATE USER IF NOT EXISTS '$RADIUS_DB_USER'@'localhost';
          GRANT ALL ON \`$DB_NAME\`.* TO '$RADIUS_DB_USER'@'localhost';"

if [ "$SKIP_RADIUS_CONF" = "yes" ] && [ -n "$RADIUS_USER_EXISTS" ]; then
    RADIUS_DB_PASS=""
    info "leaving the existing '$RADIUS_DB_USER' password alone"
else
    RADIUS_DB_PASS="$(secret 32)"
    mysql -e "SET PASSWORD FOR '$RADIUS_DB_USER'@'localhost' = PASSWORD('$RADIUS_DB_PASS');"
    if [ "$SKIP_RADIUS_CONF" = "yes" ]; then
        info "created '$RADIUS_DB_USER'; put this password in the FreeRADIUS sql module:"
        info "    $RADIUS_DB_PASS"
    fi
fi

# The web UI's account: it must never be able to read or write anything else.
# Accounting and post-auth are history, so they stay read-only.
if [ -f "$APP_DIR/.env" ] && grep -q '^DB_PASS=' "$APP_DIR/.env"; then
    DB_PASS="$(grep '^DB_PASS=' "$APP_DIR/.env" | cut -d= -f2-)"
    info "reusing the existing web database password"
else
    DB_PASS="$(secret 32)"
fi
mysql -e "CREATE USER IF NOT EXISTS '$DB_USER'@'localhost';
          SET PASSWORD FOR '$DB_USER'@'localhost' = PASSWORD('$DB_PASS');
          GRANT SELECT,INSERT,UPDATE,DELETE ON \`$DB_NAME\`.radcheck      TO '$DB_USER'@'localhost';
          GRANT SELECT,INSERT,UPDATE,DELETE ON \`$DB_NAME\`.radreply      TO '$DB_USER'@'localhost';
          GRANT SELECT,INSERT,UPDATE,DELETE ON \`$DB_NAME\`.radusergroup  TO '$DB_USER'@'localhost';
          GRANT SELECT,INSERT,UPDATE,DELETE ON \`$DB_NAME\`.radgroupreply TO '$DB_USER'@'localhost';
          GRANT SELECT,INSERT,UPDATE,DELETE ON \`$DB_NAME\`.radgroupcheck TO '$DB_USER'@'localhost';
          GRANT SELECT ON \`$DB_NAME\`.radacct    TO '$DB_USER'@'localhost';
          GRANT SELECT ON \`$DB_NAME\`.radpostauth TO '$DB_USER'@'localhost';
          GRANT SELECT ON \`$DB_NAME\`.nas         TO '$DB_USER'@'localhost';
          FLUSH PRIVILEGES;"
info "database users configured"

# --- 5. point FreeRADIUS at the database -------------------------------------

say "Pointing FreeRADIUS at $DB_NAME"
SQLCONF="$FR_DIR/mods-available/sql"
if [ "$SKIP_RADIUS_CONF" = "yes" ]; then
    info "SKIP_RADIUS_CONF=yes, leaving the FreeRADIUS configuration alone"
elif [ -f "$SQLCONF" ]; then
    cp -a "$SQLCONF" "/root/sql.conf.bak.$(date +%Y%m%d-%H%M%S)"
    RPW="$RADIUS_DB_PASS" DBN="$DB_NAME" DBU="$RADIUS_DB_USER" python3 - "$SQLCONF" <<'PY'
import os, re, sys
path = sys.argv[1]
text = open(path).read()
text = text.replace('\tdialect = "sqlite"', '\tdialect = "mysql"', 1)
text = re.sub(r'^\tserver = ".*"$',    '\tserver = "localhost"',              text, flags=re.M)
text = re.sub(r'^\tport = .*$',        '\tport = 3306',                       text, flags=re.M)
text = re.sub(r'^\tlogin = ".*"$',     '\tlogin = "%s"' % os.environ["DBU"],  text, flags=re.M)
text = re.sub(r'^\tpassword = ".*"$',  '\tpassword = "%s"' % os.environ["RPW"], text, flags=re.M)
text = re.sub(r'^\tradius_db = ".*"$', '\tradius_db = "%s"' % os.environ["DBN"], text, flags=re.M)

# The mysql{} tls block names certificate paths that do not exist on a stock
# install, and rlm_sql_mysql refuses to instantiate without them. The
# connection is to 127.0.0.1.
lines = text.splitlines(True)
try:
    start = next(i for i, l in enumerate(lines) if l.strip() == "mysql {")
except StopIteration:
    start = None
if start is not None:
    depth = 0
    for end in range(start, len(lines)):
        depth += lines[end].count("{") - lines[end].count("}")
        if depth == 0:
            break
    out, i = lines[:start], start
    while i <= end:
        if lines[i].strip() == "tls {":
            d = 0
            while i <= end:
                d += lines[i].count("{") - lines[i].count("}")
                out.append("#" + lines[i]); i += 1
                if d == 0:
                    break
            continue
        out.append(lines[i]); i += 1
    lines = out + lines[end + 1:]
open(path, "w").writelines(lines)
PY
    ln -sf "$SQLCONF" "$FR_DIR/mods-enabled/sql"
    chown root:freerad "$SQLCONF"; chmod 640 "$SQLCONF"

    # rlm_sql refuses to start when the database is unreachable, so order
    # FreeRADIUS after MariaDB or a boot race leaves RADIUS down
    mkdir -p /etc/systemd/system/freeradius.service.d
    cat > /etc/systemd/system/freeradius.service.d/after-mariadb.conf <<'EOF'
[Unit]
After=mariadb.service
Wants=mariadb.service

[Service]
Restart=on-failure
RestartSec=5s
EOF
    if freeradius -XC >/tmp/fr-check.log 2>&1; then
        systemctl daemon-reload
        systemctl enable freeradius >/dev/null 2>&1 || true
        systemctl restart freeradius || true
        info "FreeRADIUS now reads $DB_NAME"
    else
        tail -5 /tmp/fr-check.log
        info "WARNING: FreeRADIUS config check failed; see /tmp/fr-check.log"
        info "the web UI will still work, but RADIUS may not be using the database"
    fi
else
    info "FreeRADIUS SQL module not found; skipping"
fi

# --- 6. TLS ------------------------------------------------------------------

TLS_ARGS=""
if [ "$ENABLE_TLS" = "yes" ]; then
    say "TLS certificate"
    CERT="$APP_DIR/certs/server.crt"
    KEY="$APP_DIR/certs/server.key"
    if [ ! -f "$CERT" ]; then
        mkdir -p "$APP_DIR/certs"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$KEY" -out "$CERT" \
            -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
            -addext "subjectAltName=DNS:$(hostname -f 2>/dev/null || hostname),IP:127.0.0.1" \
            >/dev/null 2>&1
        info "generated a self-signed certificate, valid 10 years"
        info "replace $CERT / $KEY with a real pair when you have one"
    else
        info "certificate already present"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/certs"
    chmod 700 "$APP_DIR/certs"; chmod 600 "$KEY"
    TLS_ARGS="--certfile $CERT --keyfile $KEY"
fi

# --- 7. application config ---------------------------------------------------

say "Writing $APP_DIR/.env"
if [ ! -f "$APP_DIR/.env" ]; then
    : > "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"; chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$APP_DIR/.env"; then
        # keep whatever is already there; secrets must survive a re-run
        return
    fi
    printf '%s=%s\n' "$key" "$value" >> "$APP_DIR/.env"
}
force_env() {
    sed -i "/^$1=/d" "$APP_DIR/.env"
    printf '%s=%s\n' "$1" "$2" >> "$APP_DIR/.env"
}

set_env DB_HOST 127.0.0.1
set_env DB_NAME "$DB_NAME"
set_env DB_USER "$DB_USER"
force_env DB_PASS "$DB_PASS"
set_env SECRET_KEY "$(secret 48)"

if [ -z "${ADMIN_USER:-}" ] && ! grep -q '^ADMIN_USER=' "$APP_DIR/.env"; then
    read -rp "    Admin username for the web UI [admin]: " ADMIN_USER </dev/tty || true
    ADMIN_USER="${ADMIN_USER:-admin}"
fi
if [ -n "${ADMIN_USER:-}" ]; then
    if [ -z "${ADMIN_PASSWORD:-}" ]; then
        read -rsp "    Admin password: " ADMIN_PASSWORD </dev/tty; echo
        read -rsp "    Repeat: " ADMIN_PASSWORD2 </dev/tty; echo
        [ "$ADMIN_PASSWORD" = "${ADMIN_PASSWORD2:-}" ] || die "passwords did not match"
    fi
    [ -n "$ADMIN_PASSWORD" ] || die "admin password cannot be empty"
    HASH="$("$APP_DIR/venv/bin/python" - "$ADMIN_PASSWORD" <<'PY'
import sys
from werkzeug.security import generate_password_hash
print(generate_password_hash(sys.argv[1]))
PY
)"
    force_env ADMIN_USER "$ADMIN_USER"
    force_env ADMIN_PASSWORD_HASH "$HASH"
    info "admin login set to '$ADMIN_USER'"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

# --- 8. privileged helper ----------------------------------------------------

if [ "$SKIP_AD" != "yes" ]; then
    say "Authorising the domain-join helper"
    HELPER="$APP_DIR/deploy/radmgr-helper"
    # root-owned and not writable by the service account, so the app cannot
    # rewrite the one thing it is allowed to run as root
    chown root:root "$HELPER"
    chmod 750 "$HELPER"
    sed -e "s|__SERVICE_USER__|$SERVICE_USER|g" -e "s|__APP_DIR__|$APP_DIR|g" \
        "$APP_DIR/deploy/sudoers.d/freeradius-manager" > /tmp/frm-sudoers
    if visudo -c -q -f /tmp/frm-sudoers; then
        install -m440 -o root -g root /tmp/frm-sudoers /etc/sudoers.d/freeradius-manager
        info "the UI may now join this host to a domain"
    else
        info "WARNING: generated sudoers rule was invalid; domain join from the UI is disabled"
    fi
    rm -f /tmp/frm-sudoers
    # FreeRADIUS needs to read winbind's privileged pipe for MSCHAPv2
    getent group winbindd_priv >/dev/null && usermod -aG winbindd_priv freerad || true
fi

# --- 9. service --------------------------------------------------------------

say "Installing the $SERVICE_NAME service"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__BIND__|$BIND_ADDR|g" \
    -e "s|__TLS_ARGS__|$TLS_ARGS|g" \
    -e "s|__GITHUB_OWNER__|brhoom98x|g" \
    "$APP_DIR/deploy/$SERVICE_NAME.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
sleep 3

SCHEME=$([ -n "$TLS_ARGS" ] && echo https || echo http)
PORT="${BIND_ADDR##*:}"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    say "Done"
    info "Open  $SCHEME://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
    info "Sign in as '${ADMIN_USER:-the admin user you configured}'"
    [ -n "$TLS_ARGS" ] && info "The certificate is self-signed, so the browser will warn once."
    info ""
    info "Logs:    journalctl -u $SERVICE_NAME -f"
    info "Config:  $APP_DIR/.env"
else
    journalctl -u "$SERVICE_NAME" --no-pager -n 20 || true
    die "the service did not start; see the log above"
fi
