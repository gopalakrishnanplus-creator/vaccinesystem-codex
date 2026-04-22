# Vaccine System

Local test setup for this repository uses SQLite instead of the production MySQL topology.

## Quick start

```bash
./bootstrap_local.sh
```

That script will:

- create `.venv311`
- install Python dependencies
- run migrations with `vaccination_project.settings_local`
- load the bundled IAP schedule from `iap_final.csv`
- import UI translations from `phase5_ui_translations.csv`

## Run locally

```bash
source .venv311/bin/activate
DJANGO_SETTINGS_MODULE=vaccination_project.settings_local python manage.py runserver
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Notes

- The local setup uses `local_test.sqlite3`.
- All database aliases (`default`, `masters`, `patients`) point to the same SQLite file for testing.
- Google OAuth values in `.env.test` are placeholders; OAuth login will need real credentials before that flow can be tested.
- Automatic WhatsApp sending is disabled in local settings.

## Server Deployment

The repo includes a production-oriented one-shot deploy script at [deploy.sh](/Users/inditech-tech/Documents/Vaccine/vaccinesystem-codex/deploy.sh).

Expected production paths:

- app repo: `/var/www/vaccinesystem`
- Python venv: `/var/www/venv`
- env file: `/var/www/secrets/.env`

Server deploy flow:

```bash
cd /var/www/vaccinesystem
chmod +x deploy.sh
./deploy.sh
```

What the script handles:

- installs required Ubuntu/Debian packages when `apt-get` is available
- uses `/var/www/venv`
- installs Python packages from `requirements.txt`
- validates required env keys from `/var/www/secrets/.env`
- runs migrations for `default`, `masters`, and `patients`
- bootstraps schedule data only when the clinic DB is empty
- imports UI translations and education workbook when those files exist
- collects static files
- writes or refreshes Gunicorn and Nginx config
- restarts `gunicorn-vaccine` and Nginx

Optional HTTPS setup with Let's Encrypt:

```bash
cd /var/www/vaccinesystem
ENABLE_HTTPS=1 LETSENCRYPT_EMAIL=you@example.com ./deploy.sh
```

That mode will:

- install `certbot` and `python3-certbot-nginx`
- request or reuse a certificate for `newvaccine.cpdinclinic.co.in`
- update Nginx to redirect HTTP to HTTPS
- enable the `certbot.timer` renewal job when available
