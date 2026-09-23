import base64
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("oduflow")

LICENSE_FILENAME = "license.key"
LICENSE_CHECK_URL = "https://license.oduist.com/oduflow/check_license"

# License types
TYPE_UNLICENSED = "unlicensed"
TYPE_INDIVIDUAL = "individual"
TYPE_BUSINESS = "business"
TYPE_INTEGRATOR = "integrator"

VALID_TYPES = {TYPE_INDIVIDUAL, TYPE_BUSINESS, TYPE_INTEGRATOR}

# Display labels
TYPE_LABELS = {
    TYPE_UNLICENSED: "UNLICENSED — NON-COMMERCIAL USE ONLY",
    TYPE_INDIVIDUAL: "Licensed to individual",
    TYPE_BUSINESS: "Licensed to company",
    TYPE_INTEGRATOR: "Licensed to Odoo integrator",
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
    scope: str = ""
    subscription_id: str = ""
    paddle_environment: str = ""
    valid_from: str = ""
    expires: str = ""

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
            "can_refresh": bool(self.subscription_id),
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

    annual_fields = (
        "plan",
        "scope",
        "subscription_id",
        "paddle_environment",
        "valid_from",
        "expires",
    )
    if data.get("version") == 2 or any(data.get(field) for field in annual_fields):
        expected = {
            "solo": (TYPE_INDIVIDUAL, "individual-commercial"),
            "business": (TYPE_BUSINESS, "internal-use"),
            "integrator": (TYPE_INTEGRATOR, "client-services"),
        }
        if (
            data.get("version") != 2
            or not all(
                isinstance(data.get(field), str) and data[field]
                for field in annual_fields
            )
            or expected.get(data["plan"]) != (license_type, data["scope"])
            or not data["subscription_id"].startswith("sub_")
            or data["paddle_environment"] not in ("sandbox", "production")
            or _parse_date(data["expires"]) <= _parse_date(data["valid_from"])
        ):
            raise ValueError("Invalid annual license terms")

    return LicenseInfo(
        type=license_type,
        name=data.get("name", ""),
        email=data.get("email", ""),
        issued=data.get("issued", ""),
        **{field: data.get(field, "") for field in annual_fields},
    )


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


def refresh_license(etc_dir: str | None = None) -> tuple[LicenseInfo, bool]:
    """Manually check a paid renewal; never called during normal operation."""
    import httpx
    from cryptography.exceptions import InvalidSignature

    license_path = get_license_path(etc_dir)
    with open(license_path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    current = _verify_license_text(raw)
    if not current.subscription_id:
        return current, False
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
        return current, False
    key = data.get("license_key")
    if not isinstance(key, str) or len(key) > 16384:
        raise ValueError("Invalid renewal key")
    try:
        updated = _verify_license_text(key)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("Invalid renewal signature or payload") from exc
    for field in (
        "type",
        "name",
        "email",
        "plan",
        "scope",
        "subscription_id",
        "paddle_environment",
    ):
        if getattr(updated, field) != getattr(current, field):
            raise ValueError("Renewal does not match the installed license")
    if updated.status != "active" or _parse_date(updated.expires) <= _parse_date(
        current.expires
    ):
        raise ValueError("Renewal does not extend the paid license period")
    # An operator may have installed another license while the network call ran.
    with open(license_path, "r", encoding="utf-8") as f:
        if f.read().strip() != raw:
            raise ValueError(
                "The installed license changed. Please check its status again."
            )
    return install_license_from_text(key, etc_dir), True
