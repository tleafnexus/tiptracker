"""Tip Tracker - a small self-hosted app for logging shifts, tips and paystubs.

Multi-user: every employer, shift, paystub and attachment belongs to one user
(`user_id`). All reads and writes of those rows go through the helpers below,
which always filter on the logged-in user. Locations and minimum-wage rates are
shared and only admins can change them.
"""
import csv
import hmac
import io
import os
import secrets
import sqlite3
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for, Response)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import calc
from .db import (DATA_DIR, DB_PATH, UPLOAD_DIR, claim_unowned, connect, get_setting, init_db,
                 set_setting, unowned_count)

TZ = ZoneInfo(os.environ.get("TZ", "America/Denver"))
# Before any account exists, APP_PASSWORD (if set) is required to create the first one.
SETUP_KEY = os.environ.get("APP_PASSWORD", "")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "pdf"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MIN_PASSWORD = 8

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "") == "1",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90)
init_db()


# ---------------------------------------------------------------- plumbing
def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def today():
    return datetime.now(TZ).date()


def user_count():
    return db().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def current_user():
    """The logged-in user, or None. Signing out everywhere rotates session_version."""
    if "user" not in g:
        g.user = None
        uid = session.get("uid")
        if uid:
            u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if u and u["active"] and hmac.compare_digest(session.get("sv", ""), u["session_version"]):
                g.user = u
    return g.user


def uid():
    return current_user()["id"]


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if user_count() == 0:
            return redirect(url_for("setup"))
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.full_path))
        if u["must_change_password"] and request.endpoint not in ("account", "logout"):
            flash("Please choose a new password to finish setting up your account.", "warn")
            return redirect(url_for("account"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not current_user()["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


@app.before_request
def _csrf():
    """Every form post carries a per-session token."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("_csrf", ""), session["csrf"]):
            abort(400, "Form expired - go back, refresh the page and try again.")


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.context_processor
def _ctx():
    return dict(csrf=session.get("csrf", ""), me=current_user(),
                money=money, hrs=hrs, nav=request.endpoint)


@app.template_filter("nicedate")
def nicedate(s):
    if not s:
        return ""
    dt = s if isinstance(s, date) else date.fromisoformat(s)
    return dt.strftime("%a %b %-d, %Y")


@app.template_filter("shortdate")
def shortdate(s):
    if not s:
        return ""
    dt = s if isinstance(s, date) else date.fromisoformat(s)
    return dt.strftime("%b %-d")


def money(x, signed=False):
    if x is None:
        return "—"
    s = f"${abs(x):,.2f}"
    if x < 0:
        return "−" + s
    return ("+" + s) if signed and x > 0 else s


def hrs(x):
    if x is None:
        return "—"
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def fnum(name, default=0.0):
    v = (request.form.get(name) or "").strip().replace("$", "").replace(",", "")
    if v == "":
        return default
    try:
        return float(v)
    except ValueError:
        abort(400, f"'{name}' must be a number.")


def fdate(name, required=True):
    v = (request.form.get(name) or "").strip()
    if not v and not required:
        return ""
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        abort(400, f"'{name}' must be a date.")


def safe_next(default):
    nxt = request.values.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else default


# ---------------------------------------------------------------- ownership helpers
_OWNED = {"employers", "shifts", "paystubs", "attachments"}


def owned(table, row_id, missing=404):
    """A row from one of the per-user tables, only if it belongs to the logged-in user."""
    assert table in _OWNED
    row = db().execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (row_id, uid())).fetchone() \
        if row_id else None
    if row is None:
        abort(missing)
    return row


def _employer(eid):
    if not eid:
        return None
    return db().execute("SELECT * FROM employers WHERE id=? AND user_id=?", (eid, uid())).fetchone()


PAY_RANGES = [("payperiod", "This pay period"), ("lastpayperiod", "Last pay period")]


def range_args():
    erow = _employer(request.args.get("employer", type=int))
    emp = erow["id"] if erow else None
    key = request.args.get("range", "month")
    if key in dict(PAY_RANGES):
        if erow and calc.has_schedule(erow):
            s, e = calc.period_containing(erow, today())
            if key == "lastpayperiod":
                s, e = calc.period_containing(erow, s - timedelta(days=1))
            return key, s, e, emp
        key = "month"
    key, start, end = calc.resolve_range(key, request.args.get("start"), request.args.get("end"), today())
    return key, start, end, emp


def ranges_for(emp):
    """Date-range choices; pay-period choices appear once an employer with a schedule is picked."""
    erow = _employer(emp)
    if erow and calc.has_schedule(erow):
        return dict(PAY_RANGES) | calc.RANGES
    return calc.RANGES


# ---------------------------------------------------------------- attachments
def save_uploads(kind, parent_id, files):
    conn = db()
    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            flash(f"Skipped {f.filename}: only photos and PDFs can be attached.", "warn")
            continue
        name = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_DIR, name)
        f.save(path)
        _shrink_image(path, ext)
        conn.execute("INSERT INTO attachments(user_id, kind, parent_id, filename, original_name) VALUES (?,?,?,?,?)",
                     (uid(), kind, parent_id, name, secure_filename(f.filename)))


def _shrink_image(path, ext):
    """Rotate phone photos upright and cap them at 2000px to save space."""
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        return
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if max(im.size) > 2000:
                im.thumbnail((2000, 2000))
            if ext in {"jpg", "jpeg"}:
                im.convert("RGB").save(path, quality=85)
            else:
                im.save(path)
    except Exception:  # keep the original if Pillow can't handle it
        pass


def attachments(kind, parent_id):
    return db().execute("SELECT * FROM attachments WHERE user_id=? AND kind=? AND parent_id=? ORDER BY id",
                        (uid(), kind, parent_id)).fetchall()


def attachment_counts(kind):
    return {r[0]: r[1] for r in db().execute(
        "SELECT parent_id, COUNT(*) FROM attachments WHERE user_id=? AND kind=? GROUP BY parent_id", (uid(), kind))}


def _remove_files(rows):
    for a in rows:
        try:
            os.remove(os.path.join(UPLOAD_DIR, a["filename"]))
        except FileNotFoundError:
            pass


def delete_attachments(kind, parent_id):
    _remove_files(attachments(kind, parent_id))
    db().execute("DELETE FROM attachments WHERE user_id=? AND kind=? AND parent_id=?", (uid(), kind, parent_id))


@app.route("/uploads/<name>")
@login_required
def upload(name):
    a = db().execute("SELECT filename FROM attachments WHERE filename=? AND user_id=?",
                     (secure_filename(name), uid())).fetchone()
    if not a:
        abort(404)
    resp = send_from_directory(UPLOAD_DIR, a["filename"])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.post("/attachments/<int:aid>/delete")
@login_required
def delete_attachment(aid):
    a = owned("attachments", aid)
    _remove_files([a])
    db().execute("DELETE FROM attachments WHERE id=? AND user_id=?", (aid, uid()))
    db().commit()
    return redirect(safe_next(url_for("dashboard")))


# ---------------------------------------------------------------- auth
_failures = defaultdict(list)   # key -> timestamps of recent failed logins (per worker process)
LOCK_WINDOW = 15 * 60
# 10 wrong guesses lock one username; an IP address gets more room because everyone
# behind the same router or reverse proxy shares it.
LOCK_AFTER = {"user": 10, "ip": 40}


def _locked(*keys):
    now = time.time()
    for k in keys:
        _failures[k] = [t for t in _failures[k] if now - t < LOCK_WINDOW]
    return any(len(_failures[k]) >= LOCK_AFTER[k.split(":", 1)[0]] for k in keys)


TRUST_CLOUDFLARE = os.environ.get("TRUST_CLOUDFLARE", "") == "1"


def client_ip():
    """The visitor's address. Behind a Cloudflare Tunnel every request arrives from the
    tunnel, so use the address Cloudflare reports instead (only when told to trust it)."""
    if TRUST_CLOUDFLARE:
        cf = (request.headers.get("CF-Connecting-IP") or "").strip()
        if cf:
            return cf[:64]
    return request.remote_addr or "?"


def _new_session_version():
    return secrets.token_hex(16)


def _start_session(u):
    csrf = session.get("csrf")
    session.clear()
    session.permanent = True
    session.update(uid=u["id"], sv=u["session_version"], csrf=csrf or secrets.token_hex(16))
    db().execute("UPDATE users SET last_login=? WHERE id=?", (datetime.now(TZ).isoformat(timespec="minutes"), u["id"]))
    db().commit()


def _password_problem(pw, confirm):
    if len(pw) < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters."
    if pw != confirm:
        return "The two passwords don't match."
    return None


def _clean_username(raw):
    name = (raw or "").strip().lower()
    if not (2 <= len(name) <= 32) or not all(c.isalnum() or c in "._-" for c in name):
        abort(400, "Usernames are 2-32 characters: letters, numbers, dot, dash or underscore.")
    return name


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the very first account. Disappears once any account exists."""
    conn = db()
    if user_count() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        if SETUP_KEY and not hmac.compare_digest(request.form.get("setup_key", ""), SETUP_KEY):
            flash("That setup key doesn't match APP_PASSWORD in the .env file.", "error")
            return redirect(url_for("setup"))
        username = _clean_username(request.form.get("username"))
        pw = request.form.get("password", "")
        problem = _password_problem(pw, request.form.get("confirm", ""))
        if problem:
            flash(problem, "error")
            return redirect(url_for("setup"))
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, is_admin, session_version) VALUES (?,?,?,1,?)",
            (username, (request.form.get("display_name") or "").strip() or username,
             generate_password_hash(pw), _new_session_version()))
        claim_unowned(conn, cur.lastrowid)
        conn.commit()
        _start_session(conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
        flash("Your account is ready. You're the admin, so you can add other people from the Admin page.", "ok")
        return redirect(url_for("dashboard"))
    return render_template("setup.html", needs_key=bool(SETUP_KEY), legacy=unowned_count(conn))


@app.route("/login", methods=["GET", "POST"])
def login():
    if user_count() == 0:
        return redirect(url_for("setup"))
    if current_user():
        return redirect(safe_next(url_for("dashboard")))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        keys = ("ip:" + client_ip(), "user:" + username)
        if _locked(*keys):
            flash("Too many failed tries. Wait 15 minutes and try again.", "error")
            return render_template("login.html", username=username), 429
        u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # Hash something even for unknown users so response time doesn't reveal which names exist.
        ok = check_password_hash(u["password_hash"] if u else _DUMMY_HASH, request.form.get("password", ""))
        if u and ok and u["active"]:
            _failures.pop(keys[1], None)
            _start_session(u)
            return redirect(safe_next(url_for("dashboard")))
        for k in keys:
            _failures[k].append(time.time())
        flash("That username and password don't match." if not (u and ok) else
              "This account has been turned off. Ask the admin.", "error")
        return render_template("login.html", username=username), 401
    return render_template("login.html", username="")


_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    conn = db()
    me = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            conn.execute("UPDATE users SET display_name=? WHERE id=?",
                         ((request.form.get("display_name") or "").strip() or me["username"], me["id"]))
            flash("Saved.", "ok")
        elif action == "password":
            if not check_password_hash(me["password_hash"], request.form.get("current", "")):
                flash("Your current password isn't right.", "error")
                return redirect(url_for("account"))
            pw = request.form.get("password", "")
            problem = _password_problem(pw, request.form.get("confirm", ""))
            if problem:
                flash(problem, "error")
                return redirect(url_for("account"))
            sv = _new_session_version()
            conn.execute("UPDATE users SET password_hash=?, must_change_password=0, session_version=? WHERE id=?",
                         (generate_password_hash(pw), sv, me["id"]))
            session["sv"] = sv   # stay signed in here; every other device is signed out
            flash("Password changed. Other devices you were signed in on have been signed out.", "ok")
        elif action == "signout_all":
            sv = _new_session_version()
            conn.execute("UPDATE users SET session_version=? WHERE id=?", (sv, me["id"]))
            session["sv"] = sv
            flash("Signed out on every other device.", "ok")
        conn.commit()
        return redirect(url_for("account") if action != "password" else url_for("dashboard"))
    return render_template("account.html")


# ---------------------------------------------------------------- admin: people
def _admin_count(exclude=None):
    return db().execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1 AND id IS NOT ?",
                        (exclude,)).fetchone()[0]


def _temp_password():
    words = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    return words[:12]


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    conn = db()
    me = current_user()
    if request.method == "POST":
        action = request.form.get("action")
        target = None
        if action != "create":
            target = conn.execute("SELECT * FROM users WHERE id=?", (request.form.get("user_id", type=int),)).fetchone()
            if not target:
                abort(404)
        if action == "create":
            username = _clean_username(request.form.get("username"))
            pw = request.form.get("password") or _temp_password()
            if len(pw) < MIN_PASSWORD:
                abort(400, f"Temporary passwords need at least {MIN_PASSWORD} characters.")
            try:
                conn.execute("INSERT INTO users(username, display_name, password_hash, is_admin, must_change_password, "
                             "session_version) VALUES (?,?,?,?,1,?)",
                             (username, (request.form.get("display_name") or "").strip() or username,
                              generate_password_hash(pw), 1 if request.form.get("is_admin") else 0,
                              _new_session_version()))
            except sqlite3.IntegrityError:
                flash(f"There's already an account called {username}.", "error")
                return redirect(url_for("admin_users"))
            flash(f"Created {username}. Their temporary password is {pw} and they'll pick a new one when they log in. "
                  "This is the only time it's shown.", "ok")
        elif action == "rename":
            username = _clean_username(request.form.get("username"))
            display = (request.form.get("display_name") or "").strip() or username
            clash = conn.execute("SELECT 1 FROM users WHERE username=? AND id<>?", (username, target["id"])).fetchone()
            if clash:
                flash(f"There's already an account called {username}.", "error")
            else:
                conn.execute("UPDATE users SET username=?, display_name=? WHERE id=?", (username, display, target["id"]))
                if username != target["username"]:
                    flash(f"Renamed {target['username']} to {username}. They log in with the new username from now on.", "ok")
                else:
                    flash(f"Saved {username}.", "ok")
        elif action == "reset":
            pw = _temp_password()
            conn.execute("UPDATE users SET password_hash=?, must_change_password=1, session_version=? WHERE id=?",
                         (generate_password_hash(pw), _new_session_version(), target["id"]))
            flash(f"New temporary password for {target['username']}: {pw} (shown once). "
                  "They've been signed out everywhere.", "ok")
            if target["id"] == me["id"]:
                session.clear()
                conn.commit()
                return redirect(url_for("login"))
        elif action == "toggle_admin":
            if target["is_admin"] and _admin_count(exclude=target["id"]) == 0:
                flash("There has to be at least one admin.", "warn")
            else:
                conn.execute("UPDATE users SET is_admin=? WHERE id=?", (0 if target["is_admin"] else 1, target["id"]))
                flash(f"{target['username']} is {'no longer' if target['is_admin'] else 'now'} an admin.", "ok")
        elif action == "toggle_active":
            if target["id"] == me["id"]:
                flash("You can't turn off your own account.", "warn")
            elif target["active"] and target["is_admin"] and _admin_count(exclude=target["id"]) == 0:
                flash("There has to be at least one active admin.", "warn")
            else:
                conn.execute("UPDATE users SET active=?, session_version=? WHERE id=?",
                             (0 if target["active"] else 1, _new_session_version(), target["id"]))
                flash(f"{target['username']} is turned {'off' if target['active'] else 'back on'}.", "ok")
        elif action == "delete":
            if target["id"] == me["id"]:
                flash("You can't delete your own account.", "warn")
            elif (request.form.get("confirm") or "").strip().lower() != target["username"]:
                flash(f"Type {target['username']} exactly to confirm deleting it.", "error")
            elif target["is_admin"] and _admin_count(exclude=target["id"]) == 0:
                flash("There has to be at least one admin.", "warn")
            else:
                _remove_files(conn.execute("SELECT filename FROM attachments WHERE user_id=?", (target["id"],)).fetchall())
                for t in ("attachments", "shifts", "paystubs", "employers", "user_settings"):
                    conn.execute(f"DELETE FROM {t} WHERE user_id=?", (target["id"],))
                conn.execute("DELETE FROM users WHERE id=?", (target["id"],))
                flash(f"Deleted {target['username']} and all of their data.", "ok")
        conn.commit()
        return redirect(url_for("admin_users"))
    users = conn.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM shifts s WHERE s.user_id=u.id) AS shift_count, "
        "(SELECT MAX(date) FROM shifts s WHERE s.user_id=u.id) AS last_shift "
        "FROM users u ORDER BY u.active DESC, lower(u.username)").fetchall()
    return render_template("admin_users.html", users=users, suggested=_temp_password(),
                           legacy=unowned_count(conn))


# ---------------------------------------------------------------- dashboard
@app.route("/")
@login_required
def dashboard():
    conn = db()
    emps = calc.employers(conn, uid())
    if not emps:
        return redirect(url_for("employers_page", welcome=1))
    key, start, end, emp = range_args()
    shifts = calc.shifts_between(conn, uid(), start, end, emp)
    checks = calc.wage_check(conn, uid(), start, end, emp)
    problems = [c for c in checks if c["status"] == "short"]
    return render_template(
        "dashboard.html", ranges=ranges_for(emp), range_key=key, start=start, end=end,
        employer_id=emp, employers=emps, t=calc.totals(shifts),
        charts=calc.chart_data(conn, uid(), shifts, emps, start, end),
        has_data=bool(shifts), problems=problems,
        owed=sum(c["shortfall"] or 0 for c in problems),
        recent=shifts[:5])


# ---------------------------------------------------------------- shifts
@app.route("/shifts")
@login_required
def shifts_page():
    conn = db()
    key, start, end, emp = range_args()
    rows = calc.shifts_between(conn, uid(), start, end, emp)
    return render_template("shifts.html", shifts=rows, t=calc.totals(rows), ranges=ranges_for(emp),
                           range_key=key, start=start, end=end, employer_id=emp,
                           employers=calc.employers(conn, uid()), photos=attachment_counts("shift"))


@app.route("/shifts/new", methods=["GET", "POST"])
@app.route("/shifts/<int:sid>", methods=["GET", "POST"])
@login_required
def shift_form(sid=None):
    conn = db()
    emps = calc.employers(conn, uid())
    if not emps:
        flash("Add an employer first.", "warn")
        return redirect(url_for("employers_page"))
    shift = owned("shifts", sid) if sid else None
    if request.method == "POST":
        emp = owned("employers", request.form.get("employer_id", type=int), missing=400)
        vals = dict(
            employer_id=emp["id"], date=fdate("date"),
            start_time=request.form.get("start_time", "").strip()[:5],
            end_time=request.form.get("end_time", "").strip()[:5],
            hours=fnum("hours"), hourly_rate=fnum("hourly_rate", emp["hourly_rate"]),
            cash_tips=fnum("cash_tips"), card_tips=fnum("card_tips"),
            notes=request.form.get("notes", "").strip())
        if vals["hours"] < 0 or vals["hours"] > 24:
            abort(400, "Hours must be between 0 and 24.")
        if sid:
            conn.execute("UPDATE shifts SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=? AND user_id=?",
                         (*vals.values(), sid, uid()))
        else:
            vals["user_id"] = uid()
            cur = conn.execute(f"INSERT INTO shifts({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                               tuple(vals.values()))
            sid = cur.lastrowid
        save_uploads("shift", sid, request.files.getlist("photos"))
        set_setting(conn, uid(), "last_employer", emp["id"])
        conn.commit()
        flash("Shift saved.", "ok")
        if request.form.get("again"):
            return redirect(url_for("shift_form"))
        return redirect(safe_next(url_for("shifts_page")))
    last_emp = get_setting(conn, uid(), "last_employer")
    default = int(last_emp) if last_emp and _employer(int(last_emp)) else emps[0]["id"]
    return render_template("shift_form.html", shift=shift, employers=emps,
                           photos=attachments("shift", sid) if sid else [],
                           default_employer=default,
                           today=today().isoformat(), next=safe_next(""))


@app.post("/shifts/<int:sid>/delete")
@login_required
def shift_delete(sid):
    owned("shifts", sid)
    delete_attachments("shift", sid)
    db().execute("DELETE FROM shifts WHERE id=? AND user_id=?", (sid, uid()))
    db().commit()
    flash("Shift deleted.", "ok")
    return redirect(url_for("shifts_page"))


# ---------------------------------------------------------------- paystubs
@app.route("/paystubs")
@login_required
def paystubs_page():
    conn = db()
    emp = _employer(request.args.get("employer", type=int))
    q = ("SELECT p.*, e.name AS employer_name FROM paystubs p JOIN employers e ON e.id=p.employer_id "
         "WHERE p.user_id=? ")
    args = [uid()]
    if emp:
        q += "AND p.employer_id=? "
        args.append(emp["id"])
    stubs = conn.execute(q + "ORDER BY p.period_end DESC", args).fetchall()
    return render_template("paystubs.html", stubs=[(s, calc.reconcile(conn, s)) for s in stubs],
                           employers=calc.employers(conn, uid()), employer_id=emp["id"] if emp else None,
                           files=attachment_counts("paystub"))


@app.route("/paystubs/new", methods=["GET", "POST"])
@app.route("/paystubs/<int:pid>", methods=["GET", "POST"])
@login_required
def paystub_form(pid=None):
    conn = db()
    emps = calc.employers(conn, uid())
    if not emps:
        flash("Add an employer first.", "warn")
        return redirect(url_for("employers_page"))
    stub = owned("paystubs", pid) if pid else None
    if request.method == "POST":
        emp = owned("employers", request.form.get("employer_id", type=int), missing=400)
        vals = dict(
            employer_id=emp["id"],
            period_start=fdate("period_start"), period_end=fdate("period_end"),
            pay_date=fdate("pay_date", required=False),
            hours=fnum("hours", None), wages=fnum("wages", None), tips=fnum("tips", None),
            gross=fnum("gross", None), net=fnum("net", None),
            notes=request.form.get("notes", "").strip())
        if vals["period_start"] > vals["period_end"]:
            vals["period_start"], vals["period_end"] = vals["period_end"], vals["period_start"]
        if pid:
            conn.execute("UPDATE paystubs SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=? AND user_id=?",
                         (*vals.values(), pid, uid()))
        else:
            vals["user_id"] = uid()
            cur = conn.execute(f"INSERT INTO paystubs({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                               tuple(vals.values()))
            pid = cur.lastrowid
        save_uploads("paystub", pid, request.files.getlist("files"))
        conn.commit()
        flash("Paystub saved.", "ok")
        return redirect(url_for("paystub_form", pid=pid))
    rec = calc.reconcile(conn, stub) if stub else None
    prefill = dict(employer=request.args.get("employer", type=int), start=request.args.get("start", ""),
                   end=request.args.get("end", ""), pay_date=request.args.get("pay_date", ""))
    return render_template("paystub_form.html", stub=stub, employers=emps, rec=rec, prefill=prefill,
                           files=attachments("paystub", pid) if pid else [])


@app.post("/paystubs/<int:pid>/delete")
@login_required
def paystub_delete(pid):
    owned("paystubs", pid)
    delete_attachments("paystub", pid)
    db().execute("DELETE FROM paystubs WHERE id=? AND user_id=?", (pid, uid()))
    db().commit()
    flash("Paystub deleted.", "ok")
    return redirect(url_for("paystubs_page"))


# ---------------------------------------------------------------- wage check
@app.route("/wage-check")
@login_required
def wage_check_page():
    conn = db()
    key, start, end, emp = range_args()
    rows = calc.wage_check(conn, uid(), start, end, emp)
    flagged_only = request.args.get("flagged") == "1"
    shown = [r for r in rows if r["status"] != "ok"] if flagged_only else rows
    ws = int(get_setting(conn, uid(), "week_start"))
    return render_template("wage_check.html", rows=rows, shown=shown, flagged_only=flagged_only,
                           ranges=ranges_for(emp), range_key=key, start=start, end=end, employer_id=emp,
                           employers=calc.employers(conn, uid()),
                           owed=sum(r["shortfall"] or 0 for r in rows if r["status"] == "short"),
                           week_start=WEEKDAYS[ws], week_end=WEEKDAYS[(ws + 6) % 7])


# ---------------------------------------------------------------- pay periods
def describe_schedule(e):
    if not calc.has_schedule(e):
        return ""
    s, end = calc.period_containing(e, today())
    pay = calc.payday(e, end)
    label = calc.FREQUENCIES[e["pay_frequency"]]
    if e["pay_frequency"] in ("weekly", "biweekly"):
        return f"{label}, {WEEKDAYS[s.weekday()]}–{WEEKDAYS[end.weekday()]}, paid {WEEKDAYS[pay.weekday()]}"
    return f"{label}, paid {e['payday_offset'] or 0} days after the period ends"


@app.route("/pay-periods")
@login_required
def pay_periods_page():
    conn = db()
    emps = calc.employers(conn, uid())
    scheduled = [e for e in emps if calc.has_schedule(e)]
    emp = _employer(request.args.get("employer", type=int))
    if (not emp or not calc.has_schedule(emp)) and scheduled:
        emp = next((e for e in scheduled if e["active"]), scheduled[0])
    count = max(1, min(request.args.get("n", 8, type=int), 200))
    periods = []
    if emp:
        # enough history to show `count` periods, including the one in progress
        span = {"weekly": 7, "biweekly": 14, "semimonthly": 16, "monthly": 31}[emp["pay_frequency"]]
        periods = calc.pay_periods(conn, emp, today() - timedelta(days=span * (count - 1)), today(), today())[:count]
    return render_template("pay_periods.html", employers=emps, scheduled=scheduled, emp=emp,
                           periods=periods, count=count, schedule=describe_schedule(emp) if emp else "",
                           today=today())


# ---------------------------------------------------------------- employers & settings
@app.route("/employers", methods=["GET", "POST"])
@login_required
def employers_page():
    conn = db()
    if request.method == "POST":
        eid = request.form.get("id", type=int)
        if eid:
            owned("employers", eid, missing=400)
        name = (request.form.get("name") or "").strip()
        if not name:
            abort(400, "Employer name is required.")
        loc = request.form.get("location_id", type=int)
        if loc and not conn.execute("SELECT 1 FROM locations WHERE id=?", (loc,)).fetchone():
            abort(400, "Unknown location.")
        freq = request.form.get("pay_frequency", "")
        if freq not in calc.FREQUENCIES:
            abort(400, "Unknown pay frequency.")
        anchor, offset = "", 0
        if freq:
            anchor = fdate("period_anchor", required=freq in ("weekly", "biweekly"))
            paid_on = fdate("paid_on", required=False)
            if anchor and paid_on:
                offset = calc.payday_offset_for(freq, date.fromisoformat(anchor), date.fromisoformat(paid_on))
                if not 0 <= offset <= 45:
                    abort(400, "The payday should fall after that pay period ends.")
        vals = (name, loc, fnum("hourly_rate"),
                1 if request.form.get("tipped") else 0, 1 if request.form.get("active") else 0,
                request.form.get("notes", "").strip(), freq, anchor, offset)
        if eid:
            conn.execute("UPDATE employers SET name=?, location_id=?, hourly_rate=?, tipped=?, active=?, notes=?, "
                         "pay_frequency=?, period_anchor=?, payday_offset=? WHERE id=? AND user_id=?",
                         (*vals, eid, uid()))
        else:
            conn.execute("INSERT INTO employers(name, location_id, hourly_rate, tipped, active, notes, "
                         "pay_frequency, period_anchor, payday_offset, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (*vals, uid()))
        conn.commit()
        flash(f"Saved {name}.", "ok")
        return redirect(url_for("employers_page"))
    edit = _employer(request.args.get("edit", type=int))
    edit_paid_on = ""
    if edit and edit["pay_frequency"] and edit["period_anchor"]:
        _, pe = calc.period_containing(edit, date.fromisoformat(edit["period_anchor"]))
        edit_paid_on = calc.payday(edit, pe).isoformat()
    return render_template("employers.html", employers=calc.employers(conn, uid()), frequencies=calc.FREQUENCIES,
                           edit_paid_on=edit_paid_on, describe=describe_schedule,
                           locations=conn.execute("SELECT * FROM locations ORDER BY id").fetchall(),
                           edit=edit,
                           welcome=request.args.get("welcome"),
                           shift_counts={r[0]: r[1] for r in conn.execute(
                               "SELECT employer_id, COUNT(*) FROM shifts WHERE user_id=? GROUP BY employer_id",
                               (uid(),))})


@app.post("/employers/<int:eid>/delete")
@login_required
def employer_delete(eid):
    conn = db()
    owned("employers", eid)
    used = conn.execute("SELECT (SELECT COUNT(*) FROM shifts WHERE employer_id=? AND user_id=?) + "
                        "(SELECT COUNT(*) FROM paystubs WHERE employer_id=? AND user_id=?)",
                        (eid, uid(), eid, uid())).fetchone()[0]
    if used:
        flash("That employer has shifts or paystubs. Mark it inactive instead of deleting.", "warn")
    else:
        conn.execute("DELETE FROM employers WHERE id=? AND user_id=?", (eid, uid()))
        conn.commit()
    return redirect(url_for("employers_page"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    conn = db()
    is_admin = bool(current_user()["is_admin"])
    if request.method == "POST":
        action = request.form.get("action")
        if action == "general":
            ws = request.form.get("week_start", type=int) or 0
            set_setting(conn, uid(), "week_start", ws if 0 <= ws <= 6 else 0)
            set_setting(conn, uid(), "overtime_hours", fnum("overtime_hours", 40))
            flash("Settings saved.", "ok")
        elif action in ("location", "rate", "delete_rate"):
            if not is_admin:
                abort(403)
            if action == "location":
                name = (request.form.get("name") or "").strip()
                if name:
                    try:
                        conn.execute("INSERT INTO locations(name, notes) VALUES (?, ?)",
                                     (name, request.form.get("notes", "").strip()))
                        flash(f"Added {name}. Now add its minimum wage below.", "ok")
                    except sqlite3.IntegrityError:
                        flash("A location with that name already exists.", "warn")
            elif action == "rate":
                try:
                    conn.execute(
                        "INSERT INTO wage_rates(location_id, effective_date, min_wage, tipped_cash_wage, source) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(location_id, effective_date) DO UPDATE SET "
                        "min_wage=excluded.min_wage, tipped_cash_wage=excluded.tipped_cash_wage, source=excluded.source",
                        (request.form.get("location_id", type=int), fdate("effective_date"),
                         fnum("min_wage"), fnum("tipped_cash_wage"), request.form.get("source", "").strip()))
                    flash("Rate saved. It applies to everyone using this location.", "ok")
                except sqlite3.IntegrityError:
                    abort(400)
            else:
                conn.execute("DELETE FROM wage_rates WHERE id=?", (request.form.get("rate_id", type=int),))
                flash("Rate removed.", "ok")
        conn.commit()
        return redirect(url_for("settings_page"))
    locs = conn.execute("SELECT * FROM locations ORDER BY id").fetchall()
    rates = {l["id"]: conn.execute("SELECT * FROM wage_rates WHERE location_id=? ORDER BY effective_date DESC",
                                   (l["id"],)).fetchall() for l in locs}
    return render_template("settings.html", locations=locs, rates=rates, weekdays=WEEKDAYS,
                           week_start=int(get_setting(conn, uid(), "week_start")),
                           overtime_hours=float(get_setting(conn, uid(), "overtime_hours")),
                           today=today().isoformat(), is_admin=is_admin)


# ---------------------------------------------------------------- export & backup
def _csv_text(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _csv(rows, header, filename):
    return Response(_csv_text(rows, header), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


SHIFT_HEADER = ["date", "employer", "start", "end", "hours", "hourly_rate", "base_wages",
                "cash_tips", "card_tips", "total", "notes"]
STUB_HEADER = ["employer", "period_start", "period_end", "pay_date", "hours", "wages", "tips",
               "gross", "net", "notes"]


def _shift_rows(user_id):
    return [tuple(r) for r in db().execute(
        "SELECT s.date, e.name, s.start_time, s.end_time, s.hours, s.hourly_rate, "
        "ROUND(s.hours*s.hourly_rate, 2), s.cash_tips, s.card_tips, "
        "ROUND(s.hours*s.hourly_rate + s.cash_tips + s.card_tips, 2), s.notes "
        "FROM shifts s JOIN employers e ON e.id=s.employer_id WHERE s.user_id=? ORDER BY s.date", (user_id,))]


def _stub_rows(user_id):
    return [tuple(r) for r in db().execute(
        "SELECT e.name, p.period_start, p.period_end, p.pay_date, p.hours, p.wages, p.tips, p.gross, p.net, p.notes "
        "FROM paystubs p JOIN employers e ON e.id=p.employer_id WHERE p.user_id=? ORDER BY p.period_end", (user_id,))]


@app.route("/export/shifts.csv")
@login_required
def export_shifts():
    return _csv(_shift_rows(uid()), SHIFT_HEADER, f"shifts-{today()}.csv")


@app.route("/export/paystubs.csv")
@login_required
def export_paystubs():
    return _csv(_stub_rows(uid()), STUB_HEADER, f"paystubs-{today()}.csv")


def _send_zip(build, download_name):
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        build(z)
    resp = send_file(tmp.name, as_attachment=True, download_name=download_name)
    resp.call_on_close(lambda: os.remove(tmp.name))
    return resp


@app.route("/export/my-data.zip")
@login_required
def export_mine():
    """Everything one person entered: CSVs plus their own photos and PDFs."""
    me = current_user()
    emps = db().execute("SELECT e.name, l.name, e.hourly_rate, e.tipped, e.active, e.pay_frequency, "
                        "e.period_anchor, e.payday_offset, e.notes FROM employers e "
                        "LEFT JOIN locations l ON l.id=e.location_id WHERE e.user_id=? ORDER BY e.id", (me["id"],))
    files = db().execute("SELECT a.*, COALESCE(s.date, p.period_end) AS d FROM attachments a "
                         "LEFT JOIN shifts s ON a.kind='shift' AND s.id=a.parent_id "
                         "LEFT JOIN paystubs p ON a.kind='paystub' AND p.id=a.parent_id "
                         "WHERE a.user_id=?", (me["id"],)).fetchall()

    def build(z):
        z.writestr("shifts.csv", _csv_text(_shift_rows(me["id"]), SHIFT_HEADER))
        z.writestr("paystubs.csv", _csv_text(_stub_rows(me["id"]), STUB_HEADER))
        z.writestr("employers.csv", _csv_text([tuple(r) for r in emps],
                   ["name", "location", "hourly_rate", "tipped", "active", "pay_frequency",
                    "period_anchor", "payday_offset_days", "notes"]))
        for a in files:
            path = os.path.join(UPLOAD_DIR, a["filename"])
            if os.path.exists(path):
                z.write(path, f"{a['kind']}s/{a['d'] or 'undated'}_{a['filename']}")
    return _send_zip(build, f"tiptracker-{me['username']}-{today()}.zip")


@app.route("/backup.zip")
@admin_required
def backup():
    """Whole-server backup (every account): database snapshot plus all uploads."""
    snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()

    def build(z):
        z.write(snap, "tiptracker.db")
        for name in os.listdir(UPLOAD_DIR):
            z.write(os.path.join(UPLOAD_DIR, name), f"uploads/{name}")
    try:
        return _send_zip(build, f"tiptracker-backup-{today()}.zip")
    finally:
        os.remove(snap)


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", message=e.description), 400


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", message="Only an admin can do that."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="Not found."), 404


@app.errorhandler(413)
def too_big(_e):
    return render_template("error.html", message="Those files are too large (60 MB max per save)."), 413
