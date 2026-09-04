# Referenca: VG eFiskal POS

Izvor: [https://pos.fiskalizacija.me/](https://pos.fiskalizacija.me/)  
Snimci: `docs/reference/vg-efiskal/*.png` (17 screenshotova, 28.08.2026)  
Napomena: ovo je **UX/produkt referenca**, ne kopija koda. Sepko ima svoj UI i Navira kao fiskal partner.

Primjer tenant iz snimaka: **PROSTUDIO.ME DOO** (PIB 03452668), Cloud POS, produkcija.

## Šta radi aplikacija (mapa ekrana)

| Modul | Šta vidi korisnik | Sepko paritet |
|-------|-------------------|---------------|
| **Informacije** | Profil, odjava, PIN, upozorenje licence, verzija, licenca, sertifikat, PJ/ENU, uređaj | `/informacije` (profil + EFI kodovi) |
| **Fakture** | Lista, filter datuma, status draft/fiskalizovan, PDF/XLSX, pregled | Filter datum/status + VG kolone |
| **Izmijeni fakturu** | Draft → stavke → naplata → fiskalizacija | Modal komitenti/artikli, popust, tip plaćanja |
| **Komitenti** | Modal šifarnik + pretraga + dodaj | Modal na računu + `/kupci` |
| **Artikli** | Modal šifarnik, VP cijena, PDV % | Modal na računu + `/artikli` |
| **Dodaj stavku** | Pretraga, JM, kol, VP/MP, popust, PDV, opis | Tabela stavki + modal |
| **Tip plaćanja** | Poslovna kartica, Virman, Avans, Drugo bezgotovinsko | Mapirano na EFI PayMethod |
| **Popust na račun** | Brzi % (5–75) | Modal popust |
| **Poreske stope** | PDV 0% oslobođenja (čl. 17, 20, 25…) | Nema još (ExemptFromVAT) |
| **Izvještaji** | Periodični, računi, operateri, komitenti, artikli, kategorije, rekap po danima, transakcije, avans, lager | `/izvjestaji` (stub + linkovi) |
| **Podešavanja** | Osnovna, štampa, artikli, komitenti, ostalo | Podešavanja + šifrarnici |
| **Račun pregled** | QR, PIB, stavke, štampa / e-mail / sačuvaj / detalji | PDF pregled |
| **Jezici** | CG lat/ćir, SR, AL, TR, RU, UK, EN | Platform admin → Prevodi (Faza 1b) |

## Broj računa (njihov UI)

Na fakturi: `1-1-164/2026` / `1-1-49/2026` — lokalni format `{serija}-{serija}-{rbr}/{godina}`.

EFI InvNum (CIS) ostaje: `{BusinUnitCode}/{InvOrdNum}/{year}/{TCRCode}`  
(npr. iz Informacije: PJ `gc968bp686`, ENU `un506nt589`).

Sepko već generiše EFI InvNum; interni „lijepi“ broj može biti paralelno kasnije.

## Podaci koje platform-admin mora vidjeti po klijentu

Iz ekrana **Informacije** (produkcijski Cloud POS):

- Firma / PIB / PDV broj
- Licenca (tip, od–do, upozorenje &lt; 30 dana)
- Sertifikat (vlasnik, CA, valjanost, okruženje test/prod)
- Kod poslovne jedinice + kod ENU
- Uređaj (naziv, model Cloud POS, serijski broj, verzija softvera)
- Operateri (ko fakturiše)

## UX obrasci za Sepko (šta uzeti, šta ne)

**Dizajn — prilagoditi (ne klonirati brand):**
- Uska ikona-sidebar + crvena panel traka sa naslovom stranice
- Bijeli content, lista sa chevronima (podešavanja)
- Status: zeleno „Fiskalizovan“ / narandžasto „U pripremi“
- Crveni accent dugmad; avatar inicijali na dnu sidebara
- Sepko brand (slovo S), ne ladybug / VG boje 1:1

**Funkcionalno uzeti:**
- Draft vs Fiskalizovan status jasno u listi
- Komitenti / artikli kao modal sa pretragom, ne samo dropdown
- Filter datuma + export PDF/XLSX na listi
- Stavke: količina, cijena, popust, PDV po liniji
- Tip plaćanja usklađen sa EFI (Virman→ORDER, Poslovna kartica→BUSINESSCARD, Avans→ADVANCE)
- Upozorenje isteka licence

**Ne kopirati 1:1:**
- Brand / boje VG eFiskal (ladybug)
- Pun ERP (lager, avans UI) dok nije u cijeni ~15 €
- Svih 9 jezika od starta — prvo CG + EN, ostalo u admin prevodima
## Fajlovi snimaka

| Fajl | Ekran |
|------|--------|
| `01-pocetni-ekran.png` | Početni / home |
| `02-izvjestaji.png` | Izvještaji |
| `03-podesavanja.png` | Podešavanja |
| `04-informacije-licenca-sertifikat.png` | Licenca, sertifikat, PJ/ENU |
| `05-fakture-lista-draft.png` | Fakture (draft) |
| `06-izmjeni-fakturu.png` | Izmjena fakture |
| `07-komitenti-modal.png` | Šifarnik komitenata |
| `08-dodaj-stavku.png` | Dodaj stavku |
| `09-artikli-modal.png` | Šifarnik artikala |
| `10-tip-placanja.png` | Tip plaćanja |
| `11-popust-na-racun.png` | Popust na račun |
| `12-poreske-stope-oslobodjenja.png` | PDV oslobođenja |
| `13-fakture-lista-statusi.png` | Lista sa statusima |
| `14-fakture-fiskalizovani.png` | Fiskalizovani + korekcije |
| `15-pregled-racuna-qr.png` | Pregled računa + QR |
| `16-jezici.png` | Jezici |
| `17-akcije-stampa-email.png` | Štampa / e-mail / sačuvaj |
