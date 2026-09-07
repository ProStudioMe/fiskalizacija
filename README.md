# ProRačun

SaaS fiskalizacija CG · **proracun.me** · back-office (Jinja) + REST API · fiskal radi **Navira**.
Prostudio: [prostudio.me](https://prostudio.me)

## Faza 1

- PostgreSQL
- Login (session + CSRF + rate-limit)
- Artikli, izdaj račun, lista, podešavanja, print/PDF pregled
- API `POST /v1/invoices/fiscalize` (mock, EFI šifrarnik)

Dogovor: [`docs/SEPKO-PLAN.md`](docs/SEPKO-PLAN.md)  
UX (Hostinger shell): [`docs/reference/hostinger/`](docs/reference/hostinger/README.md)  
UX (VG tok): [`docs/reference/vg-efiskal/`](docs/reference/vg-efiskal/README.md)

## Start

```powershell
cd E:\onedrive\fiskalizacija
docker compose up -d db
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\seed.py
uvicorn sepko.main:app --reload --port 8000
```

- Back-office: http://localhost:8000/  
- Login: `admin@philia.me` / `sepko123`  
- API: http://localhost:8000/docs  

## Postgres URL

```
SEPKO_DATABASE_URL=postgresql+asyncpg://sepko:sepko@localhost:5433/sepko
```

(`postgresql+psycopg://` se i dalje prihvata.) Port **5433** da ne sudara lokalni Postgres na 5432.

## Produkcija

Server: **proracun.me** na Hetzneru (SSH user `proracun`, home `/home/proracun`). Detalji: [`deploy/README.md`](deploy/README.md).

Gunicorn s Uvicorn workerima iza Nginx-a (ne gol Uvicorn):

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Konfig: `deploy/gunicorn.conf.py`, `deploy/nginx.conf`.
Image: `ghcr.io/prostudiome/fiskalizacija:latest`.

## Backup → OneDrive

Poseban servis (nije dio app-a): vidi [`deploy/backup/README.md`](deploy/backup/README.md).

```powershell
# u .env: SEPKO_BACKUP_ONEDRIVE_DIR=E:/onedrive/SepkoBackups
docker compose -f docker-compose.yml -f docker-compose.backup.yml up -d db backup
```

Lokalni ESC/POS agent na kasi: `python -m sepko.print_agent`
