"""Multi-user tests. Run:  python3 tests/test_multiuser.py <empty-data-dir> [legacy-db-dir]

Each scenario runs in a fresh process because the app reads DATA_DIR at import.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def client_for(app):
    c = app.test_client()
    c.get("/login")
    return c


def tok(c, url="/login"):
    html = c.get(url).get_data(as_text=True)
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert m, (url, html[:300])
    return m.group(1)


def login(c, username, password):
    return c.post("/login", data={"_csrf": tok(c), "username": username, "password": password})


def add_employer(c, name, rate="7.20", loc=1, **extra):
    data = {"_csrf": tok(c, "/employers"), "name": name, "location_id": loc, "hourly_rate": rate,
            "tipped": "on", "active": "on", **extra}
    r = c.post("/employers", data=data)
    assert r.status_code == 302, r.get_data(as_text=True)[:400]


def add_shift(c, emp_id, day, hours=6, cash=40, card=80, photo=False, rate=None):
    data = {"_csrf": tok(c, "/shifts/new"), "employer_id": emp_id, "date": day, "hours": hours,
            "cash_tips": cash, "card_tips": card}
    if rate is not None:
        data["hourly_rate"] = rate
    if photo:
        from PIL import Image
        b = io.BytesIO()
        Image.new("RGB", (40, 30), "white").save(b, "JPEG")
        b.seek(0)
        data["photos"] = (b, "slip.jpg")
    r = c.post("/shifts/new", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)[:400]


def scenario_fresh(data_dir):
    os.environ["DATA_DIR"] = data_dir
    os.environ["APP_PASSWORD"] = "setup-key-123"
    sys.path.insert(0, ROOT)
    from app.main import app
    from app.db import connect, UPLOAD_DIR
    app.config["TESTING"] = True

    a = client_for(app)
    # --- first-run setup
    assert a.get("/").location.endswith("/setup")
    assert a.get("/login").location.endswith("/setup")
    t = tok(a, "/setup")
    r = a.post("/setup", data={"_csrf": t, "setup_key": "wrong", "username": "alex", "password": "password1", "confirm": "password1"})
    assert r.status_code == 302 and connect().execute("select count(*) from users").fetchone()[0] == 0
    r = a.post("/setup", data={"_csrf": t, "setup_key": "setup-key-123", "username": "Alex", "display_name": "Alex",
                               "password": "short", "confirm": "short"})
    assert connect().execute("select count(*) from users").fetchone()[0] == 0
    r = a.post("/setup", data={"_csrf": t, "setup_key": "setup-key-123", "username": "Alex", "display_name": "Alex",
                               "password": "password1", "confirm": "password1"})
    assert r.status_code == 302 and r.location.endswith("/"), r.location
    assert a.get("/setup").status_code == 302        # setup is gone once an account exists
    assert a.get("/admin/users").status_code == 200
    print("setup ok")

    # --- admin creates a member with a temp password
    r = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "create", "username": "sam",
                                     "display_name": "Sam", "password": "temp-pass-1"})
    assert r.status_code == 302
    dup = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "create", "username": "SAM", "password": "temp-pass-1"})
    assert connect().execute("select count(*) from users").fetchone()[0] == 2
    bad = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "create", "username": "x y", "password": "temp-pass-1"})
    assert bad.status_code == 400

    s = client_for(app)
    assert login(s, "sam", "wrong-password").status_code == 401
    r = login(s, "SAM", "temp-pass-1")                 # usernames are case-insensitive
    assert r.status_code == 302
    r = s.get("/")
    assert r.status_code == 302 and r.location.endswith("/account"), "temp password must be changed first"
    assert s.get("/shifts").location.endswith("/account")
    r = s.post("/account", data={"_csrf": tok(s, "/account"), "action": "password", "current": "temp-pass-1",
                                 "password": "sams-real-pw", "confirm": "sams-real-pw"})
    assert r.status_code == 302 and s.get("/").status_code == 302  # no employers yet -> employers page
    assert s.get("/").location.endswith("/employers?welcome=1")
    print("member onboarding ok")

    # --- data for both
    add_employer(a, "Alex Cantina", "7.20", pay_frequency="biweekly", period_anchor="2026-08-30", paid_on="2026-09-18")
    add_employer(a, "Alex Diner", "3.00", loc=2)
    add_employer(s, "Sam Bar", "7.50")
    conn = connect()
    ids = {r["name"]: r["id"] for r in conn.execute("select id, name from employers")}
    for d in ("2026-10-01", "2026-10-02", "2026-10-05"):
        add_shift(a, ids["Alex Cantina"], d, photo=True)
    add_shift(a, ids["Alex Diner"], "2026-10-06", hours=6, cash=3, card=4)   # under minimum wage
    add_shift(s, ids["Sam Bar"], "2026-10-03", hours=5, cash=11, card=22, photo=True)
    r = a.post("/paystubs/new", data={"_csrf": tok(a, "/paystubs/new"), "employer_id": ids["Alex Cantina"],
               "period_start": "2026-09-27", "period_end": "2026-10-10", "hours": 18, "files": (io.BytesIO(b"%PDF-1.4"), "stub.pdf")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    alex_uid = conn.execute("select id from users where username='alex'").fetchone()[0]
    sam_uid = conn.execute("select id from users where username='sam'").fetchone()[0]
    alex_shift = conn.execute("select id from shifts where user_id=?", (alex_uid,)).fetchone()[0]
    alex_stub = conn.execute("select id from paystubs where user_id=?", (alex_uid,)).fetchone()[0]
    alex_att = conn.execute("select id, filename from attachments where user_id=?", (alex_uid,)).fetchone()
    sam_att = conn.execute("select id, filename from attachments where user_id=?", (sam_uid,)).fetchone()
    for t_ in ("employers", "shifts", "paystubs", "attachments"):
        assert conn.execute(f"select count(*) from {t_} where user_id is null").fetchone()[0] == 0, t_
    print("data entry ok")

    n_rates = connect().execute("select count(*) from wage_rates").fetchone()[0]
    # --- isolation: Sam tries to reach Alex's data every way we can think of
    leak_words = ["Alex Cantina", "Alex Diner"]

    def no_leak(resp, where):
        body = resp.get_data(as_text=True)
        for w in leak_words:
            assert w not in body, f"LEAK of {w!r} at {where}"

    for url in ["/", "/shifts?range=all", "/paystubs", "/wage-check?range=all", "/pay-periods", "/employers",
                f"/?employer={ids['Alex Cantina']}&range=all", f"/shifts?employer={ids['Alex Cantina']}&range=all",
                f"/pay-periods?employer={ids['Alex Cantina']}", f"/paystubs?employer={ids['Alex Cantina']}",
                f"/wage-check?range=payperiod&employer={ids['Alex Cantina']}", f"/employers?edit={ids['Alex Cantina']}",
                f"/paystubs/new?employer={ids['Alex Cantina']}", "/settings", "/export/shifts.csv", "/export/paystubs.csv"]:
        r = s.get(url)
        assert r.status_code in (200, 302), (url, r.status_code)
        no_leak(r, url)
    assert s.get(f"/shifts/{alex_shift}").status_code == 404
    assert s.get(f"/paystubs/{alex_stub}").status_code == 404
    assert s.get(f"/uploads/{alex_att['filename']}").status_code == 404
    assert a.get(f"/uploads/{alex_att['filename']}").status_code == 200
    assert a.get(f"/uploads/{sam_att['filename']}").status_code == 404   # admins can't see members' photos either
    st = tok(s, "/shifts/new")
    posts = [
        (f"/shifts/{alex_shift}", {"employer_id": ids["Sam Bar"], "date": "2026-10-01", "hours": 1}),
        (f"/shifts/{alex_shift}/delete", {}),
        ("/shifts/new", {"employer_id": ids["Alex Cantina"], "date": "2026-10-01", "hours": 1}),
        (f"/paystubs/{alex_stub}", {"employer_id": ids["Sam Bar"], "period_start": "2026-10-01", "period_end": "2026-10-02"}),
        (f"/paystubs/{alex_stub}/delete", {}),
        ("/paystubs/new", {"employer_id": ids["Alex Cantina"], "period_start": "2026-10-01", "period_end": "2026-10-02"}),
        ("/employers", {"id": ids["Alex Cantina"], "name": "hijacked", "hourly_rate": 1}),
        (f"/employers/{ids['Alex Diner']}/delete", {}),
        (f"/attachments/{alex_att['id']}/delete", {}),
        ("/settings", {"action": "rate", "location_id": 1, "effective_date": "2027-01-01", "min_wage": 1, "tipped_cash_wage": 1}),
        ("/settings", {"action": "delete_rate", "rate_id": 1}),
        ("/admin/users", {"action": "create", "username": "evil", "password": "password123"}),
        ("/admin/users", {"action": "toggle_admin", "user_id": sam_uid}),
    ]
    for url, data in posts:
        r = s.post(url, data={"_csrf": st, **data})
        assert r.status_code in (400, 403, 404), (url, r.status_code)
    assert s.get("/admin/users").status_code == 403
    assert s.get("/backup.zip").status_code == 403
    conn = connect()
    assert conn.execute("select count(*) from shifts where user_id=?", (alex_uid,)).fetchone()[0] == 4
    assert conn.execute("select count(*) from shifts where user_id=?", (sam_uid,)).fetchone()[0] == 1
    assert conn.execute("select name from employers where id=?", (ids["Alex Cantina"],)).fetchone()[0] == "Alex Cantina"
    assert conn.execute("select count(*) from paystubs").fetchone()[0] == 1
    assert conn.execute("select count(*) from attachments where user_id=?", (alex_uid,)).fetchone()[0] == 4
    assert conn.execute("select count(*) from wage_rates").fetchone()[0] == n_rates
    assert conn.execute("select count(*) from users").fetchone()[0] == 2
    assert not conn.execute("select is_admin from users where id=?", (sam_uid,)).fetchone()[0]
    print("isolation ok")

    # --- numbers are per person
    body = s.get("/shifts?range=all").get_data(as_text=True)
    assert "$70.50" in body and "1 shifts" in body, "Sam: 5h x 7.50 + 11 + 22 = 70.50"
    body = a.get("/wage-check?range=all").get_data(as_text=True)
    assert "Underpaid" in body and "Alex Diner" in body
    z = zipfile.ZipFile(io.BytesIO(s.get("/export/my-data.zip").data))
    names = z.namelist()
    assert "shifts.csv" in names and sum(n.startswith("shifts/") for n in names) == 1, names
    assert "Alex" not in z.read("shifts.csv").decode() + z.read("employers.csv").decode()
    full = zipfile.ZipFile(io.BytesIO(a.get("/backup.zip").data))
    assert "tiptracker.db" in full.namelist() and len([n for n in full.namelist() if n.startswith("uploads/")]) == 5
    print("per-person totals and exports ok")

    # --- per-person settings
    a.post("/settings", data={"_csrf": tok(a, "/settings"), "action": "general", "week_start": 6, "overtime_hours": 40})
    assert "Sunday–Saturday" in a.get("/wage-check").get_data(as_text=True)
    assert "Monday–Sunday" in s.get("/wage-check").get_data(as_text=True)
    # admin can edit shared rates
    r = a.post("/settings", data={"_csrf": tok(a, "/settings"), "action": "rate", "location_id": 1,
                                  "effective_date": "2028-01-01", "min_wage": 14, "tipped_cash_wage": 7.30})
    assert connect().execute("select count(*) from wage_rates").fetchone()[0] == n_rates + 1
    print("settings ok")

    # --- sessions: password change signs out other devices; turning off an account signs it out
    s2 = client_for(app)
    assert login(s2, "sam", "sams-real-pw").status_code == 302 and s2.get("/shifts").status_code == 200
    s.post("/account", data={"_csrf": tok(s, "/account"), "action": "password", "current": "sams-real-pw",
                             "password": "sams-new-pw!", "confirm": "sams-new-pw!"})
    assert s.get("/shifts").status_code == 200, "the device that changed it stays signed in"
    assert s2.get("/shifts").status_code == 302, "other devices are signed out"
    r = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "toggle_active", "user_id": sam_uid})
    assert s.get("/shifts").status_code == 302
    s3 = client_for(app)
    assert login(s3, "sam", "sams-new-pw!").status_code == 401
    a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "toggle_active", "user_id": sam_uid})
    assert login(s3, "sam", "sams-new-pw!").status_code == 302
    # admin reset -> temp password shown once, old password stops working
    r = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "reset", "user_id": sam_uid}, follow_redirects=True)
    temp = re.search(r"New temporary password for sam: (\S+) ", r.get_data(as_text=True)).group(1)
    assert s3.get("/shifts").status_code == 302
    s4 = client_for(app)
    assert login(s4, "sam", "sams-new-pw!").status_code == 401
    assert login(s4, "sam", temp).status_code == 302 and s4.get("/").location.endswith("/account")
    print("sessions ok")

    # --- last-admin guards
    r = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "toggle_admin", "user_id": alex_uid})
    assert connect().execute("select is_admin from users where id=?", (alex_uid,)).fetchone()[0] == 1
    a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "delete", "user_id": alex_uid, "confirm": "alex"})
    assert connect().execute("select count(*) from users where id=?", (alex_uid,)).fetchone()[0] == 1
    print("admin guards ok")

    # --- lockout after repeated failures
    l = client_for(app)
    codes = [login(l, "alex", f"nope{i}").status_code for i in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429, codes
    assert login(l, "alex", "password1").status_code == 429, "locked even with the right password"
    print("lockout ok")

    # --- delete a member: their rows and files go, Alex's stay
    sam_files = [r[0] for r in connect().execute("select filename from attachments where user_id=?", (sam_uid,))]
    r = a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "delete", "user_id": sam_uid, "confirm": "wrong"})
    assert connect().execute("select count(*) from users").fetchone()[0] == 2
    a.post("/admin/users", data={"_csrf": tok(a, "/admin/users"), "action": "delete", "user_id": sam_uid, "confirm": "sam"})
    conn = connect()
    assert conn.execute("select count(*) from users").fetchone()[0] == 1
    for t_ in ("employers", "shifts", "paystubs", "attachments"):
        assert conn.execute(f"select count(*) from {t_} where user_id=?", (sam_uid,)).fetchone()[0] == 0
    assert all(not os.path.exists(os.path.join(UPLOAD_DIR, f)) for f in sam_files)
    assert conn.execute("select count(*) from shifts").fetchone()[0] == 4
    print("delete ok")

    # --- CSRF still enforced
    assert a.post("/settings", data={"_csrf": "nope", "action": "general"}).status_code == 400
    assert l.post("/login", data={"username": "alex", "password": "password1"}).status_code == 400
    print("ALL FRESH-INSTALL TESTS PASSED")


def scenario_upgrade(data_dir, legacy_dir):
    """A database from the single-user version keeps all its data after upgrading."""
    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.copytree(legacy_dir, data_dir)
    os.environ["DATA_DIR"] = data_dir
    os.environ.pop("APP_PASSWORD", None)
    sys.path.insert(0, ROOT)
    import sqlite3
    before = sqlite3.connect(os.path.join(data_dir, "tiptracker.db"))
    counts = {t: before.execute(f"select count(*) from {t}").fetchone()[0] for t in ("employers", "shifts", "paystubs", "attachments")}
    old_total = before.execute("select round(sum(hours*hourly_rate+cash_tips+card_tips),2) from shifts").fetchone()[0]
    before.close()
    from app.main import app
    from app.db import connect
    c = client_for(app)
    assert c.get("/").location.endswith("/setup")
    page = c.get("/setup").get_data(as_text=True)
    assert "already has data" in page and "Setup key" not in page
    r = c.post("/setup", data={"_csrf": tok(c, "/setup"), "username": "jess", "display_name": "Jess",
                               "password": "jess-password", "confirm": "jess-password"})
    assert r.status_code == 302
    conn = connect()
    uid = conn.execute("select id from users").fetchone()[0]
    for t, n in counts.items():
        assert conn.execute(f"select count(*) from {t} where user_id=?", (uid,)).fetchone()[0] == n, t
    assert conn.execute("select round(sum(hours*hourly_rate+cash_tips+card_tips),2) from shifts where user_id=?", (uid,)).fetchone()[0] == old_total
    assert conn.execute("select value from user_settings where user_id=? and key='last_employer'", (uid,)).fetchone()
    for url in ["/?range=all", "/shifts?range=all", "/paystubs", "/paystubs/1", "/wage-check?range=all",
                "/pay-periods?employer=1", "/employers", "/settings", "/account", "/admin/users", "/export/my-data.zip", "/backup.zip"]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
    fn = conn.execute("select filename from attachments").fetchone()[0]
    assert c.get(f"/uploads/{fn}").status_code == 200
    print(f"UPGRADE TEST PASSED: {counts} kept, total ${old_total}")


if __name__ == "__main__":
    data = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] != "--fresh":
        if sys.argv[-1] == "--run-upgrade":
            scenario_upgrade(data, sys.argv[2])
        else:
            shutil.rmtree(data, ignore_errors=True)
            subprocess.run([sys.executable, __file__, data, "--fresh"], check=True)
            subprocess.run([sys.executable, __file__, data + "-upgrade", sys.argv[2], "--run-upgrade"], check=True)
    else:
        if len(sys.argv) == 2:
            shutil.rmtree(data, ignore_errors=True)
        scenario_fresh(data)
