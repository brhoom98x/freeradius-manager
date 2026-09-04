import os
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

import ad
import admap
import db

load_dotenv()

app = Flask(__name__)

# SECRET_KEY must be stable across restarts, otherwise every restart logs
# everyone out. It is generated into .env by deploy/setup.sh.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


# Environment globals, not a context processor: the field macros are pulled in
# with {% from %}, and imported macros do not see the request context, so a
# context processor would hand them empty option lists.
app.jinja_env.globals.update(
    rate_presets=db.RATE_PRESETS,
    rate_units=db.RATE_UNIT_LABELS,
    duration_presets=db.DURATION_PRESETS,
    count_presets=db.COUNT_PRESETS,
)


@app.context_processor
def inject_globals():
    return {"current_admin": session.get("admin")}


@app.template_filter("split_rate")
def split_rate(value):
    """None when the value carries burst parameters and needs the raw field."""
    return db.split_rate_limit(value)


# --- template filters --------------------------------------------------------


@app.template_filter("bytes")
def format_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024


@app.template_filter("duration")
def format_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm %ds" % (m, s)
    return "%ds" % s


# --- auth --------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ADMIN_USER or not ADMIN_PASSWORD_HASH:
        return render_template(
            "login.html",
            error="No admin credentials configured. Run deploy/setup.sh to set "
            "ADMIN_USER and ADMIN_PASSWORD_HASH in .env.",
        )

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USER and check_password_hash(
            ADMIN_PASSWORD_HASH, password
        ):
            session.clear()
            session["admin"] = username
            session.permanent = False
            dest = request.args.get("next", "")
            # only allow same-site relative redirects
            if not dest.startswith("/") or dest.startswith("//"):
                dest = url_for("index")
            return redirect(dest)
        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


# --- dashboard ---------------------------------------------------------------


@app.route("/")
@login_required
def index():
    try:
        return render_template(
            "dashboard.html",
            stats=db.dashboard_stats(),
            sessions=db.list_active_sessions()[:10],
            error=None,
        )
    except Exception as exc:
        return render_template(
            "dashboard.html", stats=None, sessions=[], error=str(exc)
        )


# --- users -------------------------------------------------------------------


@app.route("/users")
@login_required
def users():
    search = request.args.get("q", "").strip()
    source = request.args.get("source", "").strip() or None
    try:
        return render_template(
            "users.html",
            users=db.list_users(search or None, source),
            q=search,
            source=source,
            error=None,
        )
    except Exception as exc:
        return render_template(
            "users.html", users=[], q=search, source=source, error=str(exc)
        )


@app.route("/users/add-directory", methods=["GET", "POST"])
@login_required
def add_directory_user():
    """Map an existing AD account to a group so FreeRADIUS has policy for it."""
    try:
        groups = db.list_groups()
    except Exception as exc:
        return render_template(
            "add_directory_user.html", groups=[], ad_users=[], ad_up=False,
            error=str(exc)
        )

    # the picker is a convenience; a DC outage must not block the form
    ad_users = ad.list_users()
    ad_up = bool(ad_users)
    # accounts already mapped or holding a local password are not offered twice
    try:
        taken = {u["username"].lower() for u in db.list_users()}
        ad_users = [u for u in ad_users if u.lower() not in taken]
    except Exception:
        pass

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        groupname = request.form.get("groupname", "").strip()

        def back(error):
            return render_template(
                "add_directory_user.html", groups=groups, ad_users=ad_users,
                ad_up=ad_up, username=username, error=error
            )

        try:
            rate_limit = db.rate_limit_from_form(request.form)
        except Exception as exc:
            return back(str(exc))

        if not username:
            return back("Pick or type a directory account.")
        # only reject when the directory is actually answering; if winbind is
        # down, trust the operator rather than blocking the work
        if ad_up and not ad.exists(username):
            return back(
                "'%s' is not a user account in the directory. Check the "
                "spelling, or clear the picker and type it if the account is "
                "new." % username
            )

        try:
            db.add_directory_user(username, groupname or None, rate_limit)
            flash("Directory account '%s' mapped." % username, "success")
            return redirect(url_for("users"))
        except Exception as exc:
            return back(str(exc))

    return render_template(
        "add_directory_user.html", groups=groups, ad_users=ad_users, ad_up=ad_up,
        error=None
    )


@app.route("/users/add", methods=["GET", "POST"])
@login_required
def add_user():
    try:
        groups = db.list_groups()
    except Exception as exc:
        return render_template("add_user.html", groups=[], error=str(exc))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        groupname = request.form.get("groupname", "").strip()

        if not username or not password:
            return render_template(
                "add_user.html",
                groups=groups,
                error="Username and password are required.",
                username=username,
            )

        try:
            db.add_user(username, password, groupname or None)
            flash("User '%s' added." % username, "success")
            return redirect(url_for("users"))
        except Exception as exc:
            return render_template(
                "add_user.html", groups=groups, error=str(exc), username=username
            )

    return render_template("add_user.html", groups=groups, error=None)


@app.route("/users/<username>", methods=["GET", "POST"])
@login_required
def edit_user(username):
    try:
        groups = db.list_groups()
        user = db.get_user(username)
    except Exception as exc:
        return render_template(
            "edit_user.html", user={"username": username}, groups=[],
            sessions=[], error=str(exc)
        )

    if request.method == "POST":
        groupname = request.form.get("groupname", "").strip()
        try:
            rate_limit = db.rate_limit_from_form(request.form)
            db.update_user(username, groupname or None, rate_limit)
            flash("Updated '%s'." % username, "success")
            return redirect(url_for("users"))
        except Exception as exc:
            # redisplay what they typed rather than what is stored
            user["groupname"] = groupname
            user["rate_limit"] = request.form.get("rate_raw", user["rate_limit"])
            return render_template(
                "edit_user.html", user=user, groups=groups, sessions=[], error=str(exc)
            )

    try:
        sessions = db.user_sessions(username)
    except Exception:
        sessions = []
    return render_template(
        "edit_user.html", user=user, groups=groups, sessions=sessions, error=None
    )


@app.route("/users/<username>/toggle", methods=["POST"])
@login_required
def toggle_user(username):
    enable = request.form.get("enable") == "1"
    try:
        db.set_user_enabled(username, enable)
        flash(
            "User '%s' %s." % (username, "enabled" if enable else "disabled"),
            "success",
        )
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("back") or url_for("users"))


@app.route("/users/<username>/password", methods=["GET", "POST"])
@login_required
def change_password(username):
    if request.method == "POST":
        new_password = request.form.get("password", "")

        if not new_password:
            return render_template(
                "change_password.html", username=username, error="Password is required."
            )

        try:
            db.change_password(username, new_password)
            flash("Password updated for '%s'." % username, "success")
            return redirect(url_for("users"))
        except Exception as exc:
            return render_template(
                "change_password.html", username=username, error=str(exc)
            )

    return render_template("change_password.html", username=username, error=None)


@app.route("/users/<username>/delete", methods=["GET", "POST"])
@login_required
def delete_user(username):
    try:
        source = db.get_user(username)["source"]
    except Exception:
        source = db.SOURCE_LOCAL

    if request.method == "POST":
        try:
            db.delete_user(username)
            flash(
                "Mapping for '%s' removed." % username
                if source == db.SOURCE_DIRECTORY
                else "User '%s' deleted." % username,
                "success",
            )
            return redirect(url_for("users"))
        except Exception as exc:
            return render_template(
                "delete_user.html", username=username, source=source, error=str(exc)
            )

    return render_template(
        "delete_user.html", username=username, source=source, error=None
    )


# --- groups ------------------------------------------------------------------


def group_from_form(form, groupname):
    """Rebuild a group dict from a submitted form, for redisplay after an error.

    Each field is read independently so one bad value does not blank the rest;
    the field that failed falls back to whatever the operator typed.
    """
    group = {"groupname": groupname}
    for spec in db.GROUP_ATTRS:
        try:
            group[spec["key"]] = db.attr_from_form(spec, form)
        except Exception:
            group[spec["key"]] = form.get(
                "rate_raw" if spec["widget"] == "rate" else spec["key"] + "_num", ""
            )
    return group


@app.route("/groups")
@login_required
def groups():
    try:
        return render_template(
            "groups.html", groups=db.list_groups_detailed(),
            specs=db.GROUP_ATTRS, error=None
        )
    except Exception as exc:
        return render_template(
            "groups.html", groups=[], specs=db.GROUP_ATTRS, error=str(exc)
        )


@app.route("/groups/add", methods=["GET", "POST"])
@login_required
def add_group():
    if request.method == "POST":
        groupname = request.form.get("groupname", "").strip()
        try:
            attrs = db.validate_attrs(request.form)
            db.create_group(groupname, attrs)
            flash("Group '%s' created." % groupname, "success")
            return redirect(url_for("groups"))
        except Exception as exc:
            return render_template(
                "group_form.html",
                mode="add",
                group=group_from_form(request.form, groupname),
                specs=db.GROUP_ATTRS,
                error=str(exc),
            )

    return render_template(
        "group_form.html", mode="add", group={}, specs=db.GROUP_ATTRS, error=None
    )


@app.route("/groups/<groupname>", methods=["GET", "POST"])
@login_required
def edit_group(groupname):
    if request.method == "POST":
        try:
            attrs = db.validate_attrs(request.form)
            db.update_group(groupname, attrs)
            flash("Group '%s' updated." % groupname, "success")
            return redirect(url_for("groups"))
        except Exception as exc:
            return render_template(
                "group_form.html",
                mode="edit",
                group=group_from_form(request.form, groupname),
                specs=db.GROUP_ATTRS,
                error=str(exc),
            )

    try:
        group = db.get_group(groupname)
    except Exception as exc:
        return render_template(
            "group_form.html",
            mode="edit",
            group={"groupname": groupname},
            specs=db.GROUP_ATTRS,
            error=str(exc),
        )
    return render_template(
        "group_form.html", mode="edit", group=group, specs=db.GROUP_ATTRS, error=None
    )


@app.route("/groups/<groupname>/delete", methods=["GET", "POST"])
@login_required
def delete_group(groupname):
    if request.method == "POST":
        try:
            db.delete_group(groupname)
            flash("Group '%s' deleted." % groupname, "success")
            return redirect(url_for("groups"))
        except Exception as exc:
            return render_template(
                "delete_group.html", groupname=groupname, error=str(exc)
            )

    return render_template("delete_group.html", groupname=groupname, error=None)


# --- directory (Active Directory) --------------------------------------------


@app.route("/directory")
@login_required
def directory():
    status = ad.domain_status()
    return render_template(
        "directory.html",
        status=status,
        secure=request.is_secure,
        error=None if status else
        "The privileged helper is not reachable, so the join state cannot be "
        "read. Check that deploy/sudoers.d/freeradius-manager is installed.",
    )


@app.route("/directory/join", methods=["POST"])
@login_required
def directory_join():
    realm = request.form.get("realm", "").strip()
    workgroup = request.form.get("workgroup", "").strip()
    dc = request.form.get("dc", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    try:
        result = ad.join_domain(realm, workgroup, dc, username, password)
        # the account list is now a different domain's
        ad.list_users(force=True)
        flash(
            "%s. FreeRADIUS now validates MS-CHAP for accounts without a local "
            "password against the directory." % result.get("message", "Joined"),
            "success",
        )
    except Exception as exc:
        flash(str(exc), "error")
    finally:
        # the value is gone either way; nothing above stores or logs it
        del password

    return redirect(url_for("directory"))


@app.route("/directory/leave", methods=["POST"])
@login_required
def directory_leave():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    try:
        ad.leave_domain(username, password)
        ad.list_users(force=True)
        flash(
            "Left the domain. Directory accounts already mapped keep their "
            "group rows but can no longer authenticate.",
            "success",
        )
    except Exception as exc:
        flash(str(exc), "error")
    finally:
        del password
    return redirect(url_for("directory"))


@app.route("/directory/reapply", methods=["POST"])
@login_required
def directory_reapply():
    workgroup = request.form.get("workgroup", "").strip()
    try:
        ad.configure_radius(workgroup)
        flash("FreeRADIUS re-wired to ntlm_auth for %s." % workgroup, "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("directory"))


@app.route("/directory/groups", methods=["GET"])
@login_required
def directory_groups():
    """Map AD groups to RADIUS groups. First match wins, so order matters."""
    try:
        radius_groups = db.list_groups()
    except Exception as exc:
        return render_template(
            "directory_groups.html", mappings=[], ad_groups=[], ad_up=False,
            radius_groups=[], error=str(exc)
        )

    ad_groups = ad.list_groups()
    mappings = admap.load()
    mapped = {m["ad_group"].lower() for m in mappings}

    # flag mappings whose RADIUS group has since been deleted -- they would
    # silently resolve to nothing at authentication time
    for m in mappings:
        m["missing"] = m["radius_group"] not in radius_groups

    return render_template(
        "directory_groups.html",
        mappings=mappings,
        ad_groups=[g for g in ad_groups if g.lower() not in mapped],
        ad_up=bool(ad_groups),
        radius_groups=radius_groups,
        error=None,
    )


@app.route("/directory/groups/add", methods=["POST"])
@login_required
def directory_groups_add():
    try:
        admap.add(
            request.form.get("ad_group", ""),
            request.form.get("radius_group", ""),
        )
        flash("Mapped '%s'." % request.form.get("ad_group", "").strip(), "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("directory_groups"))


@app.route("/directory/groups/remove", methods=["POST"])
@login_required
def directory_groups_remove():
    name = request.form.get("ad_group", "").strip()
    try:
        admap.remove(name)
        flash("Removed the mapping for '%s'." % name, "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("directory_groups"))


@app.route("/directory/groups/move", methods=["POST"])
@login_required
def directory_groups_move():
    try:
        admap.move(
            request.form.get("ad_group", ""),
            request.form.get("direction", ""),
        )
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("directory_groups"))


@app.route("/directory/groups/test", methods=["POST"])
@login_required
def directory_groups_test():
    """Show what a named account would actually resolve to, before trusting it."""
    username = request.form.get("username", "").strip()
    groups = ad.user_groups(username) if username else []
    if not username:
        flash("Enter a username to test.", "error")
    elif not groups:
        flash(
            "The directory returned no groups for '%s'. Check the spelling, or "
            "that the account exists." % username, "error",
        )
    else:
        resolved = admap.resolve(groups)
        if resolved:
            flash(
                "'%s' is in: %s — first mapped group wins, so it gets the "
                "'%s' policy." % (username, ", ".join(groups), resolved),
                "success",
            )
        else:
            flash(
                "'%s' is in: %s — none of those are mapped, so it would get no "
                "policy from the directory." % (username, ", ".join(groups)),
                "error",
            )
    return redirect(url_for("directory_groups"))


# --- read-only views ---------------------------------------------------------


@app.route("/sessions")
@login_required
def sessions_view():
    try:
        return render_template(
            "sessions.html", rows=db.list_active_sessions(), error=None
        )
    except Exception as exc:
        return render_template("sessions.html", rows=[], error=str(exc))


@app.route("/clients")
@login_required
def clients():
    try:
        return render_template("clients.html", rows=db.list_nas(), error=None)
    except Exception as exc:
        return render_template("clients.html", rows=[], error=str(exc))


@app.route("/radacct")
@login_required
def radacct():
    try:
        rows = db.list_radacct()
        error = None
    except Exception as exc:
        rows = []
        error = str(exc)
    return render_template("radacct.html", rows=rows, error=error)


@app.route("/radpostauth")
@login_required
def radpostauth():
    try:
        rows = db.list_radpostauth()
        error = None
    except Exception as exc:
        rows = []
        error = str(exc)
    return render_template("radpostauth.html", rows=rows, error=error)


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for systemd/monitoring."""
    try:
        db.get_connection().close()
        return {"status": "ok"}, 200
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}, 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
