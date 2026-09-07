# Produkcija — proracun.me

Hetzner / Virtualmin nalog **proracun**. SSH ključevi nisu u gitu (OneDrive `hetzner/proracun/ssh`).

## Pristup

| | |
|---|---|
| Host (Tailscale) | `prostudio` → `100.106.175.89` |
| Javni IP | `167.235.33.108` |
| User | `proracun` |
| Port | `22` (SFTP/SSH) |
| Home | `/home/proracun` |
| Domen | `proracun.me`, `www.proracun.me` |
| App (localhost) | `127.0.0.1:3040` → kontejner `:8000` |

PuTTY ključ (lokalno): `E:\onedrive\hetzner\proracun\ssh\id_rsa_private.ppk`

```powershell
& "C:\Program Files\PuTTY\plink.exe" -batch -i "E:\onedrive\hetzner\proracun\ssh\id_rsa_private.ppk" proracun@prostudio
```

Image: `ghcr.io/prostudiome/fiskalizacija:latest` (push na `main` pokreće `.github/workflows/ghcr.yml`).

Nalog je Virtualmin vhost; `proracun` **ne može** Docker API dok nije u grupi `docker`. Systemd unit i nginx proxy se instaliraju **jednom kao root**.

## Fajlovi na serveru

| Remote | Izvor u gitu |
|--------|----------------|
| `/home/proracun/docker-compose.yml` | `deploy/docker-compose.server.yml` |
| `/home/proracun/docker-proracun.service` | `deploy/docker-proracun.service` |
| `/home/proracun/nginx-proracun.me.conf` | `deploy/nginx-proracun.me.conf` |
| `/home/proracun/install-server.sh` | `deploy/install-server.sh` |
| `/home/proracun/.docker_env` | `deploy/env.server.example` (samo na serveru; `TOKEN` = GHCR PAT) |

## Podizanje (root, jednom)

```bash
# kao root na prostudio
chmod +x /home/proracun/install-server.sh
/home/proracun/install-server.sh
```

Isto ručno:

```bash
install -d -o proracun -g proracun /home/proracun/logs /home/proracun/data /home/proracun/certs
cp /home/proracun/docker-proracun.service /etc/systemd/system/docker-proracun.service
cp /home/proracun/nginx-proracun.me.conf /etc/nginx/sites-enabled/proracun.me.conf
nginx -t && systemctl reload nginx
systemctl daemon-reload
systemctl enable --now docker-proracun
systemctl status docker-proracun --no-pager
```

Provjera: `curl -sI -H 'Host: proracun.me' http://127.0.0.1:3040/`

Poslije toga: `systemctl restart docker-proracun` (novi GHCR `latest`).
