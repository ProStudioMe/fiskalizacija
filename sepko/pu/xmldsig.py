"""Enveloped XMLDSig za CIS (Tehnička spec v5 §4.3.1).

- enveloped-signature
- exclusive C14N
- Digest SHA-256
- Signature RSA-SHA256
- KeyInfo/X509Certificate
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree

from sepko.signing import Pkcs12Material, rsa_sign

DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
C14N_EXC = "http://www.w3.org/2001/10/xml-exc-c14n#"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _c14n(el: etree._Element) -> bytes:
    return etree.tostring(el, method="c14n", exclusive=True, with_comments=False)


def sign_enveloped(request_el: etree._Element, material: Pkcs12Material) -> etree._Element:
    """Ubaci <Signature> u RegisterInvoiceRequest (Id=Request)."""
    digest = _b64(hashlib.sha256(_c14n(request_el)).digest())

    sig = etree.SubElement(request_el, f"{{{DSIG_NS}}}Signature")
    signed_info = etree.SubElement(sig, f"{{{DSIG_NS}}}SignedInfo")
    etree.SubElement(signed_info, f"{{{DSIG_NS}}}CanonicalizationMethod", Algorithm=C14N_EXC)
    etree.SubElement(signed_info, f"{{{DSIG_NS}}}SignatureMethod", Algorithm=RSA_SHA256)
    ref = etree.SubElement(signed_info, f"{{{DSIG_NS}}}Reference", URI="#Request")
    transforms = etree.SubElement(ref, f"{{{DSIG_NS}}}Transforms")
    etree.SubElement(transforms, f"{{{DSIG_NS}}}Transform", Algorithm=ENVELOPED)
    etree.SubElement(transforms, f"{{{DSIG_NS}}}Transform", Algorithm=C14N_EXC)
    etree.SubElement(ref, f"{{{DSIG_NS}}}DigestMethod", Algorithm=SHA256)
    digest_el = etree.SubElement(ref, f"{{{DSIG_NS}}}DigestValue")
    digest_el.text = digest

    sig_value = etree.SubElement(sig, f"{{{DSIG_NS}}}SignatureValue")
    sig_value.text = _b64(rsa_sign(material, _c14n(signed_info)))

    key_info = etree.SubElement(sig, f"{{{DSIG_NS}}}KeyInfo")
    x509_data = etree.SubElement(key_info, f"{{{DSIG_NS}}}X509Data")
    x509_cert = etree.SubElement(x509_data, f"{{{DSIG_NS}}}X509Certificate")
    der = material.certificate.public_bytes(Encoding.DER)
    x509_cert.text = _b64(der)
    return request_el
