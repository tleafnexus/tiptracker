"""Totals, chart data, the minimum-wage check and paystub reconciliation."""
from collections import OrderedDict, defaultdict
from datetime import date, timedelta

from .db import get_setting


def d(s):
    return date.fromisoformat(s)


def r2(x):
    return round(x or 0.0, 2)


# ---------------------------------------------------------------- date ranges
RANGES = OrderedDict([
    ("month", "This month"),
    ("lastmonth", "Last month"),
    ("3m", "Last 3 months"),
    ("ytd", "This year"),
    ("12m", "Last 12 months"),
    ("all", "All time"),
    ("custom", "Custom"),
])


def resolve_range(key, start, end, today):
    """Return (key, start_date, end_date) for a preset or custom range."""
    if key == "custom":
        try:
            s, e = d(start), d(end)
            if s > e:
                s, e = e, s
            return key, s, e
        except (TypeError, ValueError):
            key = "month"
    if key == "lastmonth":
        first = today.replace(day=1)
        e = first - timedelta(days=1)
        return key, e.replace(day=1), e
    if key == "3m":
        return key, add_months(today.replace(day=1), -2), today
    if key == "ytd":
        return key, today.replace(month=1, day=1), today
    if key == "12m":
        return key, add_months(today.replace(day=1), -11), today
    if key == "all":
        return key, date(2000, 1, 1), date(2100, 1, 1)
    return "month", today.replace(day=1), today


def add_months(dt, n):
    m = dt.month - 1 + n
    return dt.replace(year=dt.year + m // 12, month=m % 12 + 1, day=1)


def week_start_of(dt, week_start):
    return dt - timedelta(days=(dt.weekday() - week_start) % 7)


# ---------------------------------------------------------------- queries
def employers(conn, include_inactive=True):
    q = ("SELECT e.*, l.name AS location_name FROM employers e "
         "LEFT JOIN locations l ON l.id = e.location_id ")
    if not include_inactive:
        q += "WHERE e.active = 1 "
    return conn.execute(q + "ORDER BY e.id").fetchall()


def shifts_between(conn, start, end, employer_id=None):
    q = ("SELECT s.*, e.name AS employer_name, e.location_id, e.tipped "
         "FROM shifts s JOIN employers e ON e.id = s.employer_id "
         "WHERE s.date BETWEEN ? AND ? ")
    args = [start.isoformat(), end.isoformat()]
    if employer_id:
        q += "AND s.employer_id = ? "
        args.append(employer_id)
    return conn.execute(q + "ORDER BY s.date DESC, s.id DESC", args).fetchall()


def rate_for(conn, location_id, on_date):
    """Minimum-wage rule in force for a location on a date (None if unknown)."""
    if not location_id:
        return None
    return conn.execute(
        "SELECT * FROM wage_rates WHERE location_id=? AND effective_date<=? "
        "ORDER BY effective_date DESC LIMIT 1", (location_id, on_date.isoformat())).fetchone()


# ---------------------------------------------------------------- totals
def shift_money(s):
    wages = (s["hours"] or 0) * (s["hourly_rate"] or 0)
    cash, card = s["cash_tips"] or 0, s["card_tips"] or 0
    return wages, cash, card


def totals(shifts):
    t = dict(hours=0.0, wages=0.0, cash=0.0, card=0.0, shifts=len(shifts))
    for s in shifts:
        w, c, k = shift_money(s)
        t["hours"] += s["hours"] or 0
        t["wages"] += w
        t["cash"] += c
        t["card"] += k
    t["tips"] = t["cash"] + t["card"]
    t["total"] = t["wages"] + t["tips"]
    t["per_hour"] = t["total"] / t["hours"] if t["hours"] else 0.0
    t["tips_per_hour"] = t["tips"] / t["hours"] if t["hours"] else 0.0
    return {k: (r2(v) if isinstance(v, float) else v) for k, v in t.items()}


def chart_data(conn, shifts, emp_rows, start, end):
    """Data for the dashboard charts, as plain JSON-able dicts."""
    # Employers keep a fixed color slot by their id order, never by rank.
    slot = {e["id"]: i for i, e in enumerate(emp_rows)}

    by_emp = OrderedDict()
    for s in shifts:
        w, c, k = shift_money(s)
        b = by_emp.setdefault(s["employer_id"], dict(
            name=s["employer_name"], slot=slot.get(s["employer_id"], 0),
            wages=0.0, cash=0.0, card=0.0, hours=0.0))
        b["wages"] += w
        b["cash"] += c
        b["card"] += k
        b["hours"] += s["hours"] or 0
    emp_list = sorted(by_emp.values(), key=lambda b: -(b["wages"] + b["cash"] + b["card"]))
    for b in emp_list:
        for k in ("wages", "cash", "card", "hours"):
            b[k] = r2(b[k])
        b["total"] = r2(b["wages"] + b["cash"] + b["card"])

    # Monthly buckets (or weekly when the range is short).
    first = min((d(s["date"]) for s in shifts), default=start)
    last = max((d(s["date"]) for s in shifts), default=end)
    span_days = (last - first).days
    weekly = span_days <= 62
    ws = int(get_setting(conn, "week_start"))

    def bucket(dt):
        return week_start_of(dt, ws) if weekly else dt.replace(day=1)

    keys = []
    if shifts:
        k = bucket(first)
        while k <= last:
            keys.append(k)
            k = k + timedelta(days=7) if weekly else add_months(k, 1)
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    per_emp = OrderedDict((e["id"], dict(name=e["name"], slot=slot[e["id"]], values=[0.0] * n))
                          for e in emp_rows if e["id"] in by_emp)
    cash_series, card_series = [0.0] * n, [0.0] * n
    for s in shifts:
        i = idx[bucket(d(s["date"]))]
        w, c, k = shift_money(s)
        per_emp[s["employer_id"]]["values"][i] += w + c + k
        cash_series[i] += c
        card_series[i] += k
    for p in per_emp.values():
        p["values"] = [r2(v) for v in p["values"]]

    if weekly:
        labels = ["Wk of " + k.strftime("%b %-d") for k in keys]
    else:
        labels = [k.strftime("%b %Y") if k.month == 1 or i == 0 else k.strftime("%b")
                  for i, k in enumerate(keys)]

    # Weekly effective hourly rate (all jobs) vs. the minimum wage that applied.
    weeks = OrderedDict()
    for s in sorted(shifts, key=lambda s: s["date"]):
        wk = week_start_of(d(s["date"]), ws)
        b = weeks.setdefault(wk, dict(hours=0.0, total=0.0, minw=0.0))
        w, c, k = shift_money(s)
        b["hours"] += s["hours"] or 0
        b["total"] += w + c + k
        rate = rate_for(conn, s["location_id"], d(s["date"]))
        if rate:
            b["minw"] = max(b["minw"], rate["min_wage"])
    eff = dict(
        labels=[wk.strftime("%b %-d") for wk in weeks],
        rate=[r2(b["total"] / b["hours"]) if b["hours"] else None for b in weeks.values()],
        minimum=[b["minw"] or None for b in weeks.values()],
    )

    return dict(
        employers=emp_list,
        period=dict(labels=labels, unit="week" if weekly else "month",
                    employers=list(per_emp.values()),
                    cash=[r2(v) for v in cash_series], card=[r2(v) for v in card_series]),
        effective=eff,
    )


# ---------------------------------------------------------------- minimum wage check
def wage_check(conn, start, end, employer_id=None):
    """One row per employer per workweek.

    In New Mexico an employer may pay a tipped worker a lower cash wage only if
    cash wage + tips reach the full minimum wage for the hours worked; otherwise
    the employer owes the difference. We check that per workweek, per employer.
    """
    ws = int(get_setting(conn, "week_start"))
    ot = float(get_setting(conn, "overtime_hours"))
    wk_start = week_start_of(start, ws)
    rows = shifts_between(conn, wk_start, end, employer_id)
    groups = OrderedDict()
    for s in sorted(rows, key=lambda s: (s["date"], s["employer_id"])):
        wk = week_start_of(d(s["date"]), ws)
        g = groups.setdefault((wk, s["employer_id"]), dict(
            week=wk, week_end=wk + timedelta(days=6), employer=s["employer_name"],
            employer_id=s["employer_id"], location_id=s["location_id"], tipped=s["tipped"],
            shifts=[], hours=0.0, wages=0.0, cash=0.0, card=0.0, min_rate=None))
        w, c, k = shift_money(s)
        g["shifts"].append(s)
        g["hours"] += s["hours"] or 0
        g["wages"] += w
        g["cash"] += c
        g["card"] += k
        rr = s["hourly_rate"] or 0
        g["min_rate"] = rr if g["min_rate"] is None else min(g["min_rate"], rr)

    out = []
    for g in groups.values():
        rate = rate_for(conn, g["location_id"], g["week"])
        g["tips"] = g["cash"] + g["card"]
        g["earned"] = g["wages"] + g["tips"]
        g["per_hour"] = g["earned"] / g["hours"] if g["hours"] else 0.0
        g["issues"] = []
        if rate is None:
            g["status"] = "unknown"
            g["min_wage"] = g["required"] = g["shortfall"] = None
            g["issues"].append("No minimum-wage rate set for this employer's location.")
        else:
            g["min_wage"] = rate["min_wage"]
            g["tipped_cash_wage"] = rate["tipped_cash_wage"]
            g["required"] = g["hours"] * rate["min_wage"]
            g["shortfall"] = max(0.0, g["required"] - g["earned"])
            floor = rate["tipped_cash_wage"] if g["tipped"] else rate["min_wage"]
            if g["min_rate"] is not None and g["min_rate"] + 1e-9 < floor:
                g["issues"].append(
                    f"Base pay ${g['min_rate']:.2f}/hr is below the ${floor:.2f}/hr "
                    + ("tipped cash wage" if g["tipped"] else "minimum wage")
                    + " required here, even with tips.")
            if g["shortfall"] > 0.005:
                g["issues"].append(
                    f"Base pay + tips came to ${g['earned']:.2f}, short of "
                    f"{g['hours']:g} hrs × ${rate['min_wage']:.2f} = ${g['required']:.2f}. "
                    f"The employer owes ${g['shortfall']:.2f} unless the paystub already shows a make-up payment.")
            g["status"] = "short" if g["issues"] else "ok"
        if g["hours"] > ot:
            g["issues"].append(f"{g['hours']:g} hours at one employer this week: hours over {ot:g} "
                               "should be paid as overtime (1.5×).")
            if g["status"] == "ok":
                g["status"] = "note"
        for k in ("hours", "wages", "cash", "card", "tips", "earned", "per_hour"):
            g[k] = r2(g[k])
        if g["shortfall"] is not None:
            g["shortfall"] = r2(g["shortfall"])
            g["required"] = r2(g["required"])
        out.append(g)
    out.sort(key=lambda g: (g["week"], g["employer"]), reverse=True)
    return out


# ---------------------------------------------------------------- pay schedules
FREQUENCIES = OrderedDict([
    ("", "Not set"),
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 weeks"),
    ("semimonthly", "Twice a month (1st–15th, 16th–end)"),
    ("monthly", "Monthly"),
])


def _month_end(dt):
    return add_months(dt.replace(day=1), 1) - timedelta(days=1)


def has_schedule(emp):
    f = emp["pay_frequency"] or ""
    if f in ("weekly", "biweekly"):
        return bool(emp["period_anchor"])
    return f in ("semimonthly", "monthly")


def period_containing(emp, dt, freq=None, anchor=None):
    """(start, end) of the pay period that includes dt."""
    f = freq if freq is not None else emp["pay_frequency"]
    if f in ("weekly", "biweekly"):
        n = 7 if f == "weekly" else 14
        a = anchor or d(emp["period_anchor"])
        s = a + timedelta(days=((dt - a).days // n) * n)
        return s, s + timedelta(days=n - 1)
    if f == "semimonthly":
        if dt.day <= 15:
            return dt.replace(day=1), dt.replace(day=15)
        return dt.replace(day=16), _month_end(dt)
    if f == "monthly":
        return dt.replace(day=1), _month_end(dt)
    return None


def payday_offset_for(freq, anchor, paid_on):
    """Days from the end of the period that started on `anchor` to its payday."""
    _, end = period_containing(None, anchor, freq, anchor)
    return (paid_on - end).days


def payday(emp, period_end):
    return period_end + timedelta(days=emp["payday_offset"] or 0)


def pay_periods(conn, emp, start, end, today):
    """Every pay period for an employer that overlaps [start, end], newest first."""
    if not has_schedule(emp):
        return []
    ps, _ = period_containing(emp, start)
    out = []
    while ps <= end:
        s, e = period_containing(emp, ps)
        rows = conn.execute("SELECT * FROM shifts WHERE employer_id=? AND date BETWEEN ? AND ?",
                            (emp["id"], s.isoformat(), e.isoformat())).fetchall()
        t = totals(rows)
        pay = payday(emp, e)
        stub = conn.execute(
            "SELECT * FROM paystubs WHERE employer_id=? AND period_end BETWEEN ? AND ? "
            "ORDER BY ABS(julianday(period_end)-julianday(?)) LIMIT 1",
            (emp["id"], s.isoformat(), e.isoformat(), e.isoformat())).fetchone()
        weeks = [w for w in wage_check(conn, s, e, emp["id"]) if s <= w["week"] <= e]
        topup = r2(sum(w["shortfall"] or 0 for w in weeks if w["status"] == "short"))
        if today < s:
            state = "future"
        elif today <= e:
            state = "open"
        elif today < pay:
            state = "waiting"
        else:
            state = "paid"
        out.append(dict(start=s, end=e, payday=pay, t=t, shifts=len(rows),
                        expected=r2(t["wages"] + t["card"]), stub=stub,
                        stub_check=reconcile(conn, stub) if stub else None,
                        topup=topup, state=state))
        ps = e + timedelta(days=1)
    out.reverse()
    return out


# ---------------------------------------------------------------- paystubs
def reconcile(conn, stub):
    """Compare a paystub with the shifts logged for that employer and period."""
    rows = conn.execute(
        "SELECT * FROM shifts WHERE employer_id=? AND date BETWEEN ? AND ?",
        (stub["employer_id"], stub["period_start"], stub["period_end"])).fetchall()
    t = totals(rows)
    checks = []

    def cmp(label, logged, stub_val, tol, fmt):
        if stub_val is None:
            return
        diff = r2(stub_val - logged)
        checks.append(dict(label=label, logged=logged, stub=stub_val, diff=diff,
                           ok=abs(diff) <= tol, fmt=fmt))

    cmp("Hours", t["hours"], stub["hours"], 0.25, "h")
    cmp("Base wages", t["wages"], stub["wages"], 1.0, "$")
    cmp("Card tips", t["card"], stub["tips"], 1.0, "$")
    expected_gross = r2(t["wages"] + t["card"])
    cmp("Gross (wages + card tips)", expected_gross, stub["gross"], 1.0, "$")
    if checks and checks[-1]["label"].startswith("Gross") and not checks[-1]["ok"] \
            and t["cash"] and abs(checks[-1]["diff"] - t["cash"]) <= 1.0:
        checks[-1]["ok"] = True
        checks[-1]["note"] = "Matches once declared cash tips are included in gross."
    status = "empty" if not checks else ("ok" if all(c["ok"] for c in checks) else "mismatch")
    return dict(logged=t, checks=checks, status=status, shift_count=len(rows))
