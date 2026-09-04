# Getting started

For anyone who has just run `install.sh` and is looking at a login page
wondering what to do next. No prior RADIUS knowledge assumed.

If you already run RADIUS and just want the reference, skip to
[manual-install.md](manual-install.md).

---

## What RADIUS actually does

RADIUS answers one question for network equipment: *should I let this person
on, and under what limits?*

Your router does not check passwords itself. When someone connects, it asks
the RADIUS server, and the server replies with yes or no — plus, optionally, a
set of instructions such as "cap them at 50 Mbit down" or "disconnect after an
hour".

```
  laptop / phone            router                    this server
  ────────────────          ──────────────            ──────────────────
  username + password  ──▶  Access-Request      ──▶   look up the user
                                                      check the password
                            Access-Accept       ◀──   attach the rate limit
                            + Mikrotik-Rate-Limit
       let them on     ◀──
```

Three terms you will meet constantly:

| Term | Means |
|---|---|
| **NAS** | the network device asking the question — your router, switch or access point. Nothing to do with file storage. |
| **Shared secret** | a password *between the router and this server*, so each knows the other is genuine. Not a user password. |
| **Access-Accept / Access-Reject** | the server's yes or no. |

---

## Step 1 — sign in

The installer printed a URL like `https://192.168.1.10:8443`. Open it and sign
in with the admin username and password you chose during install.

Your browser will warn about the certificate. That is expected: the installer
generated a self-signed one, because a real certificate needs a domain name it
cannot know. Click through it. To replace it later, put your own certificate
and key at `<app dir>/certs/server.crt` and `server.key` and restart the
service.

This login is only for this web interface. It is not a RADIUS account and has
nothing to do with the users you are about to create.

---

## Step 2 — tell the server about your router

**Nothing works until you do this.** The server ignores requests from devices
it does not recognise, and silently — you will see no error, just no reply.

Edit `/etc/freeradius/3.0/clients.conf` and add a block for your router:

```
client my-router {
    ipaddr    = 192.168.1.1        # your router's IP, as this server sees it
    secret    = a-long-random-string
    shortname = my-router
}
```

Generate the secret rather than inventing one:

```bash
openssl rand -base64 24
```

Then restart and check it took:

```bash
sudo systemctl restart freeradius
sudo systemctl status freeradius
```

The **Clients** page in the web UI lists devices held in the database's `nas`
table. Entries in `clients.conf` do not appear there — that is normal, and the
file is the more common way to configure them.

---

## Step 3 — create a group

A group holds the limits. Putting users in groups means changing a limit once
rather than per person.

**Groups → Create group**. Try:

- Name: `standard`
- Rate limit: Download `50Mbit/s`, Upload `20Mbit/s`
- Session timeout: leave Unlimited
- Simultaneous use: `2` — how many devices one account may use at once

Save. Behind the scenes that writes rows FreeRADIUS reads at login time; you
never touch SQL.

**On the rate limit:** download and upload are from *your user's* point of
view, which is the sensible way round. MikroTik stores it internally as
`rx/tx` from the router's perspective, where rx is the client's upload — an
easy thing to get backwards. The UI does that translation for you.

---

## Step 4 — create a user

**Users → Add user**. Give a username, a password, and pick the `standard`
group. Save.

The user inherits the group's limits. If one person needs something different,
open them and set a **rate limit override** — that beats the group for that
account only. Leave it Unlimited to inherit.

---

## Step 5 — prove it works before involving the router

Test from the server itself, so a failure means the server rather than the
network:

```bash
radtest yourusername theirpassword 127.0.0.1 0 testing123
```

`testing123` is the stock secret for the built-in `localhost` client. You want:

```
Received Access-Accept ...
    Mikrotik-Rate-Limit = "20M/50M"
```

`Access-Accept` means the account works. The rate limit coming back means the
group is being applied.

If you get `Access-Reject`, watch a request live:

```bash
sudo systemctl stop freeradius
sudo freeradius -X          # leave running, test from another terminal
```

Every step prints. Look for `User not found in radcheck table` (wrong username)
or a password mismatch. Ctrl-C and `sudo systemctl start freeradius` when done.

---

## Step 6 — point your router at it

### MikroTik, hotspot

```
/radius add service=hotspot address=192.168.1.10 secret=the-same-secret timeout=1s
/ip hotspot profile set [find name="your-profile"] use-radius=yes
```

Check the profile name first with `/ip hotspot profile print` — targeting the
wrong one is the most common mistake, and a bare `[find]` matches them all.

### MikroTik, PPPoE

```
/radius add service=ppp address=192.168.1.10 secret=the-same-secret timeout=1s
/ppp aaa set use-radius=yes
```

### Seeing what happened

- **Sessions** — who is online now
- **Accounting** — data used, session lengths
- **Post-Auth** — every accept and reject, the first place to look when
  someone says "it will not let me in"

Accounting only fills in if the router is configured to send it
(`radius-accounting=yes` on MikroTik).

---

## Optional — Active Directory

If you have a Windows domain, users can sign in with their existing domain
account and no password is copied here.

**Directory → Join a domain**, then **Users → Map directory account** to give a
domain user a group. Full walkthrough:
[manual-install.md §3](manual-install.md#3-samba-and-kerberos--only-for-active-directory).

One thing to know before you start, because it catches everyone:

> **CHAP cannot work against Active Directory.** Verifying CHAP needs the
> cleartext password, which a directory will never hand out. **MikroTik
> hotspot uses CHAP by default**, so domain users will be rejected until you
> switch it to PAP:
>
> ```
> /ip hotspot profile set [find name="your-profile"] login-by=cookie,http-pap
> ```
>
> Keep any other options that were already in `login-by` (such as `trial`) —
> setting it wholesale silently disables them.

After switching, **reload the portal page on the client**. The login page has
the old method compiled into it and will keep using CHAP until it is fetched
again — forget the Wi-Fi network and rejoin.

PAP sends the password to the router unencrypted unless your portal is HTTPS.
On a guest network that is a real downgrade, so consider adding a certificate
to the hotspot profile at the same time.

> **PAP also means FreeRADIUS logs the password.** The stock post-auth query
> stores whatever the client sent, and under PAP that is the real password —
> including domain passwords — readable in the Post-Auth page and the
> `radpostauth` table. To keep the audit trail without the password, blank
> that field in the SQL module's `postauth_query`:
>
> ```
> postauth_query = "INSERT INTO radpostauth (username, pass, reply, authdate) \
>     VALUES ('%{SQL-User-Name}', '', '%{reply:Packet-Type}', '%S.%M')"
> ```

---

## When something is wrong

| Symptom | Usually |
|---|---|
| Router gets no reply at all | the router is not in `clients.conf`, or the shared secret differs |
| `Access-Reject`, nothing in Post-Auth | request never arrived — same as above |
| `Access-Reject` in Post-Auth | wrong password, or the account is disabled |
| Accepted but no speed limit | user is in no group, or the router ignores the attribute |
| Domain user rejected, `CHAP` in the log | see the Active Directory note above |
| Web UI will not load | `systemctl status freeradius-manager`, then `journalctl -u freeradius-manager -n 50` |

Watching a live request explains more than any table:

```bash
sudo systemctl stop freeradius && sudo freeradius -X
```

---

## Where things live

| Path | What |
|---|---|
| `/etc/freeradius/3.0/clients.conf` | your routers and their shared secrets |
| `/etc/freeradius/3.0/mods-available/sql` | database connection |
| `<app dir>/.env` | web UI configuration (mode 600) |
| `<app dir>/certs/` | the TLS certificate |

```bash
journalctl -u freeradius-manager -f     # the web UI
journalctl -u freeradius -f             # the RADIUS server
```

---

## Next

- [manual-install.md](manual-install.md) — what the installer changed, and how
  to do any of it by hand
- [README](../README.md#security-notes) — security notes, including why RADIUS
  stores passwords the way it does
