# EFI dokumentacija (Crna Gora)

Primarni izvor: `docs/Fiskalizacija_uputstvo.zip` (raspakovano ovdje).
Dopuna: [Elektronska fiskalizacija — Poreska uprava](https://www.gov.me/poreskauprava/elektronska-fiskalizacija)

Interni ugovor je EFI v5. **Kanal slanja bira se po tenantu** (Admin → EFI → Fiskalni kanal):

- **Navira** — HTTP JSON (`HttpPartnerAdapter`)
- **EXTFISK** — HTTP XML (`EXTFISK.md`)
- **Poreska uprava (CIS)** — poseban modul `sepko/pu/` (IKOF + XMLDSig + SOAP). Vidi [PU-CIS.md](PU-CIS.md).
- prazno — nasljeđuje `SEPKO_PARTNER_MODE`

## Fajlovi

| Fajl | Šta koristiti |
|------|----------------|
| `1_1 - Fiskalni servis - Funkcionalna specifikacija v5(komplet).docx` | Pojmovi, tipovi računa, poslovna pravila |
| `1_2 - Fiskalni servis - Tehnicka specifikacija v5 (komplet).docx` | Metode, IKOF, QR URL |
| `1 - Fiskalni servis - Uputstva za pristup fiskalizaciji.docx` | Test/prod, certifikat, SEP |
| `1 - Tehnicka specifikacija - Prilog 2 (XML struktura i provjere).xlsx` | Šifrarnik polja i validacije |
| `2 Dodatak/Funkcionalna specifikacija - Prilog 1.docx` | Primjeri |
| `2 Dodatak/Funkcionalna specifikacija - Prilog 2 (Izvjestaji).docx` | Izvještaji |
| `2 Dodatak/Tehnicka specifikacija - Prilog 1 (Izracuni) (v3).docx` | PDV/iznosi |
| `3 XML Primjeri/` | Uzorci poruka (Advance, Credit_note, …) |
| `EN (old)/` | Arhiva v4.1/v4.2 |

Mapa polja: [EFI-NAVIRA-MAP.md](EFI-NAVIRA-MAP.md)  
EXTFISK HTTP XML: [EXTFISK.md](EXTFISK.md)  
Poreska CIS SOAP: [PU-CIS.md](PU-CIS.md)

## Endpointi (referenca, ne klijent)

- Test SEP: https://efitest.tax.gov.me/self-care
- Produkcija SEP: https://sep.tax.gov.me/self-care
- Test WSDL: https://efitest.tax.gov.me/fs-v1/FiscalizationService.wsdl
- Produkcija WSDL: https://efi.tax.gov.me/fs-v1/FiscalizationService.wsdl
- QR test: https://efitest.tax.gov.me/ic/#/verify
- QR produkcija: https://mapr.tax.gov.me/ic/#/verify
