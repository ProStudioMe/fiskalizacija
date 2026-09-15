# EXTFISK — HTTP API (RegisterInvoiceRequest XML)

Izvor: `EXTFISK_uputstvo.docx`. Sepko šalje XML na ovaj servis; CIS/Poreska uprava dolazi u drugom dijelu.

## Poziv

| Polje | Vrijednost |
|-------|------------|
| Metoda | `POST` |
| URL | `http://62.4.59.86:3366/api/extfisk` |
| Content-Type | `application/xml` (ili `text/xml`) |
| Body | Sirovi XML `RegisterInvoiceRequest` |

API key **nije** HTTP header — stoji u XML-u, u `<ApiKey>`. Ključ mora odgovarati PIB-u prodavca (`Seller/IDNum`).

**Multi-tenant (~1000 firmi):** ApiKey je **po tenantu** (Admin → Tenanti → EFI / EXTFISK), enkriptovan u `settings_json`. Nije jedan globalni ključ u `.env`. `SEPKO_EXTFISK_URL` i `SEPKO_EXTFISK_ENVIRONMENT` ostaju zajednički; `SEPKO_EXTFISK_API_KEY` je samo opcioni fallback za single-tenant / lokalni test.

## Zaglavlje

| Polje | Obavezno | Napomena |
|-------|----------|----------|
| `ApiKey` | da | Identifikuje firmu (po PIB-u) |
| `Environment` | da | `TEST` ili `PROD` |
| `RequestId` | da | Isti ID → isti JIKR (ne fiskalizuje ponovo) |
| `InvType` | da | Trenutno podržan tok: `RACUN` (`POVRAT` / `AVANS` / … kasnije) |

Ako prethodni pokušaj padne na validaciji, ponovni POST istog `RequestId` sa ispravljenim XML-om **nastavlja** proces, ne kreira novi zahtjev.

## Odgovor

Uvijek HTTP 200 + JSON, i kad je fiskalizacija poslovno odbijena. HTTP 500 samo za tehničke greške (npr. baza).

Uspjeh:

```json
{
  "status": "OK",
  "code": "1042",
  "poruka": "Racun uspjesno fiskalizovan",
  "requestId": "1000001",
  "idReq": 1042,
  "god": "2026",
  "idDok": 88210,
  "jikr": "TEST"
}
```

**JIKR:** dok nije spreman drugi dio (putanja ka Poreskoj upravi), vrijednost je uvijek `TEST`. To je uspjeh. Kasnije JIKR postaje pravi identifikator PU.

## Validacija iznosa

Zbir stavki = zaglavlje (`TotPriceWoVAT` / `TotVATAmt` / `TotPrice`). Zbir `PayMethod/Amt` = `TotPrice`.

`PayMethod/Type`: `BANKNOTE`, `CARD`, `ACCOUNT`, `BUSINESSCARD`, `OTHER`, `ADVANCE`, `SVOUCHER`.

EFI `ORDER` (virman) se šalje kao `ACCOUNT`.

## Datumi (Oracle)

EXTFISK parsira datume preko Oracle — **ne** šalji offset (`+00:00` / `Z`), inače `ORA-01841`.  
`dd.MM.yyyy` daje `ORA-01861`.

| Polje | Format | Primjer |
|-------|--------|---------|
| `IssueDateTime` | `yyyy-MM-ddTHH:mm:ss` | `2026-09-15T14:30:00` |
| `PayDeadline` | `yyyy-MM-dd` | `2026-09-30` |
| `TaxPeriod` | `MM/yyyy` | `09/2026` |

## Implementacija (Sepko)

- Adapter: `sepko/extfisk.py` (`SEPKO_PARTNER_MODE=extfisk`)
- URL: `SEPKO_EXTFISK_URL` (default `:3366`)
- ApiKey: po firmi u adminu → XML `<ApiKey>`
- `jikr=TEST` = uspješna fiskalizacija
- Isti `RequestId` (hash PIB+InvNum) pri retry-u offline računa
