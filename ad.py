"""Read-only lookups against Active Directory, via winbind.

FreeRADIUS validates domain passwords through ntlm_auth; this module exists
only so the UI can offer a list of real accounts and reject typos before they
become a mapping row that never matches anything.

It shells out to wbinfo rather than binding to LDAP so the app needs no
directory credentials of its own — the unprivileged public winbind pipe is
enough for name lookups and enumeration. Everything degrades gracefully: if
winbind is missing, stopped, or the DC is unreachable, `available()` is False
and the UI falls back to a free-text field.
"""
import json
import os
import subprocess
import threading
import time

WBINFO = "/usr/bin/wbinfo"
TIMEOUT = 5  # seconds; a wedged winbind must not hang a page load
CACHE_TTL = 60  # the directory changes rarely and page loads are frequent

_lock = threading.Lock()
_cache = {"users": None, "at": 0.0}


def _run(args):
    """Run wbinfo, returning stdout or None on any failure."""
    try:
        proc = subprocess.run(
            [WBINFO] + args,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def available():
    """True when winbind answers, i.e. this host is joined and the DC is up."""
    return _run(["--ping-dc"]) is not None


def list_users(force=False):
    """Domain account names, lowercased as winbind reports them.

    Returns [] rather than raising when the directory cannot be reached, so a
    DC outage degrades the picker instead of breaking the page.
    """
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_TTL
        if _cache["users"] is not None and fresh and not force:
            return _cache["users"]

    out = _run(["-u"])
    users = []
    if out:
        # skip the machine and service accounts an operator never maps
        skip = {"guest", "krbtgt"}
        users = sorted(
            line.strip()
            for line in out.splitlines()
            if line.strip() and not line.strip().endswith("$")
            and line.strip().lower() not in skip
        )

    with _lock:
        _cache["users"] = users
        _cache["at"] = time.time()
    return users


def exists(username):
    """True if the name resolves to a user in the directory.

    A group name resolves too, so the SID type is checked: mapping a group
    where a user is meant would produce a row that never matches a login.
    """
    if not username:
        return False
    out = _run(["-n", username])
    return bool(out) and "SID_USER" in out


# --- privileged operations ---------------------------------------------------
#
# Everything below runs through deploy/radmgr-helper under a single narrow
# sudoers rule. The app never writes /etc/krb5.conf, never calls `net`, and
# never holds root itself. See deploy/sudoers.d/freeradius-manager.

HELPER = os.environ.get(
    "HELPER_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "radmgr-helper"),
)
JOIN_TIMEOUT = 120


class HelperError(Exception):
    """The helper refused, or could not be reached."""


def _helper(command, payload=None, timeout=30):
    """Invoke one helper verb and return its parsed JSON result.

    The payload goes over stdin so a join password never appears in argv,
    where any user on the box could read it out of `ps`.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", HELPER, command],
            input=json.dumps(payload or {}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HelperError("The operation timed out after %s seconds." % timeout)
    except OSError as exc:
        raise HelperError("Could not run the privileged helper: %s" % exc)

    try:
        result = json.loads(proc.stdout or "{}")
    except ValueError:
        # sudo itself failed, or the helper crashed before printing JSON
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        if "password is required" in detail or "a password is required" in detail:
            raise HelperError(
                "The privileged helper is not authorised. Install "
                "deploy/sudoers.d/freeradius-manager."
            )
        raise HelperError(detail or "The privileged helper returned nothing.")

    if not result.get("ok"):
        raise HelperError(result.get("error") or "The operation failed.")
    return result


def domain_status():
    """Current join state, or None when the helper is unavailable."""
    try:
        return _helper("status")["status"]
    except HelperError:
        return None


def join_domain(realm, workgroup, dc, username, password):
    """Join the directory. The password is used once and never stored."""
    return _helper(
        "join",
        {
            "realm": realm,
            "workgroup": workgroup,
            "dc": dc,
            "username": username,
            "password": password,
        },
        timeout=JOIN_TIMEOUT,
    )


def leave_domain(username, password):
    return _helper(
        "leave", {"username": username, "password": password}, timeout=JOIN_TIMEOUT
    )


def configure_radius(workgroup):
    """Re-apply the ntlm_auth wiring, e.g. after a manual config change."""
    return _helper("configure", {"workgroup": workgroup}, timeout=90)
