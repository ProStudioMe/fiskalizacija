#!/bin/bash
# Pokreni kao root na Hetzneru (prostudio).
set -euo pipefail
install -d -o proracun -g proracun /home/proracun/logs /home/proracun/data /home/proracun/certs
chmod 600 /home/proracun/.docker_env 2>/dev/null || true
cp /home/proracun/docker-proracun.service /etc/systemd/system/docker-proracun.service
cp /home/proracun/nginx-proracun.me.conf /etc/nginx/sites-enabled/proracun.me.conf
nginx -t
systemctl reload nginx
systemctl daemon-reload
systemctl enable --now docker-proracun
systemctl status docker-proracun --no-pager
