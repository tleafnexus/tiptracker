# Tip Tracker

A small self-hosted web app for logging shifts, cash and card tips, and paystubs across several employers, with charts and a minimum-wage check. It's one Docker container storing everything in SQLite, and it works in a phone browser.

## What it does

- **Log shifts**: employer, date, clock in/out (hours fill in automatically), cash tips, card tips, and a **photo of the tip slip** taken with the phone camera.
- **Pay periods**: set each employer's pay schedule (weekly, every 2 weeks, twice a month, or monthly) by copying two dates off any paystub. Shifts are then grouped into paychecks, with hours, base pay, card tips, the expected check (before taxes), cash already in hand, the payday, and any minimum-wage top-up owed. The date filter on each page also gets "This pay period" and "Last pay period" once an employer is picked.
- **Paystubs**: attach a photo or PDF and enter the totals. Each stub is compared with the shifts logged for that pay period, and missing hours or wages are flagged.
- **Dashboard charts**: earnings by employer (wages / cash / card), cash vs. card tips over time, earnings per month by employer, and hourly earnings per week against minimum wage.
- **Minimum wage check**: for each workweek at each employer, base pay + all tips is compared with *hours × local minimum wage*. If a tipped job falls short, New Mexico law says the employer owes the difference, and the app shows by how much. It also flags a base rate below the local tipped cash minimum and weeks over 40 hours.
- **Export / backup**: CSV of shifts and paystubs, plus a one-click `.zip` backup (database + every photo).

## Run it

You need Docker with the Compose plugin.

```bash
cd tiptracker
# edit docker-compose.yml and set APP_PASSWORD to something real
docker compose up -d --build
```

Open `http://<server-ip>:8000` from her phone. In Safari use Share → **Add to Home Screen**, or in Chrome use ⋮ → **Add to Home screen**. It then opens like an app.

Data lives in `./data` next to `docker-compose.yml` (`tiptracker.db` + `uploads/`).

### Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `APP_PASSWORD` | *(none)* | If set, a login page protects everything. **Set this** unless the server is only reachable on your home network. |
| `TZ` | `America/Denver` | Used for "today" on new shifts. |
| `SECRET_KEY` | auto-generated in `/data/.secret_key` | Signs login cookies. |

### Reaching it away from home

The simplest safe option is **Tailscale** on the server and her phone: no ports opened, and it works anywhere. If you put it behind a reverse proxy (Caddy, Nginx Proxy Manager, Traefik) on the public internet, use HTTPS and set `APP_PASSWORD`.

### Updating

```bash
docker compose up -d --build
```

The database is created or upgraded automatically on start, and the `./data` folder is untouched.

### Backups

Use **Settings → Download full backup** now and then, or back up the `./data` folder with whatever you already use. To restore, stop the container, unzip the backup into `./data`, and start it again.

## Minimum wage rates

These rates were pre-loaded in September 2026 and can be edited under **Settings**:

| Location | Min wage | Tipped cash wage | Notes |
|---|---|---|---|
| Albuquerque (city limits) 2026 | $12.00 | $7.20 | State $12 beats the city's $11.85; city's $7.20 tipped wage beats the state's $3 |
| Albuquerque 2027 | $13.00 | $7.20 | Ordinance O-26-33; check against the city's official notice (posted by Oct 15) |
| Bernalillo County (unincorporated) 2026 | $12.00 | $3.00 | County rate is below the state's, so state rules apply |
| New Mexico elsewhere 2026 | $12.00 | $3.00 | |

When a rate changes, add a new row with its start date. Older weeks keep using the rate that applied at the time. Pick the right location for each employer on the **Employers** page. Being inside Albuquerque city limits vs. unincorporated county changes the tipped minimum ($7.20 vs. $3.00).

The check is a record-keeping aid, not legal advice. A flagged week is a reason to look at that paystub (some employers add a "tip credit make-up" line) and, if it's still short, ask the employer.

## Running without Docker

```bash
pip install -r requirements.txt
DATA_DIR=./data gunicorn -b 0.0.0.0:8000 app.main:app
```
