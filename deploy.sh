#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/var/www/vaccinesystem}"
VENV_DIR="${VENV_DIR:-/var/www/venv}"
ENV_FILE="${ENV_FILE:-/var/www/secrets/.env}"
BRANCH="${BRANCH:-main}"
APP_HOST="${APP_HOST:-newvaccine.cpdinclinic.co.in}"
APP_PORT="${APP_PORT:-8000}"
NGINX_SITE="${NGINX_SITE:-vaccinesystem}"
GUNICORN_SERVICE="${GUNICORN_SERVICE:-gunicorn-vaccine}"
DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-vaccination_project.settings}"
ENABLE_HTTPS="${ENABLE_HTTPS:-0}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_file() {
  local path="$1"
  [ -f "$path" ] || fail "Required file not found: $path"
}

run_manage() {
  DJANGO_SETTINGS_MODULE="$DJANGO_SETTINGS_MODULE" "$PYTHON_BIN" manage.py "$@"
}

db_scalar() {
  local database="$1"
  DJANGO_SETTINGS_MODULE="$DJANGO_SETTINGS_MODULE" "$PYTHON_BIN" manage.py shell -c \
    "from vaccinations.models import ScheduleVersion; print(ScheduleVersion.objects.using('${database}').count())"
}

install_os_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing OS packages needed for Django, Gunicorn, MySQL client builds, and Nginx"
    $SUDO apt-get update -y
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y \
      git \
      nginx \
      certbot \
      python3-certbot-nginx \
      python3 \
      python3-dev \
      python3-venv \
      build-essential \
      pkg-config \
      default-libmysqlclient-dev \
      libjpeg-dev \
      zlib1g-dev
  else
    log "apt-get not found; skipping OS package installation. Make sure Python, Nginx, and MySQL build deps already exist."
  fi
}

ensure_repo() {
  [ -d "$APP_DIR" ] || fail "App directory does not exist: $APP_DIR"
  cd "$APP_DIR"
  require_file "$APP_DIR/manage.py"

  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "Updating repository from origin/${BRANCH}"
    git fetch origin "$BRANCH"
    git pull --ff-only origin "$BRANCH"
  else
    log "Directory is not a git checkout; skipping git pull and using current files"
  fi
}

ensure_venv() {
  if [ ! -x "$PYTHON_BIN" ]; then
    log "Virtualenv not found at $VENV_DIR; creating it"
    python3 -m venv "$VENV_DIR"
  fi

  log "Installing Python dependencies into $VENV_DIR"
  "$PIP_BIN" install --upgrade pip setuptools wheel
  "$PIP_BIN" install -r requirements.txt
}

validate_env_file() {
  require_file "$ENV_FILE"

  log "Validating required environment keys in $ENV_FILE"
  "$PYTHON_BIN" - <<PY
from pathlib import Path
from dotenv import dotenv_values
import sys

env_file = Path("${ENV_FILE}")
values = dotenv_values(env_file)
required = [
    "PATIENT_DATA_FERNET_KEY",
    "CLINIC_DB_NAME",
    "CLINIC_DB_USER",
    "CLINIC_DB_PASSWORD",
    "CLINIC_DB_HOST",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "MASTERS_DB_NAME",
    "MASTERS_DB_USER",
    "MASTERS_DB_PASSWORD",
    "MASTERS_DB_HOST",
    "PATIENTS_DB_NAME",
    "PATIENTS_DB_USER",
    "PATIENTS_DB_PASSWORD",
    "PATIENTS_DB_HOST",
    "PHONE_HASH_SALT",
    "DATA_KEY_ACTIVE",
    "DATA_KEY_1",
    "SEARCH_PEPPER",
]
missing = [key for key in required if not values.get(key)]
if missing:
    print("Missing required env keys:", ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print("Environment file looks complete.")
PY
}

prepare_runtime_dirs() {
  log "Preparing runtime directories"
  mkdir -p "$APP_DIR/staticfiles" "$APP_DIR/media"
}

write_gunicorn_service() {
  local app_user app_group
  app_user="${APP_USER:-$(stat -c '%U' "$APP_DIR")}"
  app_group="${APP_GROUP:-$(stat -c '%G' "$APP_DIR")}"

  log "Writing systemd service ${GUNICORN_SERVICE}"
  $SUDO tee "/etc/systemd/system/${GUNICORN_SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=Gunicorn for Vaccine System
After=network.target

[Service]
User=${app_user}
Group=${app_group}
WorkingDirectory=${APP_DIR}
Environment=DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}
ExecStart=${VENV_DIR}/bin/gunicorn --workers 3 --bind 127.0.0.1:${APP_PORT} --timeout 120 vaccination_project.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "${GUNICORN_SERVICE}"
}

write_nginx_site() {
  if [ ! -d /etc/nginx/sites-available ]; then
    log "Nginx site directory not found; skipping Nginx config write"
    return
  fi

  log "Writing Nginx site ${NGINX_SITE}"
  $SUDO tee "/etc/nginx/sites-available/${NGINX_SITE}" >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${APP_HOST};

    client_max_body_size 25M;

    location /static/ {
        alias ${APP_DIR}/staticfiles/;
        access_log off;
        expires 7d;
    }

    location /media/ {
        alias ${APP_DIR}/media/;
        access_log off;
        expires 1d;
    }

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
    }
}
EOF

  $SUDO ln -sfn "/etc/nginx/sites-available/${NGINX_SITE}" "/etc/nginx/sites-enabled/${NGINX_SITE}"
  if [ -f /etc/nginx/sites-enabled/default ]; then
    $SUDO rm -f /etc/nginx/sites-enabled/default
  fi
  $SUDO nginx -t
  $SUDO systemctl enable nginx
}

migrate_all_databases() {
  log "Running migrations on default"
  run_manage migrate --noinput --database=default

  log "Running migrations on masters"
  run_manage migrate --noinput --database=masters

  log "Running migrations on patients"
  run_manage migrate --noinput --database=patients
}

bootstrap_reference_data_if_needed() {
  local default_schedule_count masters_schedule_count

  default_schedule_count="$(db_scalar default | tr -d '[:space:]')"
  masters_schedule_count="$(db_scalar masters | tr -d '[:space:]')"

  if [ "$default_schedule_count" = "0" ]; then
    require_file "$APP_DIR/iap_final.csv"
    log "No clinic schedule found in default; loading iap_final.csv"
    run_manage load_final_iap_schedule --csv-file iap_final.csv
    default_schedule_count="$(db_scalar default | tr -d '[:space:]')"
  else
    log "Default clinic schedule already present; skipping destructive schedule bootstrap"
  fi

  if [ "$masters_schedule_count" = "0" ] && [ "$default_schedule_count" != "0" ]; then
    log "Masters schedule missing; copying clinic schedule into masters"
    run_manage copy_vaccines_to_masters
  else
    log "Masters schedule already present; skipping destructive copy to masters"
  fi

  if [ -f "$APP_DIR/phase5_ui_translations.csv" ]; then
    log "Importing UI translations"
    run_manage import_ui_translations phase5_ui_translations.csv
  else
    log "phase5_ui_translations.csv not found; skipping UI translation import"
  fi

  masters_schedule_count="$(db_scalar masters | tr -d '[:space:]')"
  if [ -f "$APP_DIR/education.xlsx" ] && [ "$masters_schedule_count" != "0" ]; then
    log "Importing education workbook"
    run_manage import_education education.xlsx
  else
    log "Skipping education import because education.xlsx or masters schedule is missing"
  fi
}

run_checks_and_collectstatic() {
  log "Running Django system check"
  run_manage check

  log "Collecting static files"
  run_manage collectstatic --noinput
}

restart_services() {
  log "Restarting ${GUNICORN_SERVICE}"
  $SUDO systemctl restart "${GUNICORN_SERVICE}"
  $SUDO systemctl status "${GUNICORN_SERVICE}" --no-pager --lines=20

  if command -v nginx >/dev/null 2>&1; then
    log "Reloading Nginx"
    $SUDO systemctl restart nginx
    $SUDO systemctl status nginx --no-pager --lines=20
  fi
}

configure_https_if_requested() {
  if [ "${ENABLE_HTTPS}" != "1" ]; then
    log "HTTPS certificate setup disabled (set ENABLE_HTTPS=1 and LETSENCRYPT_EMAIL=you@example.com to enable)"
    return
  fi

  [ -n "${LETSENCRYPT_EMAIL}" ] || fail "ENABLE_HTTPS=1 requires LETSENCRYPT_EMAIL to be set"

  if ! command -v certbot >/dev/null 2>&1; then
    fail "certbot is not installed; rerun on Ubuntu/Debian with apt-get available or install certbot manually"
  fi

  log "Requesting or refreshing Let's Encrypt certificate for ${APP_HOST}"
  $SUDO systemctl start nginx
  $SUDO certbot --nginx \
    --non-interactive \
    --agree-tos \
    --redirect \
    --keep-until-expiring \
    -m "${LETSENCRYPT_EMAIL}" \
    -d "${APP_HOST}"

  if $SUDO systemctl list-unit-files | grep -q '^certbot.timer'; then
    log "Ensuring certbot renewal timer is enabled"
    $SUDO systemctl enable --now certbot.timer
  fi
}

main() {
  log "Starting Vaccine System deployment"
  install_os_packages
  ensure_repo
  ensure_venv
  validate_env_file
  prepare_runtime_dirs
  write_gunicorn_service
  write_nginx_site
  migrate_all_databases
  bootstrap_reference_data_if_needed
  run_checks_and_collectstatic
  restart_services
  configure_https_if_requested
  log "Deployment completed successfully"
}

main "$@"
