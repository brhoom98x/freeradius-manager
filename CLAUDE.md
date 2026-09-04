# FreeRADIUS User Management UI

## Context
Manages users in an existing FreeRADIUS 3.x SQL backend on this same host.
FreeRADIUS owns the schema. This app is a CRUD front-end over it and must
NOT alter the schema, add tables, or add columns.

## Stack
- Python 3 + Flask, single venv in ./venv
- PyMySQL
- Server-rendered Jinja2 templates. No React, no build step, no JS framework.
- Served by gunicorn under systemd as `radius-ui.service`, bound to
  0.0.0.0:5000. Reachable on the lab VLAN, so the app has its own sign-in.

## Database
MariaDB, database `radius`, user `radweb` (credentials in .env, never in code).
Grants:
- read/write: radcheck, radreply, radusergroup, radgroupreply, radgroupcheck
- read-only:  radacct, radpostauth, nas
The app must not attempt writes to read-only tables.

## Data model rules — these are FreeRADIUS conventions, follow them exactly
- A user's password is a row in radcheck:
  attribute = 'Cleartext-Password', op = ':=', value = <password>
- A user is disabled with a radcheck row: 'Auth-Type', ':=', 'Reject'
- Per-user reply attributes live in radreply, op = '='
- Group membership is a row in radusergroup (username, groupname, priority)
- Group reply policy lives in radgroupreply, op = '='
- Group check policy (e.g. Simultaneous-Use) lives in radgroupcheck, op = ':='
- Prefer group assignment over per-user radreply rows; a per-user row is an
  override and should be the exception
- A group is not a table row of its own — it exists precisely as long as some
  row in radgroupreply, radgroupcheck or radusergroup names it. Creating a
  group means writing at least one policy row; deleting one means removing
  its policy rows, which is refused while radusergroup still has members.
- An attribute set to empty in the UI means "delete the row", not "store an
  empty string", so the attribute is simply absent from the RADIUS reply
- Deleting a user means deleting its rows from radcheck, radreply and
  radusergroup — all three, in one transaction

## Local and directory accounts
Two kinds of account share the users list, told apart by one thing only:

- **local** -- radcheck holds a `Cleartext-Password` for it
- **directory** -- it appears in radusergroup/radreply/radcheck but has *no*
  `Cleartext-Password`. These are Active Directory accounts, validated by
  `rlm_mschap` through `ntlm_auth`; the rows here exist only to give FreeRADIUS
  policy to reply with.

**Never write a Cleartext-Password for a directory account.** rlm_mschap
prefers a local password over ntlm_auth, so doing so silently shadows the
directory: the account would keep authenticating against a stale copy after
its AD password changed. `change_password` refuses for that reason, and
`add_directory_user` never writes one.

A directory account is only its group and policy rows, so `update_user`
refuses to clear both at once -- that would delete the mapping through a form
that says "save". Removing one is an explicit delete. Disabling still works:
an `Auth-Type := Reject` row is evaluated before ntlm_auth is ever called.

`ad.py` reads the directory through `wbinfo` so the app needs no credentials
of its own -- the unprivileged winbind pipe covers name lookup and
enumeration. Every call degrades to empty on failure: if winbind is stopped or
the DC is down, the picker becomes a plain text field and the existence check
is skipped rather than blocking the operator. Do not add an LDAP bind account
for this; it would be a second credential to rotate for no extra capability.

## Managed attributes
`db.GROUP_ATTRS` is the single source of truth for which attributes the UI
exposes, which table each lives in, its operator, and which control renders it
(`widget`: `rate`, `duration` or `count`). Add an attribute there and it
appears in the group form, the group list and validation automatically.
Attributes present on a group but absent from that list are shown read-only on
the group page and are never touched on save.

Rate limiting is `Mikrotik-Rate-Limit`, matching the MikroTik gear this server
authenticates. MikroTik writes it as `rx/tx` from the router's point of view,
so **rx is the client's upload and tx is its download**. The UI says upload and
download and does that mapping in exactly one place, `db.compose_rate_limit`
and `db.split_rate_limit`. Never reorder those halves anywhere else.

## Form controls
Values are chosen, not typed. Each managed attribute renders as a preset
dropdown with a `Custom…` option that reveals a free field, built by the macros
in `templates/_fields.html`. The rate picker has separate download and upload
sides; leaving one blank mirrors the other.

A rate limit carrying burst parameters cannot be expressed with two dropdowns,
so `split_rate_limit` returns None for it and the picker falls back to a raw
text field. That path must keep working — it is the only way to edit a burst
config without destroying it.

`static/app.js` is progressive enhancement only. Every form submits correctly
with JavaScript off: the `Custom…` fields are ordinary inputs that are simply
always visible, and the script only hides and disables the ones that are not
in play. Do not move validation or value assembly into it — the server reads
the same field names either way.

The option lists reach the templates through `app.jinja_env.globals`, not a
context processor. Macros imported with `{% from %}` do not see the request
context, and a context processor silently hands them empty lists.

## Theming
Light, dark, and follow-the-system, chosen in the header and remembered in
localStorage. The light palette is the base on bare `:root`; dark is declared
twice, once behind `prefers-color-scheme` guarded with
`:not([data-theme="light"])` and once under `[data-theme="dark"]`, so the
toggle wins in both directions. No colour is defined only inside a media
query. A small inline script in `<head>` applies the stored choice before
first paint, otherwise dark-mode users get a white flash on every navigation.

Icons are inline SVG macros in `templates/_icons.html`, inheriting
`currentColor` so they follow the theme. They are not loaded from a CDN
because this host sits on a management VLAN with no internet route.

## Scope
1. Sign-in (single admin account, credentials in .env)
2. Overview: user/group/session counts, 24h accept-reject split
3. List users with group, effective rate limit and enabled state; filter by name
4. Add user (username, password, group)
5. Edit user: group assignment and per-user rate-limit override
5b. Map an AD account to a group, so directory users get policy without a
    local password
6. Enable / disable a user
7. Change a user's password
8. Delete a user
9. List, create, edit and delete groups, including rate limit, session
   timeout, idle timeout and simultaneous-use
10. Active sessions (read-only)
11. NAS / clients list (read-only)
12. Last 50 rows of radacct and radpostauth (read-only). The post-auth view
    deliberately has no password column -- see "Never log the password" below
13. Directory page: join this host to Active Directory, leave it, and re-apply
    the FreeRADIUS wiring, through the privileged helper

## The privileged helper
The app runs unprivileged and may run exactly one program as root:
`deploy/radmgr-helper`, under a single sudoers entry naming that absolute path.
It accepts four verbs and nothing else. Rules for changing it:

- Validate every value against a strict pattern and *reject* what does not
  match. Do not escape and pass through. It runs as root on behalf of a web
  form.
- Never build a shell command string. `subprocess` always gets an argument
  list.
- Secrets arrive in the JSON payload on stdin, never in argv, where `ps` would
  expose them. They are not logged, not echoed back in errors (see `_scrub`),
  and not written to disk.
- The helper must stay root-owned and not writable by the service account, or
  the app could rewrite the one thing it is allowed to run as root.

Two systemd settings cannot be tightened while this feature exists, and the
unit says so: `NoNewPrivileges` must be false or sudo cannot elevate, and
`RestrictAddressFamilies` must include `AF_NETLINK` or every `net`/`wbinfo`
call fails with "Could not determine network interfaces" -- which surfaces in
the UI as the host simply not being joined. `ProtectSystem` is `full`, not
`strict`, because the helper writes /etc.

## How each authentication method reaches the directory
Joining wires two separate paths, and they are easy to confuse:

- **MS-CHAP** -- `rlm_mschap`'s own `ntlm_auth` setting. Once set, rlm_mschap
  sends *every* MS-CHAP request to the directory, so both virtual servers carry
  a rule setting `MS-CHAP-Use-NTLM-Auth := No` whenever SQL already supplied a
  password. Without it, local users stop authenticating.
- **PAP** -- the separate `ntlm_auth` exec module, selected by an `elsif
  (&User-Password)` branch on that same rule. rlm_pap needs a known-good
  password to compare against, so a directory account would otherwise fail with
  "No Auth-Type found" before the directory was consulted. MikroTik hotspots in
  http-pap mode land here.
- **CHAP** -- cannot work against a directory at all, and no configuration
  changes that. Verifying CHAP needs the cleartext password, which the client
  never sends and AD never discloses. MikroTik hotspot uses CHAP by default.

`configure_pap` notes the one wart: the plaintext reaches ntlm_auth in argv,
because rlm_exec cannot feed a child on stdin. Documented rather than hidden.

## Never log the password
FreeRADIUS's stock post-auth query stores whatever the client sent. Under CHAP
that was a useless hash; under PAP it is the real password, and for a directory
account a working domain credential. `install.sh` blanks that field in every
dialect's `queries.conf`, and the Post-Auth page has no password column. Do not
add one back.

When clearing rows that still hold one, `authdate` must be assigned to itself:

    UPDATE radpostauth SET pass = '', authdate = authdate;

The column is `ON UPDATE current_timestamp`, so a plain `SET pass = ''`
rewrites every timestamp to the moment it runs, destroying the audit trail the
change exists to protect.

## Out of scope
Multiple admin accounts or roles, per-admin audit log, editing NAS entries
or shared secrets, charts, bulk import, REST API, Docker.

## Constraints
- Parameterised SQL queries only, never string interpolation. Where a table
  name must be interpolated it comes from GROUP_ATTRS, never from user input.
- Reject usernames that already exist in radcheck
- Every DB call wrapped in error handling that shows the real error
- Keep it in few files: app.py, db.py, ad.py, templates/. Readability over
  cleverness. ad.py is separate because it is the only thing that talks to
  something other than the database.

## Operations
- `deploy/setup.sh <admin-user> <admin-password>` installs dependencies,
  generates SECRET_KEY, and writes the admin credential hash into .env.
  SECRET_KEY must stay stable or every restart logs everyone out.
- `deploy/radius-ui.service` is the systemd unit. It is installed to
  /etc/systemd/system and enabled, so the app returns after a reboot.
  `Requires=mariadb.service` — the UI is useless without the database.
- `smoke_db.py` exercises every db.py function against the live database,
  creating and then removing a throwaway group and user. Run it after
  changing db.py: `./venv/bin/python smoke_db.py`
- `deploy/make_favicon.py` regenerates `static/favicon.png` from the same
  geometry as `static/favicon.svg`, using only the stdlib — this host has no
  ImageMagick, rsvg or Pillow. Re-run it whenever the SVG changes.
- Templates are compiled once and cached: gunicorn does not run in debug mode,
  so editing anything under `templates/` has no effect until
  `systemctl restart radius-ui`. Static files under `static/` are served from
  disk and do need only a browser reload.
- `/healthz` is an unauthenticated liveness probe that opens a DB connection.
- Logs: `journalctl -u radius-ui -f`
