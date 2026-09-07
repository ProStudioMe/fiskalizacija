#!/bin/bash
# Popuni /home/proracun/.docker_env bez ispisivanja tajni. Pokreni kao proracun.
set -euo pipefail
umask 077
ENV=/home/proracun/.docker_env
touch "$ENV"
chmod 600 "$ENV"
if [ -s "$ENV" ]; then
  last=$(tail -c 1 "$ENV" || true)
  if [ "$last" != $'\n' ]; then
    printf '\n' >> "$ENV"
  fi
fi

add_if_missing() {
  local key="$1"
  local val="$2"
  if ! grep -q "^${key}=" "$ENV" 2>/dev/null; then
    printf '%s=%s\n' "$key" "$val" >> "$ENV"
  fi
}

add_if_missing POSTGRES_PASSWORD "$(openssl rand -hex 16)"
add_if_missing SEPKO_SECRET_KEY "$(openssl rand -hex 32)"
add_if_missing SEPKO_SUPERADMIN_PASSWORD "$(openssl rand -hex 12)"
add_if_missing SEPKO_ALLOWED_HOSTS "proracun.me,www.proracun.me"
add_if_missing SEPKO_PARTNER_MODE "mock"
add_if_missing SEPKO_CERT_DIR "/certs"
add_if_missing SEPKO_BILLING_NAME "PROSTUDIO.ME DOO"
add_if_missing SEPKO_BILLING_EMAIL "finansije@prostudio.me"
add_if_missing SEPKO_BILLING_PIB "03452668"
add_if_missing SEPKO_BILLING_ADDRESS "Bulevar 21. maj 24, Podgorica, Crna Gora"
add_if_missing SEPKO_BILLING_PHONE "+382 67 254 256"
add_if_missing SEPKO_LICENSE_MONTHLY_PRICE "15"
add_if_missing SEPKO_LICENSE_ITEM_NAME "ProRačun Basic WEB"

# TOKEN ostaje ako već postoji (GHCR). Ne diraj postojeće SEPKO_SECRET_KEY.
echo "OK keys: $(sed -n 's/=.*//p' "$ENV" | tr '\n' ' ')"
