# Installing the prerequisites by hand

`install.sh` does all of this for you. Follow this guide instead when you want
to understand what it changes, when you already run FreeRADIUS and would
rather not have a script touch it, when the installer failed partway and you
need to pick up from the middle, or when you are on a distribution it does not
support.

Commands assume Debian 12 / Ubuntu 22.04 or newer, and root. Versions shown
are what this was developed against — FreeRADIUS 3.2.8, MariaDB 11.8.

Work through the parts in order. Each ends with a check that must pass before
the next one is worth attempting.

---

## 1. MariaDB

```bash
apt update
apt install -y mariadb-server
systemctl enable --now mariadb
```

Optional but sensible on an internet-facing box:

```bash
mariadb-secure-installation
```

On Debian and Ubuntu the `root` database account authenticates through the
unix socket, so `sudo mysql` works with no password and there is no root
password to set. Leave that alone; it is the safer default.

**Check:**

```bash
sudo mysql -e "SELECT VERSION();"
```

---

## 2. FreeRADIUS

```bash
apt install -y freeradius freeradius-mysql freeradius-utils
```

| Package | Why |
|---|---|
| `freeradius` | the server |
| `freeradius-mysql` | `rlm_sql_mysql`, without which the MySQL dialect cannot load |
| `freeradius-utils` | `radtest`, used for every check below |

Installing `freeradius` alone is the most common mistake — the server starts,
then fails to instantiate the SQL module.

**Check:**

```bash
freeradius -v
systemctl status freeradius
```

### 2.1 Create the database and load the schema

```bash
sudo mysql -e "CREATE DATABASE IF NOT EXISTS radius CHARACTER SET utf8mb4;"
sudo mysql radius < /etc/freeradius/3.0/mods-config/sql/main/mysql/schema.sql
```

That creates `radcheck`, `radreply`, `radusergroup`, `radgroupcheck`,
`radgroupreply`, `radacct`, `radpostauth` and `nas`. The schema comes from the
`freeradius` package, so it always matches your server version — do not copy
one from the internet.

**Check:**

```bash
sudo mysql -N -e "SHOW TABLES FROM radius;"
```

### 2.2 Two database accounts

FreeRADIUS needs full rights. The web UI gets much less, so a flaw in it
cannot rewrite accounting history or reach another database.

```bash
# generate two passwords and keep them somewhere for the next steps
openssl rand -base64 24   # for the radius account
openssl rand -base64 24   # for the radweb account
```

```bash
sudo mysql <<'SQL'
CREATE USER IF NOT EXISTS 'radius'@'localhost' IDENTIFIED BY 'RADIUS_PASSWORD_HERE';
GRANT ALL ON radius.* TO 'radius'@'localhost';

CREATE USER IF NOT EXISTS 'radweb'@'localhost' IDENTIFIED BY 'RADWEB_PASSWORD_HERE';
GRANT SELECT,INSERT,UPDATE,DELETE ON radius.radcheck      TO 'radweb'@'localhost';
GRANT SELECT,INSERT,UPDATE,DELETE ON radius.radreply      TO 'radweb'@'localhost';
GRANT SELECT,INSERT,UPDATE,DELETE ON radius.radusergroup  TO 'radweb'@'localhost';
GRANT SELECT,INSERT,UPDATE,DELETE ON radius.radgroupreply TO 'radweb'@'localhost';
GRANT SELECT,INSERT,UPDATE,DELETE ON radius.radgroupcheck TO 'radweb'@'localhost';
GRANT SELECT ON radius.radacct     TO 'radweb'@'localhost';
GRANT SELECT ON radius.radpostauth TO 'radweb'@'localhost';
GRANT SELECT ON radius.nas         TO 'radweb'@'localhost';
FLUSH PRIVILEGES;
SQL
```

**Check** that the narrow account really is narrow:

```bash
sudo mysql -N -e "SHOW GRANTS FOR 'radweb'@'localhost';"
mysql -u radweb -p -e "SELECT COUNT(*) FROM radius.radcheck;"
```

### 2.3 Point FreeRADIUS at MariaDB

**This is the step people miss.** The Debian package ships the SQL module set
to `dialect = "sqlite"` against a file in `/tmp`. FreeRADIUS starts happily,
so nothing looks wrong — but it reads nothing you put in MariaDB, and `/tmp`
means it loses everything on reboot.

Edit `/etc/freeradius/3.0/mods-available/sql`:

```
    dialect = "mysql"
    driver = "rlm_sql_${dialect}"

    server = "localhost"
    port = 3306
    login = "radius"
    password = "RADIUS_PASSWORD_HERE"
    radius_db = "radius"
```

In the same file, find the `mysql { ... }` section and **comment out the whole
`tls { ... }` block inside it**. It names certificate files such as
`/etc/ssl/certs/my_ca.crt` that do not exist on a normal install, and
`rlm_sql_mysql` refuses to instantiate without them:

```
    mysql {
#       tls {
#           ca_file = "/etc/ssl/certs/my_ca.crt"
#           ...
#       }
        warnings = auto
    }
```

The connection is to 127.0.0.1, so nothing is exposed by not using TLS here.

Enable the module and lock the file down — it now holds a password:

```bash
ln -sf /etc/freeradius/3.0/mods-available/sql /etc/freeradius/3.0/mods-enabled/sql
chown root:freerad /etc/freeradius/3.0/mods-available/sql
chmod 640 /etc/freeradius/3.0/mods-available/sql
```

**Check before restarting.** `-XC` validates the configuration without
touching the running server:

```bash
freeradius -XC
```

Look for `Configuration appears to be OK`. If it complains about `ca_file`, the
TLS block is still active.

### 2.4 Order FreeRADIUS after the database

`rlm_sql` refuses to instantiate when the database is unreachable, and
FreeRADIUS then exits rather than starting degraded. The stock unit has no
ordering against MariaDB, so on reboot it is a race — and when it loses, RADIUS
is simply down until someone notices.

```bash
mkdir -p /etc/systemd/system/freeradius.service.d
cat > /etc/systemd/system/freeradius.service.d/after-mariadb.conf <<'EOF'
[Unit]
After=mariadb.service
Wants=mariadb.service

[Service]
Restart=on-failure
RestartSec=5s
EOF

systemctl daemon-reload
systemctl restart freeradius
systemctl enable freeradius
```

`Wants` rather than `Requires`, so a MariaDB package upgrade does not drag
FreeRADIUS down with it.

### 2.5 Prove it end to end

Add a user directly to the database, then authenticate as them:

```bash
sudo mysql radius -e "INSERT INTO radcheck (username,attribute,op,value)
                      VALUES ('testuser','Cleartext-Password',':=','testpass');"

radtest testuser testpass 127.0.0.1 0 testing123
```

`Access-Accept` means FreeRADIUS is genuinely reading MariaDB. If you get
`Access-Reject`, run `freeradius -X` in another terminal and watch the request:
`User not found in radcheck table` while the row plainly exists almost always
means the dialect is still `sqlite`.

`testing123` is the stock shared secret for the `localhost` client in
`/etc/freeradius/3.0/clients.conf`. Change it, and add your real NAS devices
there, before this server sees production traffic.

Clean up:

```bash
sudo mysql radius -e "DELETE FROM radcheck WHERE username='testuser';"
```

---

## 3. Samba and Kerberos — only for Active Directory

Skip this section entirely if you are not using AD. Everything else works
without it.

```bash
apt install -y winbind libnss-winbind samba-common-bin krb5-user
```

You do **not** need the `samba` package. That is the file server; nothing here
shares files. `winbind` provides `winbindd`, `wbinfo` and `ntlm_auth`, which is
all FreeRADIUS uses.

`krb5-user` asks for a default realm during installation. Anything you type is
overwritten later, so accept the default and move on.

### 3.1 Let this host resolve the AD zone

A domain join looks up `_ldap._tcp.<realm>` SRV records. General-purpose
resolvers do not serve those, so joins fail with "cannot find a domain
controller" even though the DC is reachable by ping.

Point **only** the AD zone at the domain controller, keeping normal DNS where
it is:

```bash
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/ad-dns.conf <<'EOF'
[Resolve]
DNS=10.0.0.10
Domains=~ad.example.com
EOF

systemctl restart systemd-resolved
```

The `~` prefix makes it a routing-only domain: queries for `ad.example.com` go
to the DC, everything else keeps using your normal resolver. A domain
controller is rarely a good general resolver, so do not make it one.

**Check both halves:**

```bash
getent hosts dc01.ad.example.com      # AD name resolves
getent hosts deb.debian.org           # normal DNS still works
```

### 3.2 Find the NetBIOS domain name

It is often **not** the first label of the realm, and guessing wrong makes the
join fail confusingly. This needs no credentials:

```bash
net ads lookup -S 10.0.0.10 | grep 'Pre-Win2k Domain'
```

### 3.3 Join

Either use the **Directory** page in the web UI, which does everything below
for you, or by hand:

```bash
hostnamectl set-hostname radius.ad.example.com   # a real FQDN helps
net ads join -U Administrator
```

You will be prompted for the password. Success prints
`Joined 'RADIUS' to dns domain 'ad.example.com'`.

Common failures:

| Message | Cause |
|---|---|
| `Preauthentication failed` | wrong password |
| `Insufficient access` | that account may not join computers |
| `Clock skew too great` | more than 5 minutes off the DC — check `timedatectl` |
| `Cannot find a DC` | DNS, section 3.1 |

Then:

```bash
systemctl enable --now winbind
```

**Check:**

```bash
net ads testjoin     # "Join is OK"
wbinfo -t            # trust secret works
wbinfo -u            # lists domain users
```

### 3.4 Let FreeRADIUS use it

FreeRADIUS reads winbind's privileged pipe, which is group-restricted:

```bash
usermod -aG winbindd_priv freerad
systemctl restart freeradius
```

The restart matters — group membership is only picked up when the process
starts.

In `/etc/freeradius/3.0/mods-available/sql`'s sibling
`/etc/freeradius/3.0/mods-available/mschap`, set:

```
    ntlm_auth = "/usr/bin/ntlm_auth --request-nt-key --allow-mschapv2 --username=%{%{Stripped-User-Name}:-%{%{User-Name}:-None}} --domain=EXAMPLE --challenge=%{%{mschap:Challenge}:-00} --nt-response=%{%{mschap:NT-Response}:-00}"
```

replacing `EXAMPLE` with the NetBIOS name from 3.2.

**Then read this carefully.** Once `ntlm_auth` is configured, `rlm_mschap`
sends **every** MS-CHAP request to the directory — it does not prefer a local
password. Any local user you have will stop authenticating over MS-CHAP unless
you add this to the `authorize` section of **both**
`sites-available/default` and `sites-available/inner-tunnel`, at the end,
after `sql`:

```
    if (&control:Cleartext-Password || &control:NT-Password) {
        update control {
            &MS-CHAP-Use-NTLM-Auth := No
        }
    }
```

That means: if the database already gave us a password, use it; otherwise ask
the directory. CHAP and PAP never enter `rlm_mschap`, so they are unaffected
either way.

**Check** without needing anyone's real password:

```bash
sudo -u freerad ntlm_auth --request-nt-key --domain=EXAMPLE \
     --username=somerealuser --password=DeliberatelyWrong
```

`NT_STATUS_WRONG_PASSWORD` is success for this test — it proves the request
reached the DC and the account exists. `NT_STATUS_NO_SUCH_USER` means the
username is wrong. Anything mentioning the pipe means 3.4's group membership
or the restart is missing.

### 3.5 PAP against the directory

The wiring above covers MS-CHAP only. A PAP request carries the plaintext
password, but `rlm_pap` still needs a known-good password to compare it
against, so a directory account fails with "No Auth-Type found" before the
directory is consulted. A MikroTik hotspot in `http-pap` mode lands exactly
here.

Point the stock exec module at winbind in
`/etc/freeradius/3.0/mods-available/ntlm_auth`:

```
    program = "/usr/bin/ntlm_auth --request-nt-key --domain=EXAMPLE --username=%{%{Stripped-User-Name}:-%{User-Name}} --password=%{User-Password}"
```

Add an `Auth-Type` block inside `authenticate {}` in both virtual servers:

```
    Auth-Type ntlm_auth {
        ntlm_auth
    }
```

And select it in `authorize {}`, extending the rule from 3.4 so it only
applies to accounts with no local password:

```
    if (&control:Cleartext-Password || &control:NT-Password) {
        update control {
            &MS-CHAP-Use-NTLM-Auth := No
        }
    }
    elsif (&User-Password) {
        update control {
            &Auth-Type := ntlm_auth
        }
    }
```

The plaintext reaches `ntlm_auth` in argv, so it is briefly visible in `ps`.
`rlm_exec` cannot feed a child on stdin; use `rlm_ldap` with a bind if that is
unacceptable in your environment.

**Check** — a wrong password must be rejected by the directory rather than
failing earlier:

```
radtest someaduser DeliberatelyWrong 127.0.0.1 0 testing123
```

In `freeradius -X` you should see `Found Auth-Type = ntlm_auth` followed by
`NT_STATUS_WRONG_PASSWORD`. Seeing `No Auth-Type found` instead means the
`authorize` rule did not match.

### 3.6 Which authentication methods work against AD

| Client uses | Local users | AD users |
|---|---|---|
| PAP | yes | yes |
| CHAP | yes | **no** |
| MS-CHAPv2 / PEAP | yes | yes |

CHAP verification needs the cleartext password so the server can compute the
same hash. A directory only answers "is this correct", which requires the
plaintext you never receive under CHAP, and AD will not export NT hashes. No
configuration changes this.

MikroTik hotspot uses CHAP by default. For AD-backed hotspot users:

```
/ip hotspot profile set [find] login-by=http-pap
```

---

## 4. The web UI itself

With the above in place, `install.sh` will detect what exists and skip it. To
do this part by hand too:

```bash
useradd --system --home-dir /opt/freeradius-manager --shell /usr/sbin/nologin radmgr
git clone https://github.com/brhoom98x/freeradius-manager.git /opt/freeradius-manager
cd /opt/freeradius-manager

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Write `.env` (copy `.env.example`), mode 600, owned by `radmgr`:

```bash
cp .env.example .env
chmod 600 .env

# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# ADMIN_PASSWORD_HASH
./venv/bin/python -c \
  "from werkzeug.security import generate_password_hash as h; print(h('yourpassword'))"
```

A certificate, so the domain-join form is not typed over plaintext HTTP:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/server.key -out certs/server.crt -subj "/CN=$(hostname -f)"
chmod 600 certs/server.key
chown -R radmgr:radmgr /opt/freeradius-manager
```

The service unit is a template. Substitute the placeholders:

```bash
sed -e 's|__APP_DIR__|/opt/freeradius-manager|g' \
    -e 's|__SERVICE_USER__|radmgr|g' \
    -e 's|__BIND__|0.0.0.0:8443|g' \
    -e 's|__TLS_ARGS__|--certfile /opt/freeradius-manager/certs/server.crt --keyfile /opt/freeradius-manager/certs/server.key|g' \
    -e 's|__GITHUB_OWNER__|brhoom98x|g' \
    deploy/freeradius-manager.service > /etc/systemd/system/freeradius-manager.service

systemctl daemon-reload
systemctl enable --now freeradius-manager
```

For domain join from the UI, authorise the helper. It must be root-owned and
not writable by the service account, so the app cannot rewrite the one thing
it may run as root:

```bash
chown root:root deploy/radmgr-helper
chmod 750 deploy/radmgr-helper

sed -e 's|__SERVICE_USER__|radmgr|g' -e 's|__APP_DIR__|/opt/freeradius-manager|g' \
    deploy/sudoers.d/freeradius-manager > /tmp/frm-sudoers
visudo -c -f /tmp/frm-sudoers && \
  install -m440 -o root -g root /tmp/frm-sudoers /etc/sudoers.d/freeradius-manager
rm -f /tmp/frm-sudoers
```

Note what the shipped unit does **not** set, and why — see the comments in
`deploy/freeradius-manager.service`. `NoNewPrivileges` must be `false` or sudo
cannot elevate, `ProtectSystem` is `full` rather than `strict` because the
helper writes `/etc`, and `RestrictAddressFamilies` must include `AF_NETLINK`
or every `net` and `wbinfo` call fails with "Could not determine network
interfaces" — which looks exactly like the host is not joined.

If you do not want domain join from the UI, delete the sudoers file and set
`NoNewPrivileges=true` and `ProtectSystem=strict`. Everything else keeps
working.

**Check:**

```bash
systemctl status freeradius-manager
curl -k https://127.0.0.1:8443/healthz     # {"status": "ok"}
cd /opt/freeradius-manager && sudo -u radmgr ./venv/bin/python smoke_db.py
```

`smoke_db.py` exercises every database function, creating and removing a
throwaway group and user. If it ends with `ALL OK`, the grants and schema are
right.

---

## Where things live

| Path | What |
|---|---|
| `/etc/freeradius/3.0/mods-available/sql` | database connection, dialect |
| `/etc/freeradius/3.0/mods-available/mschap` | `ntlm_auth` wiring |
| `/etc/freeradius/3.0/sites-available/{default,inner-tunnel}` | the local-first MS-CHAP rule |
| `/etc/freeradius/3.0/clients.conf` | NAS devices and shared secrets |
| `/etc/krb5.conf`, `/etc/samba/smb.conf` | written by the join |
| `/opt/freeradius-manager/.env` | web UI configuration |
| `/etc/sudoers.d/freeradius-manager` | the single privilege grant |

Logs:

```bash
journalctl -u freeradius-manager -f
journalctl -u freeradius -f
freeradius -X                 # stop the service first; shows every request
```
