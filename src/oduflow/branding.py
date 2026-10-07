"""White-label branding for custom licenses.

A custom license (see oduflow.licensing) lets its holder run Oduflow for their
own clients under their own name: the dashboard, login page and MCP surface
show the signed ``brand_name`` instead of "Oduflow", vendor links (Docs,
Feedback, updates, license dialog) are removed, and the logo and icon can be
replaced by files in ``<config dir>/branding/``.

Branding is on only while all of these hold:

- the installed license is a valid custom license, not before its validity
  period and at most ``GRACE_DAYS`` after it expires;
- every team hostname is one of the license's domains or a subdomain of one,
  so a copied key does nothing on someone else's servers.

Otherwise the product looks like stock Oduflow. Nothing is ever disabled: the
brand is the only thing the license controls. Hidden infrastructure names
(containers, databases, ``oduflow.toml``) are out of scope by design.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

PRODUCT_NAME = "Oduflow"
GRACE_DAYS = 30
BRANDING_DIRNAME = "branding"
LOGO_FILENAME = "logo.png"
ICON_FILENAME = "icon.png"

# Recompute at least this often so the end of the grace period is noticed
# without a restart; a changed license file is noticed immediately.
_CACHE_SECONDS = 60
# Daily renewal attempts during the grace period after expiry.
_RENEWAL_INTERVAL_SECONDS = 24 * 3600
_RENEWAL_TICK_SECONDS = 3600


@dataclass(frozen=True)
class Brand:
    name: str = PRODUCT_NAME
    white_label: bool = False
    logo_path: str = ""
    icon_path: str = ""
    # Why white label is off for an installed custom license (for the CLI and
    # the log); empty when it is on or no custom license is installed.
    reason: str = ""

    @property
    def slug(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return slug or "oduflow"

    def rebrand(self, text: str) -> str:
        """Show the brand instead of the product name in user-facing prose.

        Only the capitalized product name is replaced. Lower-case technical
        identifiers (``oduflow.toml``, container and database names) are left
        alone on purpose: they belong to the operator's servers.
        """
        if not self.white_label or PRODUCT_NAME not in text:
            return text
        return text.replace(PRODUCT_NAME, self.name)


_STOCK = Brand()
_lock = threading.Lock()
_get_settings: Callable[[], Settings] | None = None
_cached: tuple[float, tuple[float, ...], Brand] | None = None
_last_logged = ""


def configure(get_settings: Callable[[], Settings]) -> None:
    """Remember how to load settings; call once at startup (idempotent)."""
    global _get_settings, _cached  # noqa: PLW0603
    with _lock:
        if _get_settings is not get_settings:
            _get_settings = get_settings
            _cached = None


def current() -> Brand:
    """The brand in effect now; stock Oduflow until configure() is called."""
    global _cached  # noqa: PLW0603
    getter = _get_settings
    if getter is None:
        return _STOCK
    try:
        settings = getter()
    except Exception:  # noqa: BLE001 - branding must never break a request
        return _STOCK
    stamp = _file_stamp(settings.etc_dir)
    now = time.monotonic()
    with _lock:
        if _cached and _cached[1] == stamp and now - _cached[0] < _CACHE_SECONDS:
            return _cached[2]
    brand = compute(settings)
    with _lock:
        _cached = (now, stamp, brand)
    _log_change(brand)
    return brand


def name() -> str:
    return current().name


def is_white_label() -> bool:
    return current().white_label


def rebrand(text: str) -> str:
    return current().rebrand(text)


def compute(settings: Settings, now: datetime | None = None) -> Brand:
    from oduflow.licensing import TYPE_CUSTOM, get_license_info

    info = get_license_info(settings.etc_dir)
    if info.type != TYPE_CUSTOM:
        return _STOCK
    now = now or datetime.now(timezone.utc)
    if info.status == "not_yet_valid":
        return Brand(reason="the custom license is not valid yet")
    expires = datetime.fromisoformat(info.expires.replace("Z", "+00:00"))
    if now > expires + timedelta(days=GRACE_DAYS):
        return Brand(
            reason=f"the custom license expired more than {GRACE_DAYS} days ago"
        )
    hostnames = [_host(team.hostname) for team in settings.teams.values()]
    outside = [h for h in hostnames if not _covered(h, info.domains)]
    if not hostnames or outside:
        return Brand(
            reason="team hostname not covered by the license domains: "
            + ", ".join(outside or ["(none configured)"])
        )
    branding_dir = os.path.join(settings.etc_dir, BRANDING_DIRNAME)
    logo = os.path.join(branding_dir, LOGO_FILENAME)
    icon = os.path.join(branding_dir, ICON_FILENAME)
    logo_path = logo if os.path.isfile(logo) else ""
    icon_path = icon if os.path.isfile(icon) else logo_path
    return Brand(
        name=info.brand_name,
        white_label=True,
        logo_path=logo_path,
        icon_path=icon_path,
    )


def grace_days_left(settings: Settings, now: datetime | None = None) -> int | None:
    """Days of white label left after an expired custom license, else None."""
    from oduflow.licensing import TYPE_CUSTOM, get_license_info

    info = get_license_info(settings.etc_dir)
    if info.type != TYPE_CUSTOM or not info.expired:
        return None
    now = now or datetime.now(timezone.utc)
    expires = datetime.fromisoformat(info.expires.replace("Z", "+00:00"))
    left = (expires + timedelta(days=GRACE_DAYS) - now).total_seconds()
    return max(0, math.ceil(left / 86400))


def start_grace_renewal(get_settings: Callable[[], Settings]) -> threading.Thread:
    """Try to install a renewed custom license once a day during the grace period.

    The operator's clients never see license prompts, so a paid renewal that
    nobody installed would otherwise turn the brand off after the grace period.
    """

    def loop() -> None:
        last_attempt = 0.0
        while True:
            try:
                if time.monotonic() - last_attempt >= _RENEWAL_INTERVAL_SECONDS:
                    if _renew_in_grace(get_settings()):
                        last_attempt = time.monotonic()
            except Exception:  # noqa: BLE001 - keep the daemon alive
                logger.warning("Custom license renewal check failed", exc_info=True)
                last_attempt = time.monotonic()
            time.sleep(_RENEWAL_TICK_SECONDS)

    thread = threading.Thread(target=loop, name="license-grace-renewal", daemon=True)
    thread.start()
    return thread


def _renew_in_grace(settings: Settings) -> bool:
    """One renewal attempt; False when no attempt was due."""
    from oduflow.licensing import refresh_license

    days_left = grace_days_left(settings)
    if days_left is None:
        return False
    logger.warning(
        "Custom license expired; white label turns off in %d day(s) unless it is "
        "renewed. Checking the license server for a renewal.",
        days_left,
    )
    try:
        result = refresh_license(settings.etc_dir)
    except (ValueError, OSError) as exc:
        logger.warning("Custom license renewal check failed: %s", exc)
        return True
    if result.renewed:
        logger.info("Custom license renewed until %s", result.info.expires)
    elif result.subscription_required:
        logger.warning(
            "Custom license has no active subscription; run "
            "`oduflow license subscribe` to renew it."
        )
    return True


def _file_stamp(etc_dir: str) -> tuple[float, ...]:
    from oduflow.licensing import LICENSE_FILENAME

    stamps = []
    for path in (
        os.path.join(etc_dir, LICENSE_FILENAME),
        os.path.join(etc_dir, BRANDING_DIRNAME, LOGO_FILENAME),
        os.path.join(etc_dir, BRANDING_DIRNAME, ICON_FILENAME),
    ):
        try:
            stamps.append(os.stat(path).st_mtime)
        except OSError:
            stamps.append(0.0)
    return tuple(stamps)


def _host(hostname: str) -> str:
    return (urlsplit(f"//{hostname.strip()}").hostname or "").lower().rstrip(".")


def _covered(hostname: str, domains: tuple[str, ...]) -> bool:
    return bool(hostname) and any(
        hostname == domain or hostname.endswith("." + domain) for domain in domains
    )


def _log_change(brand: Brand) -> None:
    global _last_logged  # noqa: PLW0603
    message = (
        f"White label active: {brand.name}"
        if brand.white_label
        else f"White label off: {brand.reason}"
        if brand.reason
        else ""
    )
    if message and message != _last_logged:
        (logger.info if brand.white_label else logger.warning)(message)
        if brand.white_label and any(
            _host(h).startswith("oduflow.") for h in _team_hostnames()
        ):
            logger.warning(
                "A team hostname starts with 'oduflow.'; set an explicit "
                "[team.*] hostname so clients do not see it."
            )
    _last_logged = message


def _team_hostnames() -> list[str]:
    if _get_settings is None:
        return []
    try:
        return [team.hostname for team in _get_settings().teams.values()]
    except Exception:  # noqa: BLE001
        return []
