# Poreska uprava — CIS SOAP (odvojeno od Navire / EXTFISK)

Implementacija: `sepko/pu/`. Kanal: Admin → EFI → **Poreska uprava (CIS SOAP)** ili `SEPKO_PARTNER_MODE=poreska`.

## Endpointi (Tehnička spec v5)

| Okruženje | URL |
|-----------|-----|
| Test | `https://efitest.tax.gov.me/fs-v1/FiscalizationService` |
| Produkcija | `https://efi.tax.gov.me/fs-v1/FiscalizationService` |

`SEPKO_CIS_TEST_URL` / `SEPKO_CIS_PROD_URL` ako treba override. Tenant `mode=prod` bira produkciju.

## IKOF (§4.3.2)

```
PIB|IssueDateTime|InvOrdNum|BusinUnitCode|TCRCode|SoftCode|TotPrice
```

1. RSA-SHA256 (PKCS#1 v1.5) nad UTF-8 ulazom  
2. `IICSignature` = hex potpisa (velika slova)  
3. `IIC` = MD5(bajtovi potpisa) hex  

Nije isti algoritam kao Navira (`sign_iic` / SHA256 plain).

## XMLDSig (§4.3.1)

Na `RegisterInvoiceRequest` (`Id="Request"`):

- enveloped-signature  
- exclusive C14N  
- Digest SHA-256  
- Signature RSA-SHA256  
- `KeyInfo/X509Certificate`  

## Certifikat

SEP aplikativni certifikat: `SEPKO_CERT_DIR/{tenant-slug}.p12` (ili `.pfx`), lozinka `SEPKO_CERT_PASSWORD`. Bez certifikata PU kanal ne šalje račun.

## Šta ovo nije

- Nije Navira HTTP JSON  
- Nije EXTFISK HTTP XML (`ApiKey` u XML-u)  
- `RegisterCashDeposit` još nije na ovom kanalu  
