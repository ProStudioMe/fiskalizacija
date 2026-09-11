# EFI → Sepko → Navira

Ugovor za `HttpPartnerAdapter` / `to_navira_payload`. Imena desno su EFI XML (`RegisterInvoice`), iz Priloga 2.

Sepko API i dalje prima stare alias vrijednosti (`cash`, `non_cash`, `transfer`).

## Zaglavlje računa

| EFI | Sepko | Napomena |
|-----|-------|----------|
| `InvType` | `inv_type` | `INVOICE`, `CREDIT_NOTE`, `CORRECTIVE`, `ERROR_CORRECTIVE`, … |
| `TypeOfInv` | `invoice_type` | `CASH` / `NONCASH` (`cash`/`non_cash` alias) |
| `InvNum` | `inv_num` | `{BusinUnitCode}/{InvOrdNum}/{year}/{TCRCode}` |
| `InvOrdNum` | `inv_ord_num` | redni broj u godini |
| `IssueDateTime` | `issue_datetime` | ISO 8601; **ne mijenja se** pri offline retry |
| `TCRCode` | tenant `tcr_code` | ENU, 10 znakova (SEP) |
| `BusinUnitCode` | tenant `busin_unit_code` | poslovni prostor |
| `SoftCode` | tenant `soft_code` | kod softvera |
| `OperatorCode` | tenant `operator_code` | operator |
| `IsIssuerInVAT` | tenant `is_issuer_in_vat` | |
| `IIC` | `ikof` | 32 hex; lokalni potpis ili CIS |
| `IICSignature` | `iic_signature` | RSA potpis IIC plain-a |
| `IICRef` | `iic_ref` / `ref_ikof` | **obavezno** za CREDIT_NOTE / CORRECTIVE / ERROR_CORRECTIVE |
| `FIC` | `jikr` | odgovor CIS-a |
| `SameTaxes` | `sameTaxes` | agregacija po `vatRate` (+ `exemptFromVAT`) |
| `TotPriceWoVAT` | `totals.net` | kredit: negativan |
| `TotVATAmt` | `totals.vat` | kredit: negativan |
| `TotPrice` | `totals.gross` | kredit: negativan, ≠ 0 |
| `Note` | `notes` | |
| `PayMethod.Type` | `payment_method` | vidi šifrarnik |
| `Buyer.IDType` / `IDNum` | `buyer.pib` | TIN za CG |

## Stavke

| EFI | Sepko / payload | Napomena |
|-----|-----------------|----------|
| `N` / `C` / `U` / `Q` | `n`/`c`/`u`/`q` | `u` iz artikla (`unit`, default `KOM`) |
| `UPB` | `upb` | jed. cijena bez PDV |
| `VR` | `vr` | stopa % |
| `PA` | `pa` | bruto linije |
| `ExemptFromVAT` | `ex` / `exemptFromVAT` | samo `EX*` kodovi → `VAT_CL_N`; `PDV0` nije oslobođenje |

## Plaćanja (`PayMethod.Type`)

CASH račun mora imati `BANKNOTE` ili `CARD`. NONCASH ne smije imati ta dva.

| Alias (stari API) | EFI |
|-------------------|-----|
| `cash` | `BANKNOTE` |
| `card` | `CARD` |
| `transfer` | `ORDER` |
| `other` | `OTHER` |

Ostalo (samo EFI ime): `BUSINESSCARD`, `SVOUCHER`, `COMPANY`, `ADVANCE`, `ACCOUNT`, `FACTORING`, `OTHER-CASH`.

## Kredit / korektivni

1. UI bira tip + **originalni fiskalizovani račun** (`ref_invoice_id` → IKOF = `iicRef`).
2. Korisnik unosi **pozitivne** iznose; builder negira količine i totale.
3. Payload: `invType`, negativni `items`/`tot*`, `iicRef`.

## Offline (48h)

Ako CIS/Navira nije dostupan (mreža / 5xx / nema FIC) a lokalni IKOF postoji:

- status `pending`, štampa sa IKOF + QR, **bez** JIKR;
- isti `iic` + `iicSignature` + `issueDateTime` se šalju ponovo;
- `/cron/raspored` poziva `retry_offline_invoices` (prozor 48h).

## QR

`{base}?iic=&tin=&crtd=&ord=&bu=&cr=&sw=&prc=`

- test: `https://efitest.tax.gov.me/ic/#/verify`
- prod: `https://mapr.tax.gov.me/ic/#/verify`

## Ostalo (nije Faza 1 UI)

`RegisterTCR`, puni `SUMMARY` / `PERIODICAL` tokovi kroz UI.

`RegisterCashDeposit` je wired u `HttpPartnerAdapter` (`POST /v1/register-cash-deposit`).
