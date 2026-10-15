# Tip Tracker

A small self-hosted web app for logging shifts, cash and card tips, and paystubs across several employers, with charts and a minimum-wage check. It's one Docker container storing everything in SQLite, and it works in a phone browser. Several people can use one server, each with their own private account.

## What it does

- **Log shifts**: employer, date, clock in/out (hours fill in automatically), cash tips, card tips, and a **photo of the tip slip** taken with the phone camera.
- **Pay periods**: set each employer's pay schedule (weekly, every 2 weeks, twice a month, or monthly) by copying two dates off any paystub. Shifts are then grouped into paychecks, with hours, base pay, card tips, the expected check (before taxes), cash already in hand, the payday, and any minimum-wage top-up owed. The date filter on each page also gets "This pay period" and "Last pay period" once an employer is picked.
- **Paystubs**: attach a photo or PDF and enter the totals. Each stub is compared with the shifts logged for that pay period, and missing hours or wages are flagged.
- **Dashboard charts**: earnings by employer (wages / cash / card), cash vs. card tips over time, earnings per month by employer, and hourly earnings per week against minimum wage.
- **Minimum wage check**: for each workweek at each employer, base pay + all tips is compared with *hours × local minimum wage*. If a tipped job falls short, New Mexico law says the employer owes the difference, and the app shows by how much. It also flags a base rate below the local tipped cash minimum and weeks over 40 hours.
- **Accounts**: each person logs in with their own username and password and only ever sees their own employers, shifts, tips, paystubs and photos. Admins add people, reset passwords and keep the shared minimum wage rates current. Admins can't see anyone else's data either.
- **Export / backup**: each person can download their own data (CSVs + photos). Admins can download a whole-server backup.

## Run it

You need Docker with the Compose plugin.

```bash
cd tiptracker
cp .env.example .env     # then edit .env: pick your PORT and set APP_PASSWORD (the setup key)
docker compose up -d --build
```

Your settings live in `.env`, so copying in a newer version of the app never overwrites them.

Open `http://<server-ip>:8000` (or your port). The first visit asks you to **create the first account**, which becomes the admin. You'll need the `APP_PASSWORD` value from `.env` as the setup key. On a phone, use Share → **Add to Home Screen** in Safari, or in Chrome use ⋮ → **Add to Home screen**. It then opens like an app.

Data lives in `./data` next to `docker-compose.yml` (`tiptracker.db` + `uploads/`).

### Adding people

1. As an admin, open **Admin → Add a person**. Enter their name and a username, and keep or change the suggested temporary password.
2. Send them the address, their username and the temporary password privately.
3. The first time they log in they have to choose their own password.

On the Admin page you can also **reset a password** (it shows a new temporary one once), **make someone an admin**, **turn an account off** (they can't log in, but their data is kept), or **delete** an account along with all of its data. There's always at least one admin.

Forgotten passwords: there's no email reset. An admin resets it and passes on the new temporary password.

### Upgrading from the single-user version

Your data is kept. After `docker compose up -d --build`, the first visit shows **Create the first account** with a note that existing data will belong to that account. Use the **old `APP_PASSWORD` as the setup key**. Create the account for whoever entered that data first (it becomes the admin), then add any other people from the Admin page. Make them admins too if you like.

### Settings (in `.env`)

| Variable | Default | What it does |
|---|---|---|
| `APP_PASSWORD` | *(none)* | **Setup key.** Needed only to create the very first account, so a stranger who finds the server before you can't claim it. After that, everyone logs in with their own username and password. **Set this.** |
| `SECURE_COOKIES` | *(off)* | Set to `1` when the app is only reached over HTTPS (behind a reverse proxy), so login cookies are never sent unencrypted. |
| `PORT` | `8000` | Port on the server you open in the browser. |
| `TZ` | `America/Denver` | Used for "today" on new shifts. |
| `SECRET_KEY` | auto-generated in `/data/.secret_key` | Signs login cookies. |

### Reaching it away from home

The simplest safe option is **Tailscale** on the server and each person's phone: no ports opened, and it works anywhere. To let coworkers in without making them Tailscale users, put it behind a reverse proxy (Nginx Proxy Manager, Caddy, Traefik, or a Cloudflare Tunnel) with **HTTPS**, and set `SECURE_COOKIES=1`. Logins are rate-limited: 10 wrong passwords lock that username for 15 minutes.

### Updating

If the folder was set up with `git clone` (or linked to the repo):

```bash
bash /path/to/tiptracker/update.sh      # e.g. /mnt/user/appdata/tiptracker/update.sh on Unraid
```

It pulls the latest version from GitHub, shows what changed and rebuilds. It won't overwrite files you edited on the server. Otherwise copy in the new files and run `docker compose up -d --build`.

The database is created or upgraded automatically on start, and the `./data` folder is untouched.

### Backups

Admins: use **Settings → Download full backup** now and then, or back up the `./data` folder with whatever you already use. That folder holds everyone's records, so keep a copy off the server. To restore, stop the container, unzip the backup into `./data`, and start it again.

## Minimum wage rates

These rates were pre-loaded in September 2026. They're shared by every account, and admins edit them under **Settings**:

| Location | Min wage | Tipped cash wage | Notes |
|---|---|---|---|
| Albuquerque (city limits) 2026 | $12.00 | $7.20 | State $12 beats the city's $11.85; city's $7.20 tipped wage beats the state's $3 |
| Albuquerque 2027 | $13.00 | $7.20 | Ordinance O-26-33; check against the city's official notice (posted by Oct 15) |
| Bernalillo County (unincorporated) 2026 | $12.00 | $3.00 | County rate is below the state's, so state rules apply |
| New Mexico elsewhere 2026 | $12.00 | $3.00 | |

When a rate changes, add a new row with its start date. Older weeks keep using the rate that applied at the time. Pick the right location for each employer on the **Employers** page. Being inside Albuquerque city limits vs. unincorporated county changes the tipped minimum ($7.20 vs. $3.00).

The check is a record-keeping aid, not legal advice. A flagged week is a reason to look at that paystub (some employers add a "tip credit make-up" line) and, if it's still short, ask the employer.

## Development

```bash
pip install -r requirements.txt
python3 tests/test_multiuser.py /tmp/tt-test          # account isolation, sessions, admin, lockout
```

The test signs in as two people and tries to read and change the other's shifts, paystubs, employers and photos through every URL, and checks that admin-only actions are refused for members.

## Running without Docker

```bash
pip install -r requirements.txt
DATA_DIR=./data gunicorn -b 0.0.0.0:8000 app.main:app
```
