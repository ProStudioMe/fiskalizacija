# Sepko backup → OneDrive

Poseban Docker servis (`backup`) radi `pg_dump` (+ opciono uploads/certs) i piše u folder
koji OneDrive sinkronizuje na hostu.

## Destinacija

U `.env`:

```env
SEPKO_BACKUP_ONEDRIVE_DIR=E:/onedrive/SepkoBackups
SEPKO_BACKUP_RETENTION_DAYS=14
SEPKO_BACKUP_INTERVAL_HOURS=24
```

Ako nije setovano, koristi se `./backups` u repou (i to je OK ako je projekat već na OneDrive).

## Pokretanje

```powershell
# db + backup loop (svakih N sati)
docker compose -f docker-compose.yml -f docker-compose.backup.yml up -d db backup

# jedan dump odmah
docker compose -f docker-compose.yml -f docker-compose.backup.yml run --rm backup once
```

Produkcija:

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.backup.yml up -d
```

## Struktura dumpa

```text
SepkoBackups/
  2026-09-07_074500/
    sepko.dump          # pg_dump custom (-Fc)
    uploads.tar.gz      # opciono
    certs.tar.gz        # opciono (.p12/.pfx)
    MANIFEST.txt
    .complete
```

## Restore

```powershell
docker compose -f docker-compose.yml -f docker-compose.backup.yml run --rm --entrypoint /usr/local/bin/restore.sh backup /backup/2026-09-07_074500
```

## Windows Task Scheduler (bez loop kontejnera)

```powershell
.\deploy\backup\run-once.ps1
```

Zakaži dnevno u Task Scheduleru.

## Napomena

Dumpovi sadrže poslovne podatke (i eventualno cert fajlove) — ne commitaj `backups/` u git.
