# Jednokratni Sepko backup → OneDrive (Docker).
# Zakaži u Task Scheduleru ili pokreni ručno.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$compose = @(
  "-f", "docker-compose.yml",
  "-f", "docker-compose.backup.yml"
)

# osiguraj da db radi
docker compose @compose up -d db | Out-Null
docker compose @compose run --rm backup once
if ($LASTEXITCODE -ne 0) {
  throw "Backup failed with exit $LASTEXITCODE"
}
Write-Host "Backup OK → $(if ($env:SEPKO_BACKUP_ONEDRIVE_DIR) { $env:SEPKO_BACKUP_ONEDRIVE_DIR } else { '.\backups' })"
