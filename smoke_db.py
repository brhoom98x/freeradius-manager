"""Exercise every db.py function against the live radius database.

Creates a throwaway group and user, then removes them again. Depends on no
pre-existing rows, so it stays valid however the real data changes.
"""
import db

TMP_GROUP = "zz-smoke-group"
TMP_USER = "zz-smoke-user"

failures = []


def show(label, value):
    print("  %-26s %s" % (label + ":", value))


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append("%s: got %r, want %r" % (label, got, want))
    print("  %-26s %s %r" % (label + ":", "ok " if ok else "FAIL", got))


print("== read paths")
show("groups", db.list_groups())
show("users", [u["username"] for u in db.list_users()])
show("stats", db.dashboard_stats())
show("active sessions", len(db.list_active_sessions()))
show("nas rows", len(db.list_nas()))
show("radacct rows", len(db.list_radacct()))
show("postauth rows", len(db.list_radpostauth()))

print("\n== rate limit round trip")
check("split 2M/2M down", db.split_rate_limit("2M/2M")["down"], ("2", "M"))
check("split 512k/1M up", db.split_rate_limit("512k/1M")["up"], ("512", "k"))
check("lowercase m folded", db.split_rate_limit("60m/60m")["up"], ("60", "M"))
check("bare rx mirrors", db.split_rate_limit("10M")["down"], ("10", "M"))
check("burst not splittable", db.split_rate_limit("10M/10M 20M/20M 8"), None)
check("compose up/down", db.compose_rate_limit("5", "M", "20", "M"), "5M/20M")
check("blank side mirrors", db.compose_rate_limit("", "M", "20", "M"), "20M/20M")
check("both blank", db.compose_rate_limit("", "M", "", "M"), "")
# the form posts download and upload; rx/tx order must come out upload first
check(
    "form -> rx/tx",
    db.rate_limit_from_form({"rate_down": "20M", "rate_up": "5M"}),
    "5M/20M",
)
check(
    "form custom",
    db.rate_limit_from_form(
        {"rate_down": "custom", "rate_down_num": "35", "rate_down_unit": "M",
         "rate_up": "512k"}
    ),
    "512k/35M",
)
check(
    "form advanced raw",
    db.rate_limit_from_form({"rate_mode": "advanced", "rate_raw": "10M/10M 20M/20M 8"}),
    "10M/10M 20M/20M 8",
)

print("\n== validation")
for bad in [
    {"rate_down": "custom", "rate_down_num": "abc", "rate_up": ""},
    {"rate_mode": "advanced", "rate_raw": "nonsense"},
]:
    try:
        db.rate_limit_from_form(bad)
        failures.append("accepted bad rate %r" % bad)
        print("  ACCEPTED (should not): %r" % bad)
    except db.ValidationError as exc:
        print("  rejected: %s" % exc)
for bad in ["bogus!", "-lead"]:
    try:
        db.validate_groupname(bad)
        failures.append("accepted bad group name %r" % bad)
    except db.ValidationError:
        print("  rejected group name %r" % bad)

print("\n== group create/read/edit/delete")
db.create_group(
    TMP_GROUP,
    {"rate_limit": "5M/20M", "session_timeout": "1800",
     "idle_timeout": "", "simultaneous_use": "2"},
)
group = db.get_group(TMP_GROUP)
check("stored rate", group["rate_limit"], "5M/20M")
check("stored timeout", group["session_timeout"], "1800")
check("empty attr absent", group["idle_timeout"], "")
check("check-table attr", group["simultaneous_use"], "2")
db.update_group(
    TMP_GROUP,
    {"rate_limit": "", "session_timeout": "", "idle_timeout": "300",
     "simultaneous_use": ""},
)
group = db.get_group(TMP_GROUP)
check("cleared rate", group["rate_limit"], "")
check("new idle timeout", group["idle_timeout"], "300")
try:
    db.create_group(TMP_GROUP, {"rate_limit": "1M/1M"})
    failures.append("duplicate group accepted")
except db.GroupExistsError as exc:
    print("  duplicate rejected: %s" % exc)

print("\n== user create/edit/toggle/delete")
db.add_user(TMP_USER, "smokepass", TMP_GROUP)
check("group assigned", db.get_user(TMP_USER)["groupname"], TMP_GROUP)
db.update_user(TMP_USER, TMP_GROUP, "3M/9M")
check("per-user override", db.get_user(TMP_USER)["rate_limit"], "3M/9M")
db.set_user_enabled(TMP_USER, False)
check("disabled", db.get_user(TMP_USER)["disabled"], True)
db.set_user_enabled(TMP_USER, True)
check("re-enabled", db.get_user(TMP_USER)["disabled"], False)
db.change_password(TMP_USER, "smokepass2")
check("search finds it", [u["username"] for u in db.list_users(TMP_USER)], [TMP_USER])
try:
    db.delete_group(TMP_GROUP)
    failures.append("deleted a group that still had members")
except db.GroupInUseError as exc:
    print("  in-use group refused: %s" % exc)

print("")
print("== directory (AD-backed) accounts")
DIR_USER = "zz-smoke-dir"
db.add_directory_user(DIR_USER, TMP_GROUP, "")
d = db.get_user(DIR_USER)
check("source is directory", d["source"], db.SOURCE_DIRECTORY)
check("group assigned", d["groupname"], TMP_GROUP)

# the whole point: no local password, so ntlm_auth is never shadowed
conn = db.get_connection()
try:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM radcheck WHERE username = %s", (DIR_USER,)
        )
        check("no radcheck rows", cur.fetchone()["n"], 0)
finally:
    conn.close()

check(
    "listed as directory",
    [u["username"] for u in db.list_users(DIR_USER, db.SOURCE_DIRECTORY)],
    [DIR_USER],
)
check("excluded from local", db.list_users(DIR_USER, db.SOURCE_LOCAL), [])

try:
    db.change_password(DIR_USER, "x")
    failures.append("set a local password on a directory account")
except db.ValidationError as exc:
    print("  password change refused: %s" % str(exc)[:52])

try:
    db.update_user(DIR_USER, None, "")
    failures.append("update_user silently deleted a directory mapping")
except db.ValidationError as exc:
    print("  blank-out refused:       %s" % str(exc)[:52])

# a directory account can still be locked at the RADIUS layer
db.set_user_enabled(DIR_USER, False)
check("can be disabled", db.get_user(DIR_USER)["disabled"], True)
check("still directory", db.get_user(DIR_USER)["source"], db.SOURCE_DIRECTORY)
db.set_user_enabled(DIR_USER, True)

db.delete_user(DIR_USER)
check("mapping removed", DIR_USER in [u["username"] for u in db.list_users()], False)

print("\n== cleanup")
db.delete_user(TMP_USER)
db.delete_group(TMP_GROUP)
check("group gone", TMP_GROUP in db.list_groups(), False)
check("user gone", TMP_USER in [u["username"] for u in db.list_users()], False)

print()
if failures:
    print("%d FAILURE(S):" % len(failures))
    for f in failures:
        print("  - %s" % f)
    raise SystemExit(1)
print("ALL OK")
