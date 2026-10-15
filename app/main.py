"""Tip Tracker - a small self-hosted app for logging shifts, tips and paystubs."""
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for, Response)
from werkzeug.utils import secure_filename

from . import calc
from .db import DATA_DIR, DB_PATH, UPLOAD_DIR, connect, get_setting, init_db, set_setting

TZ = ZoneInfo(os.environ.get("TZ", "America/Denver"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "pdf"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if APP_PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.full_path))
        return fn(*a, **kw)
    return wrapper


@app.before_request
def _csrf():
    """Simple CSRF guard for form posts."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST" and request.endpoint != "login":
        if not hmac.compare_digest(request.form.get("_csrf", ""), session["csrf"]):
            abort(400, "Form expired - go back, refresh the page and try again.")


@app.context_processor
def _ctx():
    return dict(csrf=session.get("csrf", ""), auth_on=bool(APP_PASSWORD),
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


PAY_RANGES = [("payperiod", "This pay period"), ("lastpayperiod", "Last pay period")]


def _employer(eid):
    return db().execute("SELECT * FROM employers WHERE id=?", (eid,)).fetchone() if eid else None


def range_args():
    emp = request.args.get("employer", type=int)
    key = request.args.get("range", "month")
    erow = _employer(emp)
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
        conn.execute("INSERT INTO attachments(kind, parent_id, filename, original_name) VALUES (?,?,?,?)",
                     (kind, parent_id, name, secure_filename(f.filename)))


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
    return db().execute("SELECT * FROM attachments WHERE kind=? AND parent_id=? ORDER BY id",
                        (kind, parent_id)).fetchall()


def attachment_counts(kind):
    return {r[0]: r[1] for r in db().execute(
        "SELECT parent_id, COUNT(*) FROM attachments WHERE kind=? GROUP BY parent_id", (kind,))}


def delete_attachments(kind, parent_id):
    for a in attachments(kind, parent_id):
        try:
            os.remove(os.path.join(UPLOAD_DIR, a["filename"]))
        except FileNotFoundError:
            pass
    db().execute("DELETE FROM attachments WHERE kind=? AND parent_id=?", (kind, parent_id))


@app.route("/uploads/<name>")
@login_required
def upload(name):
    return send_from_directory(UPLOAD_DIR, secure_filename(name))


@app.post("/attachments/<int:aid>/delete")
@login_required
def delete_attachment(aid):
    a = db().execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if a:
        try:
            os.remove(os.path.join(UPLOAD_DIR, a["filename"]))
        except FileNotFoundError:
            pass
        db().execute("DELETE FROM attachments WHERE id=?", (aid,))
        db().commit()
    return redirect(request.form.get("next") or url_for("dashboard"))


# ---------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("Wrong password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- dashboard
@app.route("/")
@login_required
def dashboard():
    conn = db()
    emps = calc.employers(conn)
    if not emps:
        return redirect(url_for("employers_page", welcome=1))
    key, start, end, emp = range_args()
    shifts = calc.shifts_between(conn, start, end, emp)
    checks = calc.wage_check(conn, start, end, emp)
    problems = [c for c in checks if c["status"] == "short"]
    return render_template(
        "dashboard.html", ranges=ranges_for(emp), range_key=key, start=start, end=end,
        employer_id=emp, employers=emps, t=calc.totals(shifts),
        charts=calc.chart_data(conn, shifts, emps, start, end),
        has_data=bool(shifts), problems=problems,
        owed=sum(c["shortfall"] or 0 for c in problems),
        recent=shifts[:5])


# ---------------------------------------------------------------- shifts
@app.route("/shifts")
@login_required
def shifts_page():
    conn = db()
    key, start, end, emp = range_args()
    rows = calc.shifts_between(conn, start, end, emp)
    return render_template("shifts.html", shifts=rows, t=calc.totals(rows), ranges=ranges_for(emp),
                           range_key=key, start=start, end=end, employer_id=emp,
                           employers=calc.employers(conn), photos=attachment_counts("shift"))


@app.route("/shifts/new", methods=["GET", "POST"])
@app.route("/shifts/<int:sid>", methods=["GET", "POST"])
@login_required
def shift_form(sid=None):
    conn = db()
    emps = calc.employers(conn)
    if not emps:
        flash("Add an employer first.", "warn")
        return redirect(url_for("employers_page"))
    shift = None
    if sid:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone() or abort(404)
    if request.method == "POST":
        emp_id = request.form.get("employer_id", type=int)
        emp = conn.execute("SELECT * FROM employers WHERE id=?", (emp_id,)).fetchone() or abort(400)
        vals = dict(
            employer_id=emp_id, date=fdate("date"),
            start_time=request.form.get("start_time", "").strip(),
            end_time=request.form.get("end_time", "").strip(),
            hours=fnum("hours"), hourly_rate=fnum("hourly_rate", emp["hourly_rate"]),
            cash_tips=fnum("cash_tips"), card_tips=fnum("card_tips"),
            notes=request.form.get("notes", "").strip())
        if vals["hours"] < 0 or vals["hours"] > 24:
            abort(400, "Hours must be between 0 and 24.")
        if sid:
            conn.execute("UPDATE shifts SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=?",
                         (*vals.values(), sid))
        else:
            cur = conn.execute(f"INSERT INTO shifts({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                               tuple(vals.values()))
            sid = cur.lastrowid
        save_uploads("shift", sid, request.files.getlist("photos"))
        set_setting(conn, "last_employer", emp_id)
        conn.commit()
        flash("Shift saved.", "ok")
        if request.form.get("again"):
            return redirect(url_for("shift_form"))
        return redirect(request.form.get("next") or url_for("shifts_page"))
    last_emp = get_setting(conn, "last_employer")
    return render_template("shift_form.html", shift=shift, employers=emps,
                           photos=attachments("shift", sid) if sid else [],
                           default_employer=int(last_emp) if last_emp else emps[0]["id"],
                           today=today().isoformat(), next=request.args.get("next", ""))


@app.post("/shifts/<int:sid>/delete")
@login_required
def shift_delete(sid):
    delete_attachments("shift", sid)
    db().execute("DELETE FROM shifts WHERE id=?", (sid,))
    db().commit()
    flash("Shift deleted.", "ok")
    return redirect(url_for("shifts_page"))


# ---------------------------------------------------------------- paystubs
@app.route("/paystubs")
@login_required
def paystubs_page():
    conn = db()
    emp = request.args.get("employer", type=int)
    q = ("SELECT p.*, e.name AS employer_name FROM paystubs p JOIN employers e ON e.id=p.employer_id ")
    args = []
    if emp:
        q += "WHERE p.employer_id=? "
        args.append(emp)
    stubs = conn.execute(q + "ORDER BY p.period_end DESC", args).fetchall()
    return render_template("paystubs.html", stubs=[(s, calc.reconcile(conn, s)) for s in stubs],
                           employers=calc.employers(conn), employer_id=emp,
                           files=attachment_counts("paystub"))


@app.route("/paystubs/new", methods=["GET", "POST"])
@app.route("/paystubs/<int:pid>", methods=["GET", "POST"])
@login_required
def paystub_form(pid=None):
    conn = db()
    emps = calc.employers(conn)
    if not emps:
        flash("Add an employer first.", "warn")
        return redirect(url_for("employers_page"))
    stub = None
    if pid:
        stub = conn.execute("SELECT * FROM paystubs WHERE id=?", (pid,)).fetchone() or abort(404)
    if request.method == "POST":
        vals = dict(
            employer_id=request.form.get("employer_id", type=int),
            period_start=fdate("period_start"), period_end=fdate("period_end"),
            pay_date=fdate("pay_date", required=False),
            hours=fnum("hours", None), wages=fnum("wages", None), tips=fnum("tips", None),
            gross=fnum("gross", None), net=fnum("net", None),
            notes=request.form.get("notes", "").strip())
        if vals["period_start"] > vals["period_end"]:
            vals["period_start"], vals["period_end"] = vals["period_end"], vals["period_start"]
        if pid:
            conn.execute("UPDATE paystubs SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=?",
                         (*vals.values(), pid))
        else:
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
    delete_attachments("paystub", pid)
    db().execute("DELETE FROM paystubs WHERE id=?", (pid,))
    db().commit()
    flash("Paystub deleted.", "ok")
    return redirect(url_for("paystubs_page"))


# ---------------------------------------------------------------- wage check
@app.route("/wage-check")
@login_required
def wage_check_page():
    conn = db()
    key, start, end, emp = range_args()
    rows = calc.wage_check(conn, start, end, emp)
    flagged_only = request.args.get("flagged") == "1"
    shown = [r for r in rows if r["status"] != "ok"] if flagged_only else rows
    return render_template("wage_check.html", rows=rows, shown=shown, flagged_only=flagged_only, ranges=ranges_for(emp), range_key=key,
                           start=start, end=end, employer_id=emp, employers=calc.employers(conn),
                           owed=sum(r["shortfall"] or 0 for r in rows if r["status"] == "short"),
                           week_start=WEEKDAYS[int(get_setting(conn, "week_start"))],
                           week_end=WEEKDAYS[(int(get_setting(conn, "week_start")) + 6) % 7])


# ---------------------------------------------------------------- pay periods
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def describe_schedule(e):
    if not calc.has_schedule(e):
        return ""
    s, end = calc.period_containing(e, today())
    pay = calc.payday(e, end)
    label = calc.FREQUENCIES[e["pay_frequency"]]
    if e["pay_frequency"] in ("weekly", "biweekly"):
        return f"{label}, {WEEKDAY_NAMES[s.weekday()]}–{WEEKDAY_NAMES[end.weekday()]}, paid {WEEKDAY_NAMES[pay.weekday()]}"
    return f"{label}, paid {e['payday_offset'] or 0} days after the period ends"


@app.route("/pay-periods")
@login_required
def pay_periods_page():
    conn = db()
    emps = calc.employers(conn)
    scheduled = [e for e in emps if calc.has_schedule(e)]
    emp = _employer(request.args.get("employer", type=int))
    if (not emp or not calc.has_schedule(emp)) and scheduled:
        emp = next((e for e in scheduled if e["active"]), scheduled[0])
    count = request.args.get("n", 8, type=int)
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
        name = (request.form.get("name") or "").strip()
        if not name:
            abort(400, "Employer name is required.")
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
        vals = (name, request.form.get("location_id", type=int), fnum("hourly_rate"),
                1 if request.form.get("tipped") else 0, 1 if request.form.get("active") else 0,
                request.form.get("notes", "").strip(), freq, anchor, offset)
        if eid:
            conn.execute("UPDATE employers SET name=?, location_id=?, hourly_rate=?, tipped=?, active=?, notes=?, "
                         "pay_frequency=?, period_anchor=?, payday_offset=? WHERE id=?", (*vals, eid))
        else:
            conn.execute("INSERT INTO employers(name, location_id, hourly_rate, tipped, active, notes, "
                         "pay_frequency, period_anchor, payday_offset) VALUES (?,?,?,?,?,?,?,?,?)", vals)
        conn.commit()
        flash(f"Saved {name}.", "ok")
        return redirect(url_for("employers_page"))
    edit = _employer(request.args.get("edit", type=int))
    edit_paid_on = ""
    if edit and edit["pay_frequency"] and edit["period_anchor"]:
        _, pe = calc.period_containing(edit, date.fromisoformat(edit["period_anchor"]))
        edit_paid_on = calc.payday(edit, pe).isoformat()
    return render_template("employers.html", employers=calc.employers(conn), frequencies=calc.FREQUENCIES,
                           edit_paid_on=edit_paid_on, describe=describe_schedule,
                           locations=conn.execute("SELECT * FROM locations ORDER BY id").fetchall(),
                           edit=edit,
                           welcome=request.args.get("welcome"),
                           shift_counts={r[0]: r[1] for r in conn.execute(
                               "SELECT employer_id, COUNT(*) FROM shifts GROUP BY employer_id")})


@app.post("/employers/<int:eid>/delete")
@login_required
def employer_delete(eid):
    conn = db()
    used = conn.execute("SELECT (SELECT COUNT(*) FROM shifts WHERE employer_id=?) + "
                        "(SELECT COUNT(*) FROM paystubs WHERE employer_id=?)", (eid, eid)).fetchone()[0]
    if used:
        flash("That employer has shifts or paystubs. Mark it inactive instead of deleting.", "warn")
    else:
        conn.execute("DELETE FROM employers WHERE id=?", (eid,))
        conn.commit()
    return redirect(url_for("employers_page"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "general":
            set_setting(conn, "week_start", request.form.get("week_start", type=int) or 0)
            set_setting(conn, "overtime_hours", fnum("overtime_hours", 40))
            flash("Settings saved.", "ok")
        elif action == "location":
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
                flash("Rate saved.", "ok")
            except sqlite3.IntegrityError:
                abort(400)
        elif action == "delete_rate":
            conn.execute("DELETE FROM wage_rates WHERE id=?", (request.form.get("rate_id", type=int),))
            flash("Rate removed.", "ok")
        conn.commit()
        return redirect(url_for("settings_page"))
    locs = conn.execute("SELECT * FROM locations ORDER BY id").fetchall()
    rates = {l["id"]: conn.execute("SELECT * FROM wage_rates WHERE location_id=? ORDER BY effective_date DESC",
                                   (l["id"],)).fetchall() for l in locs}
    return render_template("settings.html", locations=locs, rates=rates, weekdays=WEEKDAYS,
                           week_start=int(get_setting(conn, "week_start")),
                           overtime_hours=float(get_setting(conn, "overtime_hours")),
                           today=today().isoformat())


# ---------------------------------------------------------------- export & backup
def _csv(rows, header, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/export/shifts.csv")
@login_required
def export_shifts():
    rows = db().execute(
        "SELECT s.date, e.name, s.start_time, s.end_time, s.hours, s.hourly_rate, "
        "ROUND(s.hours*s.hourly_rate, 2), s.cash_tips, s.card_tips, "
        "ROUND(s.hours*s.hourly_rate + s.cash_tips + s.card_tips, 2), s.notes "
        "FROM shifts s JOIN employers e ON e.id=s.employer_id ORDER BY s.date").fetchall()
    return _csv([tuple(r) for r in rows],
                ["date", "employer", "start", "end", "hours", "hourly_rate", "base_wages",
                 "cash_tips", "card_tips", "total", "notes"], f"shifts-{today()}.csv")


@app.route("/export/paystubs.csv")
@login_required
def export_paystubs():
    rows = db().execute(
        "SELECT e.name, p.period_start, p.period_end, p.pay_date, p.hours, p.wages, p.tips, p.gross, p.net, p.notes "
        "FROM paystubs p JOIN employers e ON e.id=p.employer_id ORDER BY p.period_end").fetchall()
    return _csv([tuple(r) for r in rows],
                ["employer", "period_start", "period_end", "pay_date", "hours", "wages", "tips",
                 "gross", "net", "notes"], f"paystubs-{today()}.csv")


@app.route("/backup.zip")
@login_required
def backup():
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    snap = tmp.name + ".db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "tiptracker.db")
        for name in os.listdir(UPLOAD_DIR):
            z.write(os.path.join(UPLOAD_DIR, name), f"uploads/{name}")
    os.remove(snap)
    resp = send_file(tmp.name, as_attachment=True, download_name=f"tiptracker-backup-{today()}.zip")
    resp.call_on_close(lambda: os.remove(tmp.name))
    return resp


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", message=e.description), 400


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="Not found."), 404


@app.errorhandler(413)
def too_big(_e):
    return render_template("error.html", message="Those files are too large (60 MB max per save)."), 413
