"""PKCS#12 / KPM potpisivanje fiskalnih poruka (JSON, XML, IIC).

SOAP/XAdES prema CIS-u i dalje radi Navira. Ovaj modul:
- učitava .p12/.pfx (cryptography)
- potpisuje IIC (IKOF) string i JSON payload RSA-SHA256
- nudi opciono PKCS#11 (pametna kartica) kad je modul dostupan
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509 import Certificate

from sepko.config import get_settings


class SigningError(Exception):
    """Certifikat nije učitan, lozinka je pogrešna, ili PKCS#11 nije dostupan."""


@dataclass(frozen=True)
class Pkcs12Material:
    private_key: PrivateKeyTypes
    certificate: Certificate
    ca_certs: list[Certificate]

    @property
    def fingerprint_sha256(self) -> str:
        return self.certificate.fingerprint(hashes.SHA256()).hex()

    @property
    def subject(self) -> str:
        return self.certificate.subject.rfc4514_string()

    @property
    def not_valid_after(self) -> datetime:
        utc = getattr(self.certificate, "not_valid_after_utc", None)
        if utc is not None:
            return utc
        naive = self.certificate.not_valid_after
        return naive.replace(tzinfo=timezone.utc) if naive.tzinfo is None else naive


def load_pkcs12(data: bytes, password: str | None) -> Pkcs12Material:
    pwd = password.encode("utf-8") if password else None
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, pwd)
    except ValueError as exc:
        raise SigningError(f"PKCS#12 nije učitan: {exc}") from exc
    if key is None or cert is None:
        raise SigningError("PKCS#12 ne sadrži privatni ključ i sertifikat")
    return Pkcs12Material(private_key=key, certificate=cert, ca_certs=list(extra or []))


def load_pkcs12_file(path: str | Path, password: str | None) -> Pkcs12Material:
    p = Path(path)
    if not p.is_file():
        raise SigningError(f"PKCS#12 fajl nije pronađen: {p}")
    return load_pkcs12(p.read_bytes(), password)


def inspect_pkcs12(data: bytes, password: str | None) -> dict[str, str]:
    """Metapodaci sertifikata — bez izvoza privatnog ključa."""
    mat = load_pkcs12(data, password)
    return {
        "subject": mat.subject,
        "fingerprint_sha256": mat.fingerprint_sha256,
        "not_valid_after": mat.not_valid_after.isoformat(),
    }


def rsa_sign(material: Pkcs12Material, payload: bytes) -> bytes:
    key = material.private_key
    if not hasattr(key, "sign"):
        raise SigningError("Privatni ključ ne podržava RSA potpis")
    return key.sign(payload, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]


def rsa_sign_b64(material: Pkcs12Material, payload: bytes) -> str:
    return base64.b64encode(rsa_sign(material, payload)).decode("ascii")


def build_iic_plain(
    *,
    tin: str,
    issue_datetime: datetime,
    inv_num: str,
    busin_unit_code: str,
    tcr_code: str,
    soft_code: str,
    tot_price: Decimal,
) -> str:
    """EFI IIC ulaz (pipe-separated) — isti string se hešira i potpisuje."""
    dt = issue_datetime.isoformat(timespec="seconds")
    return "|".join(
        [
            tin or "",
            dt,
            inv_num or "",
            busin_unit_code or "",
            tcr_code or "",
            soft_code or "",
            f"{tot_price:.2f}",
        ]
    )


def iic_from_plain(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()[:32].upper()


def sign_iic(material: Pkcs12Material, plain: str) -> tuple[str, str]:
    """Vrati (IIC/IKOF hex, IICSignature base64)."""
    iic = iic_from_plain(plain)
    signature = rsa_sign_b64(material, plain.encode("utf-8"))
    return iic, signature


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_json(material: Pkcs12Material, payload: dict[str, Any]) -> dict[str, Any]:
    raw = canonical_json(payload)
    return {
        "payload": payload,
        "algorithm": "RSASSA-PKCS1-v1_5-SHA256",
        "signature": rsa_sign_b64(material, raw),
        "cert_fingerprint": material.fingerprint_sha256,
    }


def sign_xml(material: Pkcs12Material, xml: str | bytes) -> dict[str, str]:
    """Detached RSA potpis XML bajtova.

    Pun enveloped XMLDSig/XAdES (xmlsec) ostaje Naviri / Linux workeru.
    """
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    return {
        "algorithm": "RSASSA-PKCS1-v1_5-SHA256",
        "signature": rsa_sign_b64(material, raw),
        "cert_fingerprint": material.fingerprint_sha256,
        "digest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def export_public_pem(material: Pkcs12Material) -> bytes:
    return material.certificate.public_bytes(serialization.Encoding.PEM)


def load_tenant_pkcs12(slug: str) -> Pkcs12Material | None:
    """Traži `{cert_dir}/{slug}.p12` ili `.pfx`. Lozinka: SEPKO_CERT_PASSWORD."""
    settings = get_settings()
    cert_dir = (settings.cert_dir or "").strip()
    if not cert_dir:
        return None
    root = Path(cert_dir)
    for name in (f"{slug}.p12", f"{slug}.pfx"):
        path = root / name
        if path.is_file():
            return load_pkcs12_file(path, settings.cert_password or None)
    return None


def sign_with_pkcs11(payload: bytes) -> bytes:
    """Pametna kartica (KPM) preko PKCS#11 — opciono, nije default na Windowsu."""
    settings = get_settings()
    module = (settings.pkcs11_module or "").strip()
    if not module:
        raise SigningError("SEPKO_PKCS11_MODULE nije setovan")
    try:
        import pkcs11  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SigningError("python-pkcs11 nije instaliran") from exc

    lib = pkcs11.lib(module)
    token = next(iter(lib.get_tokens()), None)
    if token is None:
        raise SigningError("PKCS#11 token (kartica) nije pronađen")
    pin = settings.pkcs11_pin or None
    with token.open(user_pin=pin) as session:
        keys = list(session.get_keys(object_class=pkcs11.constants.ObjectClass.PRIVATE_KEY))
        if not keys:
            raise SigningError("Na kartici nema privatnog ključa")
        mechanism = pkcs11.mechanisms.Mechanism.SHA256_RSA_PKCS
        return keys[0].sign(payload, mechanism=mechanism)
