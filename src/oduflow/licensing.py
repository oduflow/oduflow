import base64
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

logger = logging.getLogger("oduflow")

LICENSE_FILENAME = "license.key"
LICENSE_CHECK_URL = "https://license.oduist.com/oduflow/check_license"

# License types
TYPE_UNLICENSED = "unlicensed"
TYPE_INDIVIDUAL = "individual"
TYPE_BUSINESS = "business"
TYPE_INTEGRATOR = "integrator"
# Individually agreed hosting / white-label license: carries a signed brand
# name and the hostnames it may be shown on (see oduflow.branding).
TYPE_CUSTOM = "custom"

VALID_TYPES = {TYPE_INDIVIDUAL, TYPE_BUSINESS, TYPE_INTEGRATOR, TYPE_CUSTOM}

# Display labels
TYPE_LABELS = {
    TYPE_UNLICENSED: "UNLICENSED — NON-COMMERCIAL USE ONLY",
    TYPE_INDIVIDUAL: "Licensed to individual",
    TYPE_BUSINESS: "Licensed to company",
    TYPE_INTEGRATOR: "Licensed to Odoo integrator",
    TYPE_CUSTOM: "Licensed to",
}

TYPE_SUFFIXES = {
    TYPE_BUSINESS: " (internal use only)",
}

# RSA public key for license verification
_PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuWvmwwY7/C1c9W4kU1/V
9o79OypjcOKCGvZ9H1IsmoAHtMHQ/Idfz2Rr/py6UVs37GH2mgA5BVSMA81Gecyb
gX8lvFGxzqh9UvgVpsyWGDFOOyjoQAPSVcbMmQxmYn0AzdyARdBUA62ripmS+m8s
4y12cS5S+ANqhjK83HCVuJMwCxwNFra2QumZ0PePNog+QSv1t74Ky6T6diNsnHGe
44GkmOS1WM1eHUKJ5buI+gupCrapvgwqcAhjE0M8Opj0du3mqDgD3yr+Rtr2qMo0
jEO6kSannZrR42puJ1+rVj10SnZQYDrz+EkC07UsFiTUGIadZ3zw/+AFE6DPaoej
pQIDAQAB
-----END PUBLIC KEY-----
"""


@dataclass(frozen=True)
class LicenseInfo:
    type: str
    name: str
    email: str
    issued: str = ""
    plan: str = ""
    license_id: str = ""
    valid_from: str = ""
    expires: str = ""
    brand_name: str = ""
    domains: tuple[str, ...] = ()

    @property
    def expired(self) -> bool:
        return bool(self.expires) and _parse_date(self.expires) <= datetime.now(
            timezone.utc
        )

    @property
    def status(self) -> str:
        if self.type == TYPE_UNLICENSED:
            return "unlicensed"
        if self.expired:
            return "expired"
        if self.valid_from and _parse_date(self.valid_from) > datetime.now(
            timezone.utc
        ):
            return "not_yet_valid"
        return "active"

    @property
    def label(self) -> str:
        base = TYPE_LABELS.get(self.type, TYPE_LABELS[TYPE_UNLICENSED])
        if self.type == TYPE_UNLICENSED:
            return base
        suffix = TYPE_SUFFIXES.get(self.type, "")
        label = f"{base}: {self.name}{suffix}"
        if self.expired:
            return f"LICENSE EXPIRED - {label}"
        if self.status == "not_yet_valid":
            return f"NOT YET VALID - {label}"
        return label

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "email": self.email,
            "label": self.label,
            "status": self.status,
            "issued": self.issued,
            "plan": self.plan,
            "valid_from": self.valid_from,
            "expires": self.expires,
            "perpetual": self.type != TYPE_UNLICENSED and not self.expires,
            "can_refresh": self.type != TYPE_UNLICENSED,
        }


_UNLICENSED = LicenseInfo(type=TYPE_UNLICENSED, name="", email="")


def _parse_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("License timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def get_license_path(etc_dir: str | None = None) -> str:
    if not etc_dir:
        from oduflow.settings import _resolve_etc_dir

        etc_dir = _resolve_etc_dir()
    return os.path.join(etc_dir, LICENSE_FILENAME)


def _verify_license_text(raw: str) -> LicenseInfo:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    raw = raw.strip()
    if "." not in raw:
        raise ValueError("Invalid license format")

    sig_b64, payload_b64 = raw.split(".", 1)
    signature = base64.b64decode(sig_b64)
    payload_bytes = base64.b64decode(payload_b64)

    pub_key = load_pem_public_key(_PUBLIC_KEY_PEM.encode())
    if not isinstance(pub_key, rsa.RSAPublicKey):
        raise ValueError("License public key must be an RSA key")
    pub_key.verify(
        signature,
        payload_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    data = json.loads(payload_bytes)
    if not isinstance(data, dict):
        raise ValueError("Invalid license payload")
    license_type = data.get("type", "")
    if license_type not in VALID_TYPES:
        raise ValueError(f"Unknown license type: {license_type}")

    # The signature already proves the license server issued these terms; the
    # client only checks what it relies on. How a period was paid (and with
    # which provider) is the license server's business, so any provider fields
    # in older keys are ignored.
    valid_from = _optional_str(data, "valid_from")
    expires = _optional_str(data, "expires")
    if expires:
        if valid_from and _parse_date(expires) <= _parse_date(valid_from):
            raise ValueError("Invalid license period")
    elif valid_from:
        raise ValueError("Invalid license period")
    license_id = _optional_str(data, "license_id")
    if license_id and not re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", license_id
    ):
        raise ValueError("Invalid license ID")

    brand_name = ""
    domains: tuple[str, ...] = ()
    if license_type == TYPE_CUSTOM:
        brand_name = _optional_str(data, "brand_name")
        raw_domains = data.get("domains")
        if (
            not license_id
            or not expires
            or not brand_name
            or brand_name != brand_name.strip()
            or len(brand_name) > 60
            or re.search(r"[\x00-\x1f\x7f<>]", brand_name)
            or not isinstance(raw_domains, list)
            or not raw_domains
            or not all(
                isinstance(d, str) and _DOMAIN_RE.fullmatch(d) for d in raw_domains
            )
        ):
            raise ValueError("Invalid custom license terms")
        domains = tuple(raw_domains)

    return LicenseInfo(
        type=license_type,
        name=_optional_str(data, "name"),
        email=_optional_str(data, "email"),
        issued=_optional_str(data, "issued"),
        plan=_optional_str(data, "plan"),
        license_id=license_id,
        valid_from=valid_from,
        expires=expires,
        brand_name=brand_name,
        domains=domains,
    )


_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def _optional_str(data: dict[str, Any], field: str) -> str:
    value = data.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"Invalid license field: {field}")
    return value


def get_license_info(etc_dir: str | None = None) -> LicenseInfo:
    license_path = get_license_path(etc_dir)
    if not os.path.isfile(license_path):
        return _UNLICENSED
    try:
        raw = open(license_path, "r", encoding="utf-8").read()
        return _verify_license_text(raw)
    except Exception as e:
        logger.warning("Invalid license file %s: %s", license_path, e)
        return _UNLICENSED


def install_license(source_path: str, etc_dir: str | None = None) -> LicenseInfo:
    raw = open(source_path, "r", encoding="utf-8").read()
    info = _verify_license_text(raw)
    license_path = get_license_path(etc_dir)
    os.makedirs(os.path.dirname(license_path), exist_ok=True)
    shutil.copy2(source_path, license_path)
    logger.info("License installed: %s", info.label)
    return info


def install_license_from_text(key_text: str, etc_dir: str | None = None) -> LicenseInfo:
    info = _verify_license_text(key_text)
    license_path = get_license_path(etc_dir)
    os.makedirs(os.path.dirname(license_path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=os.path.dirname(license_path), prefix=".license-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key_text.strip())
        os.replace(temp_path, license_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    logger.info("License installed: %s", info.label)
    return info


class RefreshResult(NamedTuple):
    info: LicenseInfo
    renewed: bool
    # The license server found no paid period to install and the holder can
    # subscribe again (get_license_checkout_url). The server decides; the
    # client never needs to know how payments are processed.
    subscription_required: bool = False


def refresh_license(etc_dir: str | None = None) -> RefreshResult:
    """Ask the license server for a renewed key and install it if there is one.

    Called when an operator asks (dashboard button, ``oduflow license refresh``)
    and, for a custom license, by the daily check during the grace period after
    expiry (oduflow.branding).
    """
    import httpx
    from cryptography.exceptions import InvalidSignature

    license_path = get_license_path(etc_dir)
    with open(license_path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    current = _verify_license_text(raw)
    try:
        response = httpx.post(
            LICENSE_CHECK_URL,
            json={"license_key": raw},
            timeout=25,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(
            "Unable to check the subscription. Please retry or contact support."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid license server response")
    if not data.get("renewed"):
        return RefreshResult(current, False, bool(data.get("subscription_required")))
    key = data.get("license_key")
    if not isinstance(key, str) or len(key) > 16384:
        raise ValueError("Invalid renewal key")
    try:
        updated = _verify_license_text(key)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Invalid renewal signature or payload") from exc
    identity_fields = ["type", "name", "email"]
    if current.expires:
        identity_fields.append("plan")
        if current.license_id:
            identity_fields.append("license_id")
    elif not updated.license_id:
        raise ValueError("Legacy migration requires a stable license identity")
    for field in identity_fields:
        if getattr(updated, field) != getattr(current, field):
            raise ValueError("Renewal does not match the installed license")
    if current.expires and (
        updated.status != "active"
        or _parse_date(updated.expires) <= _parse_date(current.expires)
    ):
        raise ValueError("Renewal does not extend the paid license period")
    # An operator may have installed another license while the network call ran.
    with open(license_path, "r", encoding="utf-8") as f:
        if f.read().strip() != raw:
            raise ValueError(
                "The installed license changed. Please check its status again."
            )
    return RefreshResult(install_license_from_text(key, etc_dir), True)


def get_license_checkout_url(etc_dir: str | None = None) -> str:
    """Exchange the installed credential for a short-lived hosted checkout link."""
    from urllib.parse import urlparse

    import httpx

    with open(get_license_path(etc_dir), "r", encoding="utf-8") as f:
        raw = f.read().strip()
    current = _verify_license_text(raw)
    if not current.license_id or not current.expired:
        raise ValueError(
            "Update license status first; subscribe after the granted period ends."
        )
    try:
        response = httpx.post(
            "https://license.oduist.com/oduflow/renew_checkout",
            json={"license_key": raw},
            timeout=25,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(
            "Unable to open checkout. Update license status or contact support."
        ) from exc
    url = data.get("checkout_url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        raise ValueError("Invalid checkout response")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "license.oduist.com"
        or parsed.path != "/oduflow/buy"
        or not parsed.query.startswith("renewal=")
    ):
        raise ValueError("Invalid checkout URL")
    return url
