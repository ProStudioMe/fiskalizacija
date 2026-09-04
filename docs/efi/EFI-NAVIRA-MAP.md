# EFI → Sepko → Navira

Ugovor za `HttpPartnerAdapter._to_navira_payload`. Imena desno su EFI XML (`RegisterInvoice`), iz Priloga 2.

Sepko API i dalje prima stare alias vrijednosti (`cash`, `non_cash`, `transfer`).

## Zaglavlje računa

| EFI | Sepko | Napomena |
|-----|-------|----------|
| `InvType` | `inv_type` | default `INVOICE` |
| `TypeOfInv` | `invoice_type` | `CASH` / `NONCASH` (`cash`/`non_cash` alias) |
| `InvNum` | `inv_num` | `{BusinUnitCode}/{InvOrdNum}/{year}/{TCRCode}` |
| `InvOrdNum` | `inv_ord_num` | redni broj u godini |
| `IssueDateTime` | `issue_datetime` | ISO 8601 |
| `TCRCode` | tenant `tcr_code` | ENU, 10 znakova (SEP) |
| `BusinUnitCode` | tenant `busin_unit_code` | poslovni prostor |
| `SoftCode` | tenant `soft_code` | kod softvera |
| `OperatorCode` | tenant `operator_code` | operator |
| `IsIssuerInVAT` | tenant `is_issuer_in_vat` | |
| `IIC` | `ikof` | 32 hex; Navira/CIS |
| `FIC` | `jikr` | odgovor CIS-a |
| `TotPriceWoVAT` | `totals.net` | |
| `TotVATAmt` | `totals.vat` | |
| `TotPrice` | `totals.gross` | |
| `Note` | `notes` | |
| `PayMethod.Type` | `payment_method` | vidi šifrarnik |
| `Buyer.IDType` / `IDNum` | `buyer.pib` | TIN za CG |

## Plaćanja (`PayMethod.Type`)

CASH račun mora imati `BANKNOTE` ili `CARD`. NONCASH ne smije imati ta dva.

| Alias (stari API) | EFI |
|-------------------|-----|
| `cash` | `BANKNOTE` |
| `card` | `CARD` |
| `transfer` | `ORDER` |
| `other` | `OTHER` |

Ostalo (samo EFI ime): `BUSINESSCARD`, `SVOUCHER`, `COMPANY`, `ADVANCE`, `ACCOUNT`, `FACTORING`, `OTHER-CASH`.

## QR

`{base}?iic=&tin=&crtd=&ord=&bu=&cr=&sw=&prc=`

- test: `https://efitest.tax.gov.me/ic/#/verify`
- prod: `https://mapr.tax.gov.me/ic/#/verify`

## Kasnije (nije Faza 1)

`RegisterTCR`, `CORRECTIVE` / `ADVANCE` / `SUMMARY` / `PERIODICAL` / `CREDIT_NOTE` / `ERROR_CORRECTIVE`.

`RegisterCashDeposit` je wired u `HttpPartnerAdapter` (`POST /v1/register-cash-deposit`).
