import os
import re

import pymysql
import pymysql.cursors
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database=os.environ["DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
    )


class UserExistsError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


class GroupExistsError(Exception):
    pass


class GroupNotFoundError(Exception):
    pass


class GroupInUseError(Exception):
    pass


class ValidationError(Exception):
    pass


# --- policy attributes -------------------------------------------------------
#
# Each entry describes one attribute the UI is allowed to manage. `table` is
# where the row lives ('reply' for radgroupreply/radreply, 'check' for
# radgroupcheck/radcheck) and `op` is the FreeRADIUS operator that table wants.

RATE_LIMIT_ATTR = "Mikrotik-Rate-Limit"

# `widget` picks the control the form renders and the parser used to read it
# back: "rate" is the upload/download picker, "duration" a seconds dropdown,
# "count" a small integer dropdown. Each also offers a Custom… escape hatch.
GROUP_ATTRS = [
    {
        "key": "rate_limit",
        "attribute": RATE_LIMIT_ATTR,
        "table": "reply",
        "op": "=",
        "label": "Rate limit",
        "widget": "rate",
        "help": "Applied to every member of the group.",
    },
    {
        "key": "session_timeout",
        "attribute": "Session-Timeout",
        "table": "reply",
        "op": "=",
        "label": "Session timeout",
        "widget": "duration",
        "help": "Disconnect after this long, whether idle or not.",
    },
    {
        "key": "idle_timeout",
        "attribute": "Idle-Timeout",
        "table": "reply",
        "op": "=",
        "label": "Idle timeout",
        "widget": "duration",
        "help": "Disconnect after this long with no traffic.",
    },
    {
        "key": "simultaneous_use",
        "attribute": "Simultaneous-Use",
        "table": "check",
        "op": ":=",
        "label": "Simultaneous use",
        "widget": "count",
        "help": "How many devices one account may use at once.",
    },
]

DURATION_PRESETS = [
    ("300", "5 minutes"),
    ("900", "15 minutes"),
    ("1800", "30 minutes"),
    ("3600", "1 hour"),
    ("7200", "2 hours"),
    ("14400", "4 hours"),
    ("28800", "8 hours"),
    ("43200", "12 hours"),
    ("86400", "24 hours"),
]

COUNT_PRESETS = [(str(n), str(n)) for n in range(1, 11)]

_GROUPNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# rx/tx with an optional k/M/G suffix, plus optional trailing burst parameters
_RATE_RE = re.compile(r"^\d+[kKmMgG]?(/\d+[kKmMgG]?)?( [^\s].*)?$")
_SIMPLE_RATE_RE = re.compile(r"^(\d+)([kKmMgG]?)$")

# MikroTik writes rates as rx/tx *from the router's point of view*, so rx is
# what the client uploads and tx is what it downloads. The UI says "upload"
# and "download" and does the mapping here, once.
RATE_UNITS = ["", "k", "M", "G"]
RATE_UNIT_LABELS = [("", "bit/s"), ("k", "kbit/s"), ("M", "Mbit/s"), ("G", "Gbit/s")]
RATE_PRESETS = [
    "256k", "512k", "1M", "2M", "5M", "10M",
    "20M", "50M", "100M", "200M", "500M", "1G",
]

# k/M/G are the spellings RouterOS expects; fold the other cases onto them
_UNIT_CANON = {"": "", "k": "k", "K": "k", "m": "M", "M": "M", "g": "G", "G": "G"}


def split_rate_limit(value):
    """Split a stored Mikrotik-Rate-Limit into upload and download halves.

    Returns {"up": (number, unit), "down": (number, unit)} when the value is a
    plain rx/tx pair the picker can represent, or None when it carries burst
    parameters and has to be edited as raw text.
    """
    value = (value or "").strip()
    if not value:
        return {"up": ("", "M"), "down": ("", "M")}
    if " " in value:
        return None  # burst parameters — beyond what two dropdowns can express

    parts = value.split("/")
    if len(parts) > 2:
        return None

    sides = []
    for part in parts:
        match = _SIMPLE_RATE_RE.match(part)
        if not match:
            return None
        sides.append((match.group(1), _UNIT_CANON[match.group(2)]))

    if len(sides) == 1:
        sides.append(sides[0])  # bare rx means tx follows it
    return {"up": sides[0], "down": sides[1]}


def compose_rate_limit(up_num, up_unit, down_num, down_unit):
    """Build a Mikrotik-Rate-Limit string from picker fields.

    Both sides blank means no limit. One side blank mirrors the other, which is
    what symmetric links want and saves a click.
    """
    up_num = (up_num or "").strip()
    down_num = (down_num or "").strip()
    if not up_num and not down_num:
        return ""

    if not up_num:
        up_num, up_unit = down_num, down_unit
    if not down_num:
        down_num, down_unit = up_num, up_unit

    for num in (up_num, down_num):
        if not num.isdigit() or int(num) <= 0:
            raise ValidationError("Rate limit must be a positive whole number")
    for unit in (up_unit, down_unit):
        if unit not in RATE_UNITS:
            raise ValidationError("Unknown rate unit '%s'" % unit)

    return "%s%s/%s%s" % (up_num, up_unit, down_num, down_unit)


def rate_limit_from_form(form, prefix="rate"):
    """Read the rate-limit picker out of a submitted form.

    Advanced mode hands back the raw string untouched, so a value with burst
    parameters survives a round trip through the form.
    """
    if (form.get(prefix + "_mode") or "simple") == "advanced":
        return validate_rate_limit(form.get(prefix + "_raw", ""))

    def side(name):
        choice = (form.get("%s_%s" % (prefix, name)) or "").strip()
        if choice == "custom":
            return (
                (form.get("%s_%s_num" % (prefix, name)) or "").strip(),
                (form.get("%s_%s_unit" % (prefix, name)) or "M").strip(),
            )
        if not choice:
            return ("", "M")
        match = _SIMPLE_RATE_RE.match(choice)
        if not match:
            raise ValidationError("Unrecognised speed '%s'" % choice)
        return (match.group(1), _UNIT_CANON[match.group(2)])

    up = side("up")
    down = side("down")
    return compose_rate_limit(up[0], up[1], down[0], down[1])


def validate_groupname(name):
    if not name:
        raise ValidationError("Group name is required")
    if not _GROUPNAME_RE.match(name):
        raise ValidationError(
            "Group name must start alphanumeric and use only letters, digits, "
            "dot, dash or underscore (max 64 chars)"
        )
    return name


def validate_rate_limit(value):
    """Accept a MikroTik-Rate-Limit string, or empty to mean 'no limit'."""
    value = (value or "").strip()
    if not value:
        return ""
    if not _RATE_RE.match(value):
        raise ValidationError(
            "Rate limit must look like 10M/10M (rx/tx), optionally followed by "
            "burst parameters"
        )
    return value


def _validate_seconds(value, label):
    value = (value or "").strip()
    if not value:
        return ""
    if not value.isdigit() or int(value) <= 0:
        raise ValidationError("%s must be a positive whole number" % label)
    return value


def _number_from_form(form, key, label):
    """Read a dropdown whose 'custom' option reveals a free number field."""
    choice = (form.get(key) or "").strip()
    if choice == "custom":
        choice = (form.get(key + "_num") or "").strip()
    return _validate_seconds(choice, label)


def attr_from_form(spec, form):
    """Read one managed attribute out of a submitted form."""
    if spec["widget"] == "rate":
        return rate_limit_from_form(form)
    return _number_from_form(form, spec["key"], spec["label"])


def validate_attrs(form):
    """Turn a form dict into {key: value}, validating each managed attribute.

    An empty value means 'remove this attribute'.
    """
    return {spec["key"]: attr_from_form(spec, form) for spec in GROUP_ATTRS}


# --- users -------------------------------------------------------------------


# A username is 'local' when radcheck holds a Cleartext-Password for it, and
# 'directory' when it is known to the RADIUS tables but has no local password —
# those are the AD accounts, authenticated through ntlm_auth, that exist here
# only to carry group membership and policy.
SOURCE_LOCAL = "local"
SOURCE_DIRECTORY = "directory"

# Every table a username can appear in. A directory mapping usually lives only
# in radusergroup, but a per-user rate override or a disable row can put one in
# radreply or radcheck without any password.
_ALL_USERNAMES = """
    SELECT username FROM radcheck
    UNION SELECT username FROM radusergroup
    UNION SELECT username FROM radreply
"""


def list_users(search=None, source=None):
    """Every known username with its group, rate limit, source and enabled flag.

    A user is 'disabled' when it has an Auth-Type := Reject row in radcheck,
    which is the FreeRADIUS way of locking an account without deleting it. That
    works for directory accounts too: the reject is evaluated before ntlm_auth
    is ever called.
    """
    sql = """
        SELECT u.username                     AS username,
               rug.groupname                  AS groupname,
               rr.value                       AS rate_limit,
               MAX(pw.id IS NOT NULL)         AS has_password,
               MAX(rej.id IS NOT NULL)        AS disabled
        FROM ({all_usernames}) u
        LEFT JOIN radusergroup rug ON rug.username = u.username
        LEFT JOIN radreply rr      ON rr.username = u.username
                                  AND rr.attribute = %s
        LEFT JOIN radcheck pw      ON pw.username = u.username
                                  AND pw.attribute = 'Cleartext-Password'
        LEFT JOIN radcheck rej     ON rej.username = u.username
                                  AND rej.attribute = 'Auth-Type'
                                  AND rej.value = 'Reject'
        {where}
        GROUP BY u.username, rug.groupname, rr.value
        ORDER BY u.username
    """
    params = [RATE_LIMIT_ATTR]
    where = ""
    if search:
        where = "WHERE u.username LIKE %s"
        params.append("%" + search + "%")
    sql = sql.format(all_usernames=_ALL_USERNAMES, where=where)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            out = []
            for r in rows:
                r["disabled"] = bool(r["disabled"])
                r["source"] = (
                    SOURCE_LOCAL if r.pop("has_password") else SOURCE_DIRECTORY
                )
                if source and r["source"] != source:
                    continue
                out.append(r)
            return out
    finally:
        conn.close()


def get_user(username):
    """Full detail for one user: group, rate limit, source, enabled state."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM (" + _ALL_USERNAMES + ") u "
                "WHERE u.username = %s LIMIT 1",
                (username,),
            )
            if not cur.fetchone():
                raise UserNotFoundError("User '%s' does not exist" % username)

            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s "
                "AND attribute = 'Cleartext-Password' LIMIT 1",
                (username,),
            )
            source = SOURCE_LOCAL if cur.fetchone() else SOURCE_DIRECTORY

            cur.execute(
                "SELECT groupname FROM radusergroup WHERE username = %s LIMIT 1",
                (username,),
            )
            row = cur.fetchone()
            groupname = row["groupname"] if row else None

            cur.execute(
                "SELECT value FROM radreply "
                "WHERE username = %s AND attribute = %s LIMIT 1",
                (username, RATE_LIMIT_ATTR),
            )
            row = cur.fetchone()
            rate_limit = row["value"] if row else ""

            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s "
                "AND attribute = 'Auth-Type' AND value = 'Reject' LIMIT 1",
                (username,),
            )
            disabled = cur.fetchone() is not None

            return {
                "username": username,
                "groupname": groupname,
                "rate_limit": rate_limit,
                "disabled": disabled,
                "source": source,
            }
    finally:
        conn.close()


def add_user(username, password, groupname):
    """Create a user in radcheck, optionally assigned to a group in radusergroup.

    Raises UserExistsError if the username is already present in radcheck.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s LIMIT 1", (username,)
            )
            if cur.fetchone():
                raise UserExistsError("User '%s' already exists" % username)

            cur.execute(
                "INSERT INTO radcheck (username, attribute, op, value) "
                "VALUES (%s, 'Cleartext-Password', ':=', %s)",
                (username, password),
            )
            if groupname:
                cur.execute(
                    "INSERT INTO radusergroup (username, groupname, priority) "
                    "VALUES (%s, %s, 1)",
                    (username, groupname),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_directory_user(username, groupname, rate_limit=""):
    """Map an AD account to a group, without giving it a local password.

    The account authenticates against the directory through ntlm_auth; these
    rows exist only so FreeRADIUS has policy to return for it. Writing a
    Cleartext-Password here would shadow the directory, so this never does.
    """
    username = (username or "").strip()
    if not username:
        raise ValidationError("Username is required")
    if not groupname and not rate_limit:
        raise ValidationError(
            "Give the account a group or a rate limit, otherwise the mapping "
            "carries no policy and does nothing"
        )
    rate_limit = validate_rate_limit(rate_limit)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s "
                "AND attribute = 'Cleartext-Password' LIMIT 1",
                (username,),
            )
            if cur.fetchone():
                raise UserExistsError(
                    "'%s' already exists as a local user with its own password"
                    % username
                )
            cur.execute(
                "SELECT 1 FROM radusergroup WHERE username = %s LIMIT 1", (username,)
            )
            if cur.fetchone():
                raise UserExistsError("'%s' is already mapped" % username)

            if groupname:
                cur.execute(
                    "INSERT INTO radusergroup (username, groupname, priority) "
                    "VALUES (%s, %s, 1)",
                    (username, groupname),
                )
            if rate_limit:
                cur.execute(
                    "INSERT INTO radreply (username, attribute, op, value) "
                    "VALUES (%s, %s, '=', %s)",
                    (username, RATE_LIMIT_ATTR, rate_limit),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def change_password(username, new_password):
    """Update the Cleartext-Password value for an existing user in radcheck.

    Raises UserNotFoundError if the username has no radcheck row.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s "
                "AND attribute = 'Cleartext-Password' LIMIT 1",
                (username,),
            )
            if not cur.fetchone():
                # a directory account has no local password by design; adding
                # one here would shadow AD, because rlm_mschap prefers a local
                # password over ntlm_auth
                raise ValidationError(
                    "'%s' authenticates against the directory and has no local "
                    "password to change" % username
                )

            cur.execute(
                "UPDATE radcheck SET value = %s "
                "WHERE username = %s AND attribute = 'Cleartext-Password'",
                (new_password, username),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_user(username, groupname, rate_limit):
    """Set a user's group membership and per-user rate limit, atomically.

    An empty rate_limit removes the radreply row, so the user falls back to
    whatever the group provides. An empty groupname removes group membership.
    """
    rate_limit = validate_rate_limit(rate_limit)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM (" + _ALL_USERNAMES + ") u "
                "WHERE u.username = %s LIMIT 1",
                (username,),
            )
            if not cur.fetchone():
                raise UserNotFoundError("User '%s' does not exist" % username)

            cur.execute(
                "SELECT 1 FROM radcheck WHERE username = %s "
                "AND attribute = 'Cleartext-Password' LIMIT 1",
                (username,),
            )
            is_local = cur.fetchone() is not None
            if not is_local and not groupname and not rate_limit:
                # a directory account is only these rows; clearing both would
                # delete the mapping through a form that says "save"
                raise ValidationError(
                    "'%s' authenticates against the directory, so a group or a "
                    "rate limit is all that keeps it here. Use Delete to remove "
                    "the mapping." % username
                )

            cur.execute("DELETE FROM radusergroup WHERE username = %s", (username,))
            if groupname:
                cur.execute(
                    "INSERT INTO radusergroup (username, groupname, priority) "
                    "VALUES (%s, %s, 1)",
                    (username, groupname),
                )

            cur.execute(
                "DELETE FROM radreply WHERE username = %s AND attribute = %s",
                (username, RATE_LIMIT_ATTR),
            )
            if rate_limit:
                cur.execute(
                    "INSERT INTO radreply (username, attribute, op, value) "
                    "VALUES (%s, %s, '=', %s)",
                    (username, RATE_LIMIT_ATTR, rate_limit),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_enabled(username, enabled):
    """Lock or unlock an account with an Auth-Type := Reject row in radcheck."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM (" + _ALL_USERNAMES + ") u "
                "WHERE u.username = %s LIMIT 1",
                (username,),
            )
            if not cur.fetchone():
                raise UserNotFoundError("User '%s' does not exist" % username)

            cur.execute(
                "DELETE FROM radcheck WHERE username = %s "
                "AND attribute = 'Auth-Type' AND value = 'Reject'",
                (username,),
            )
            if not enabled:
                cur.execute(
                    "INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (%s, 'Auth-Type', ':=', 'Reject')",
                    (username,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_user(username):
    """Delete a user's rows from radcheck, radreply and radusergroup, atomically.

    Raises UserNotFoundError if the username has no radcheck row.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM (" + _ALL_USERNAMES + ") u "
                "WHERE u.username = %s LIMIT 1",
                (username,),
            )
            if not cur.fetchone():
                raise UserNotFoundError("User '%s' does not exist" % username)

            cur.execute("DELETE FROM radcheck WHERE username = %s", (username,))
            cur.execute("DELETE FROM radreply WHERE username = %s", (username,))
            cur.execute("DELETE FROM radusergroup WHERE username = %s", (username,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- groups ------------------------------------------------------------------


def list_groups():
    """Distinct group names available for assignment.

    Drawn from all three group tables so a group with no policy rows yet, or
    one that only has members, still appears.
    """
    sql = """
        SELECT groupname FROM radgroupreply
        UNION SELECT groupname FROM radgroupcheck
        UNION SELECT groupname FROM radusergroup
        ORDER BY groupname
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [row["groupname"] for row in cur.fetchall()]
    finally:
        conn.close()


def list_groups_detailed():
    """Every group with its managed attributes and its member count."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT groupname FROM radgroupreply "
                "UNION SELECT groupname FROM radgroupcheck "
                "UNION SELECT groupname FROM radusergroup "
                "ORDER BY groupname"
            )
            names = [r["groupname"] for r in cur.fetchall()]

            cur.execute(
                "SELECT groupname, COUNT(*) AS members FROM radusergroup "
                "GROUP BY groupname"
            )
            members = {r["groupname"]: r["members"] for r in cur.fetchall()}

            cur.execute("SELECT groupname, attribute, value FROM radgroupreply")
            rows = cur.fetchall()
            cur.execute("SELECT groupname, attribute, value FROM radgroupcheck")
            rows += cur.fetchall()

            by_attr = {}
            for r in rows:
                by_attr.setdefault(r["groupname"], {})[r["attribute"]] = r["value"]

            out = []
            for name in names:
                attrs = by_attr.get(name, {})
                group = {"groupname": name, "members": members.get(name, 0)}
                for spec in GROUP_ATTRS:
                    group[spec["key"]] = attrs.get(spec["attribute"], "")
                out.append(group)
            return out
    finally:
        conn.close()


def get_group(groupname):
    """One group's managed attributes, plus any unmanaged ones for display."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radgroupreply WHERE groupname = %s "
                "UNION SELECT 1 FROM radgroupcheck WHERE groupname = %s "
                "UNION SELECT 1 FROM radusergroup WHERE groupname = %s LIMIT 1",
                (groupname, groupname, groupname),
            )
            if not cur.fetchone():
                raise GroupNotFoundError("Group '%s' does not exist" % groupname)

            cur.execute(
                "SELECT attribute, op, value FROM radgroupreply WHERE groupname = %s",
                (groupname,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT attribute, op, value FROM radgroupcheck WHERE groupname = %s",
                (groupname,),
            )
            rows += cur.fetchall()

            managed = {s["attribute"] for s in GROUP_ATTRS}
            by_attr = {r["attribute"]: r["value"] for r in rows}

            group = {"groupname": groupname}
            for spec in GROUP_ATTRS:
                group[spec["key"]] = by_attr.get(spec["attribute"], "")
            # attributes this UI does not manage, shown read-only so the
            # operator can see they exist and are left untouched
            group["other"] = [r for r in rows if r["attribute"] not in managed]

            cur.execute(
                "SELECT username FROM radusergroup WHERE groupname = %s "
                "ORDER BY username",
                (groupname,),
            )
            group["members"] = [r["username"] for r in cur.fetchall()]
            return group
    finally:
        conn.close()


def _write_group_attrs(cur, groupname, attrs):
    """Insert/update/delete the managed attribute rows for one group.

    An empty value deletes the row rather than storing an empty string, so the
    attribute is simply absent from the RADIUS reply.
    """
    for spec in GROUP_ATTRS:
        table = "radgroupreply" if spec["table"] == "reply" else "radgroupcheck"
        value = attrs.get(spec["key"], "")

        cur.execute(
            "DELETE FROM " + table + " WHERE groupname = %s AND attribute = %s",
            (groupname, spec["attribute"]),
        )
        if value:
            cur.execute(
                "INSERT INTO " + table + " (groupname, attribute, op, value) "
                "VALUES (%s, %s, %s, %s)",
                (groupname, spec["attribute"], spec["op"], value),
            )


def create_group(groupname, attrs):
    """Create a group by writing its policy rows. Raises if it already exists."""
    validate_groupname(groupname)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radgroupreply WHERE groupname = %s "
                "UNION SELECT 1 FROM radgroupcheck WHERE groupname = %s "
                "UNION SELECT 1 FROM radusergroup WHERE groupname = %s LIMIT 1",
                (groupname, groupname, groupname),
            )
            if cur.fetchone():
                raise GroupExistsError("Group '%s' already exists" % groupname)

            if not any(attrs.get(s["key"]) for s in GROUP_ATTRS):
                raise ValidationError(
                    "Set at least one attribute, otherwise the group has no "
                    "policy and nothing to store"
                )

            _write_group_attrs(cur, groupname, attrs)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_group(groupname, attrs):
    """Replace a group's managed attribute rows. Unmanaged rows are untouched."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM radgroupreply WHERE groupname = %s "
                "UNION SELECT 1 FROM radgroupcheck WHERE groupname = %s "
                "UNION SELECT 1 FROM radusergroup WHERE groupname = %s LIMIT 1",
                (groupname, groupname, groupname),
            )
            if not cur.fetchone():
                raise GroupNotFoundError("Group '%s' does not exist" % groupname)

            _write_group_attrs(cur, groupname, attrs)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_group(groupname):
    """Delete a group's policy rows. Refuses while the group still has members."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM radusergroup WHERE groupname = %s",
                (groupname,),
            )
            n = cur.fetchone()["n"]
            if n:
                raise GroupInUseError(
                    "Group '%s' still has %d member%s. Move them to another "
                    "group first." % (groupname, n, "" if n == 1 else "s")
                )

            cur.execute(
                "SELECT 1 FROM radgroupreply WHERE groupname = %s "
                "UNION SELECT 1 FROM radgroupcheck WHERE groupname = %s LIMIT 1",
                (groupname, groupname),
            )
            if not cur.fetchone():
                raise GroupNotFoundError("Group '%s' does not exist" % groupname)

            cur.execute("DELETE FROM radgroupreply WHERE groupname = %s", (groupname,))
            cur.execute("DELETE FROM radgroupcheck WHERE groupname = %s", (groupname,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- read-only views ---------------------------------------------------------


def dashboard_stats():
    """Counts and recent auth outcomes for the overview page."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM (" + _ALL_USERNAMES + ") u"
            )
            users = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(DISTINCT username) AS n FROM radcheck "
                "WHERE attribute = 'Cleartext-Password'"
            )
            local = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(DISTINCT username) AS n FROM radcheck "
                "WHERE attribute = 'Auth-Type' AND value = 'Reject'"
            )
            disabled = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM ("
                "  SELECT groupname FROM radgroupreply"
                "  UNION SELECT groupname FROM radgroupcheck"
                "  UNION SELECT groupname FROM radusergroup) g"
            )
            groups = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM radacct WHERE acctstoptime IS NULL")
            online = cur.fetchone()["n"]

            cur.execute(
                "SELECT reply, COUNT(*) AS n FROM radpostauth "
                "WHERE authdate >= NOW() - INTERVAL 24 HOUR GROUP BY reply"
            )
            auths = {r["reply"]: r["n"] for r in cur.fetchall()}

            return {
                "users": users,
                "local": local,
                "directory": users - local,
                "disabled": disabled,
                "enabled": users - disabled,
                "groups": groups,
                "online": online,
                "accepts_24h": auths.get("Access-Accept", 0),
                "rejects_24h": auths.get("Access-Reject", 0),
            }
    finally:
        conn.close()


def list_active_sessions():
    """Sessions in radacct with no stop time, i.e. currently online."""
    sql = """
        SELECT radacctid, username, nasipaddress, callingstationid,
               framedipaddress, acctstarttime,
               TIMESTAMPDIFF(SECOND, acctstarttime, NOW()) AS duration,
               acctinputoctets, acctoutputoctets
        FROM radacct
        WHERE acctstoptime IS NULL
        ORDER BY acctstarttime DESC
        LIMIT 100
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def user_sessions(username, limit=20):
    """Recent accounting rows for one user."""
    sql = """
        SELECT radacctid, nasipaddress, callingstationid, framedipaddress,
               acctstarttime, acctstoptime, acctsessiontime,
               acctinputoctets, acctoutputoctets, acctterminatecause
        FROM radacct
        WHERE username = %s
        ORDER BY radacctid DESC
        LIMIT %s
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (username, limit))
            return cur.fetchall()
    finally:
        conn.close()


def list_radacct():
    """Last 50 accounting rows, most recent first. Read-only table."""
    sql = """
        SELECT radacctid, username, nasipaddress, callingstationid,
               framedipaddress, acctstarttime, acctstoptime, acctsessiontime,
               acctinputoctets, acctoutputoctets, acctterminatecause
        FROM radacct
        ORDER BY radacctid DESC
        LIMIT 50
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def list_radpostauth():
    """Last 50 post-auth log rows, most recent first. Read-only table."""
    sql = """
        SELECT id, username, pass, reply, authdate
        FROM radpostauth
        ORDER BY id DESC
        LIMIT 50
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def list_nas():
    """RADIUS clients from the nas table. Read-only here."""
    sql = "SELECT id, nasname, shortname, type, ports, server, description FROM nas ORDER BY nasname"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()
