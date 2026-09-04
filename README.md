# freeradius-manager

A web UI for FreeRADIUS 3 with a MySQL/MariaDB backend. Manage users, groups
and bandwidth limits in a browser instead of in SQL, and join the server to
Active Directory so domain accounts can authenticate without their passwords
being copied anywhere.

Built for MikroTik deployments — rate limits are written as
`Mikrotik-Rate-Limit` — but everything else is stock FreeRADIUS.

## What it does

- **Users** — add, edit, disable and delete; filter by name; per-user
  bandwidth overrides
- **Groups** — create and edit groups with download/upload limits, session
  timeout, idle timeout and simultaneous-use, all chosen from dropdowns
  rather than typed
- **Active Directory** — join the domain from the UI, then map domain accounts
  to groups so they get policy without a local password
- **Read-only views** — live sessions, accounting and post-auth history, and
  the NAS client list
- **Light, dark and system themes**

## Requirements

- Debian 12+ or Ubuntu 22.04+
- Root access
- 512 MB RAM and a few hundred MB of disk

FreeRADIUS, MariaDB and Samba are installed for you if they are not present.

## Install

```bash
git clone https://github.com/brhoom98x/freeradius-manager.git
cd freeradius-manager
sudo ./install.sh
```

The installer asks for an admin username and password, then:

1. installs FreeRADIUS, MariaDB and the Python dependencies
2. creates the `radius` database and loads the stock FreeRADIUS schema
   (an existing database is detected and left alone)
3. creates two database accounts — one for FreeRADIUS with full rights, one
   for the web UI with deliberately narrow grants
4. switches FreeRADIUS's SQL module from the default SQLite to MariaDB and
   orders it after the database so a reboot cannot race
5. generates a self-signed TLS certificate
6. installs and enables a systemd service

When it finishes it prints the URL. The certificate is self-signed, so your
browser will warn once.

Unattended:

```bash
sudo APP_DIR=/opt/freeradius-manager ADMIN_USER=admin \
     ADMIN_PASSWORD='choose-something-better' ./install.sh
```

Other variables: `SERVICE_USER`, `DB_NAME`, `DB_USER`, `BIND_ADDR`
(default `0.0.0.0:8443`), `ENABLE_TLS=no`, `SKIP_AD=yes`.

Re-running the installer is safe. Your data, your `.env` and your existing
database password are preserved.

## Connecting to Active Directory

Open **Directory** in the UI. It shows the current state and, if the host is
not joined, offers a form: realm, NetBIOS domain, optionally a specific domain
controller, and an account allowed to join computers.

Two things worth knowing before you try:

**The NetBIOS domain is often not the first label of the realm.** A realm of
`ad.example.com` frequently has a NetBIOS name that is nothing like `AD`.
Find it with:

```bash
net ads lookup -S <dc-address> | grep 'Pre-Win2k Domain'
```

**This server must resolve the AD zone.** Domain join needs the `_ldap._tcp`
SRV records, which a general-purpose resolver usually will not serve. If your
DNS server is not the domain controller, route just that one zone to it:

```
# /etc/systemd/resolved.conf.d/ad-dns.conf
[Resolve]
DNS=10.0.0.10
Domains=~ad.example.com
```

Then `systemctl restart systemd-resolved`. This keeps normal DNS on your usual
resolver and sends only the AD zone to the controller.

Once joined, **Users → Map directory account** assigns a domain account to a
group. The account keeps its password in the directory; the rows written here
only tell FreeRADIUS what to reply with.

### What authentication methods work

This is the part that catches people out.

| Client uses | Against local users | Against AD |
|---|---|---|
| PAP | yes | yes |
| CHAP | yes | **no** |
| MS-CHAPv2 / PEAP (802.1X, PPPoE) | yes | yes |

CHAP requires the server to hold the cleartext password so it can compute the
same hash. A directory can only answer "is this password correct", which needs
the plaintext you never receive under CHAP, and Active Directory will not hand
out NT hashes over LDAP. There is no configuration that fixes this.

**MikroTik hotspot uses CHAP by default.** If you want hotspot users
authenticated by AD, switch the hotspot to PAP:

```
/ip hotspot profile set [find] login-by=http-pap
```

Otherwise keep hotspot users as local accounts — they work fine alongside
domain accounts, and the UI shows both.

## How local and directory accounts differ

An account is **local** when the database holds a password for it, and
**directory** when it does not. Only that.

Local users authenticate against the database. Directory users are validated
by `ntlm_auth` against AD, and the rows here carry only their group and
bandwidth policy.

The UI will not let you set a local password on a directory account.
`rlm_mschap` prefers a local password over `ntlm_auth`, so doing that would
silently shadow the directory with a copy that never expires when the user
changes their domain password.

## Security notes

- The web UI has its own sign-in, separate from RADIUS. The password is stored
  as a hash.
- The database account used by the UI cannot touch accounting history beyond
  reading it, and cannot see any other database.
- Joining a domain needs root, so the app runs one root-owned helper through a
  single `sudoers` entry naming that exact path. The helper accepts four verbs,
  validates every value against a strict pattern rather than escaping it, and
  takes the join password on stdin so it never appears in `ps`. The password is
  never written to disk or logged.
- Because `sudo` cannot elevate under `NoNewPrivileges`, the service unit sets
  it to `false` and uses `ProtectSystem=full` rather than `strict`. If you do
  not want domain join from the UI, install with `SKIP_AD=yes`, then set
  `NoNewPrivileges=true` and `ProtectSystem=strict` — everything else keeps
  working.
- **RADIUS stores passwords in cleartext by design.** `Cleartext-Password` is
  what CHAP and MS-CHAP need. Anyone who can read the database can read every
  local user's password. This is a property of RADIUS, not of this app —
  restrict database access accordingly, and prefer directory accounts where you
  can.

## Operating it

```bash
systemctl status freeradius-manager
journalctl -u freeradius-manager -f
```

Configuration lives in `<app dir>/.env` (mode 600). `SECRET_KEY` must stay
stable or every restart signs everyone out.

Change the admin password:

```bash
cd /opt/freeradius-manager
sudo -u radmgr ./venv/bin/python -c \
  "from werkzeug.security import generate_password_hash as h; print(h('newpassword'))"
# put the result in .env as ADMIN_PASSWORD_HASH, then restart the service
```

Templates are compiled once and cached, so edits under `templates/` need a
service restart. Files under `static/` do not.

`smoke_db.py` exercises every database function against the live database,
creating and removing a throwaway group and user:

```bash
cd /opt/freeradius-manager && sudo -u radmgr ./venv/bin/python smoke_db.py
```

## Troubleshooting

**The Directory page says the helper is not reachable.** The sudoers rule is
missing or invalid. Check `/etc/sudoers.d/freeradius-manager` with
`visudo -c -f`.

**Domain join fails with "Could not determine network interfaces".** Samba
enumerates interfaces over netlink. The service unit must allow `AF_NETLINK` in
`RestrictAddressFamilies`; the shipped unit does.

**Domain users authenticate but get no bandwidth limit.** They need mapping —
Users → Map directory account. Authentication and policy are separate.

**Clock skew errors on join.** Kerberos rejects a difference of more than five
minutes from the domain controller. Check `timedatectl`.

## Licence

MIT. See [LICENSE](LICENSE).
