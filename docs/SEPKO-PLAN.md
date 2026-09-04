# Sepko — plan (OneDrive)

Radi sync na drugi računar: sve bitne odluke žive ovdje, ne samo u Cursor chatu.
**Pravilo:** agent uvijek čuva u `E:\onedrive\fiskalizacija\` (bez posebnog „sačuvaj”).

## Proizvod

- **Sepko** — SaaS fiskalizacija CG, cilj **~15 €/mjesec**
- **Vi:** frontend + pretplata + back-office + **platform admin**
- **Navira:** prava fiskalizacija (JIKR/IKOF/QR)
- **Prostudio:** ekosistem / kanal
- UX referenca tok: [VG eFiskal](https://pos.fiskalizacija.me/) → [`docs/reference/vg-efiskal/`](reference/vg-efiskal/README.md)
- **UI shell:** inspiracija Hostinger hPanel → [`docs/reference/hostinger/`](reference/hostinger/README.md)
- Brand Sepko ostaje svoj (ne klon Hostinger/VG)
## Arhitektura (dva portala)

```mermaid
flowchart TB
  subgraph platform [PlatformAdmin]
    SuperAdmin[superadmin]
    Tenants[tenants_licence_ENU]
    I18n[prevodi_jezici]
  end
  subgraph tenantUI [TenantBackoffice]
    Kasir[admin_kasir]
    Artikli[artikli_kupci]
    Racuni[racuni_blagajna]
  end
  subgraph fiskal [Fiskal]
    Navira[Navira]
    EFI[EFI_CIS]
  end
  SuperAdmin --> Tenants
  SuperAdmin --> I18n
  Tenants --> Kasir
  Kasir --> Artikli
  Kasir --> Racuni
  Racuni --> Navira --> EFI
```
| Dio | Ko | Tehnologija |
|-----|----|-------------|
| Tenant back-office | klijent (firma) | FastAPI + Jinja2 |
| **Platform admin** | ti / Prostudio | FastAPI + Jinja2 (`/admin`) — odvojeno od tenanta |
| POS | Faza 2 | Vue/Svelte/Flutter preko REST |
| Fiskal | Navira | mock → HTTP adapter |

Interni ugovor = **EFI v5** ([docs/efi/](efi/README.md)). SOAP/XAdES radi Navira.
`SEPKO_PARTNER_MODE=mock` ostaje default. Za sandbox: `navira` + `SEPKO_NAVIRA_BASE_URL` + `SEPKO_NAVIRA_API_KEY`.
Endpointi: `/v1/register-invoice`, `/v1/register-cash-deposit` (EFI-shaped JSON).

**Multi-tenant pravilo:** svaki upit filtriran `tenant_id`. Platform admin je uloga `superadmin` (nije tenant user). Cilj: **~1000 tenanata**.

## Faza 1 (gotovo) — Tenant back-office

1. Login (session HttpOnly + CSRF + rate-limit)
2. Dashboard / promet
3. Artikli CRUD
4. Izdaj račun + lista + pretraga
5. Podešavanja firme + EFI kodovi (ENU, poslovni prostor, operator, softver)
6. HTML/print pregled računa (IKOF/JIKR/QR u EFI formatu)
7. REST API `POST /v1/invoices/fiscalize` (mock → Navira)
8. Automatski InvNum `{PJ}/{rbr}/{godina}/{ENU}`
9. Blagajna: INITIAL / WITHDRAW + pregled gotovine
10. Šifarnik kupaca + izbor na računu

## Faza 1c — Mini knjigovodstvo + PWA (CG, 2026)

Paralelno sa Navira wire:

1. **Navira `HttpPartnerAdapter`** — `POST {base}/v1/register-invoice` i `/v1/register-cash-deposit`, Bearer/X-Api-Key, retry 5xx; mock ostaje default (`SEPKO_PARTNER_MODE=mock`)
2. **Ulazne fakture** — `Supplier`, `IncomingInvoice` (+ stavke); UI `/ulazne`, `/ulazne/qr`, `/dobavljaci`
3. **QR import** — parse `tax.gov.me` / `efitest` / `mapr` verify URL → predpopuna; kamera (`BarcodeDetector` + jsQR)
4. **Troškovnik** — kategorije + `/troskovi`; kreiranje troška iz ulazne
5. **PWA** — `manifest.webmanifest`, `/sw.js`, `/app` shell, brza fiskalizacija `/app/brzo`
6. **REST** — `/v1/incoming-invoices`, `/from-qr`, `/v1/expenses`, `/v1/dashboard/summary`
7. **Bank match** — parse izvoda (tekst/PDF rough) + UI uparivanje na ulaznu/trošak
8. **Izvještaji** — `/izvjestaji/pregled` (izlaz/ulaz/trošak/PDV saldo indikativno)

Namjerno nije u ovom ciklusu: regionalni e-račun (SEF/Peppol), offline fiskalizacija queue, puni lager/plate.

## Faza 1b — Platform admin (sljedeće, za 1000 klijenata)

Ruta `/admin` (odvojen login, npr. `super@sepko.me`):

1. Lista tenanata (pretraga PIB/naziv, status trial/active/suspended)
2. Kreiraj / suspenduj tenant + prvi admin user
3. Licenca: tip, od–do, upozorenje &lt; 30 dana (kao VG eFiskal)
4. Pregled EFI kodova po tenantu (PJ, ENU, soft, operator) — bez cert fajla u UI
5. Statistika: broj računa / greške / zadnji promet
6. Audit log (ko je kreirao tenanta, promjene statusa)
7. **Prevodi (i18n)** — centralno, za sve tenante / POS:
   - Jezici (start: `cnr` lat, `cnr` ćir, `sr` lat/ćir, `en`; kasnije AL/TR/RU/UK kao VG)
   - Ključevi UI stringova (`nav.invoices`, `invoice.status.fiscalized`, …)
   - CRUD / import-export JSON ili CSV po jeziku
   - Tenant bira podrazumijevani jezik; korisnik može override u sessionu
   - Fallback: nedostajući ključ → `cnr` lat → `en`

Ne miješati: tenant admin **ne vidi** druge firme i **ne edituje** platform prevode. Superadmin **ne izdaje** račune umjesto klijenta (osim support impersonation kasnije, eksplicitno).

## Faza 2 — POS

- Klijent u bilo kojoj tehnologiji
- Offline queue, printer, bar-kod
- Isti REST API
- UX inspiracija: fakture lista, modal komitenti/artikli, draft→fiskalizovan

## Sigurnost

- Session cookie: **HttpOnly** (Starlette), **Secure** u produkciji, SameSite=Lax — nema JWT u LocalStorage
- CSRF token na svakoj HTML formi + Origin/Referer check u produkciji
- Rate limit `/login` i `/admin/login` (app 5/min + Nginx `limit_req`)
- Stroga Pydantic validacija (dužine, predznak cijena; negativno samo CREDIT_NOTE/CORRECTIVE)
- `audit_log`: korisnik, IP, vrijeme — račun, cijene, blagajna (`/dnevnik`)
- App Postgres user `sepko_app` bez DROP/CREATE (owner `sepko` samo za migracije)
- ORM + allowlist za ALTER; tajne u `.env` (gitignored); IMAP/SMTP lozinke Fernet u bazi
- Produkcija: Nginx TLS (`deploy/nginx-ssl.conf`), HSTS

## Baza

**PostgreSQL** (Docker):

```powershell
cd E:\onedrive\fiskalizacija
docker compose up -d db
```

Connection (default): `postgresql+asyncpg://sepko:sepko@localhost:5433/sepko`  
(`postgresql+psycopg://` i dalje radi — sync URL se izvodi za seed/init_db.)  
(Docker mapira **5433→5432** da ne sudara postojeći Postgres.)

FastAPI: `get_async_db()` + asyncpg da se ne blokira event loop. HTML `def` rute koriste sync Session u threadpoolu.

## Pokretanje

```powershell
cd E:\onedrive\fiskalizacija
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\seed.py
uvicorn sepko.main:app --reload --port 8000
```

Back-office: http://localhost:8000/  
API docs: http://localhost:8000/docs  

Demo login: `admin@philia.me` / `sepko123`

## Produkcija

Uvicorn sam nije primarni server. Linux/Docker:

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Gunicorn + `uvicorn.workers.UvicornWorker` iza Nginx-a (`deploy/`).

## Štampa i sertifikati

- A4: `/racuni/{id}/pdf` → `window.print()` + `@media print`
- Termal: `/racuni/{id}/stampa` + lokalni agent `python -m sepko.print_agent` (WebSocket `ws://127.0.0.1:17890/ws`)
- PKCS#12: `SEPKO_CERT_DIR/{slug}.p12` — IKOF potpis u mock/Navira payloadu (`iicSignature`)
