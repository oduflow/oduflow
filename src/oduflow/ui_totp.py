"""Local, per-team dashboard TOTP enrollment and persistent replay protection.

Only the server CLI can enroll/reset a factor. Atomic files and flock coordinate
CLI processes with the running web server; no secret is sent to an external API.
"""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TypedDict, cast

import pyotp

from oduflow.errors import FlowError
from oduflow.settings import Settings, TeamSettings

_MAX_ATTEMPTS = 10
_WINDOW = 300


class _State(TypedDict):
    version: str
    secret: str
    last_step: int
    failures: list[float]


class TOTPStateError(FlowError):
    """Unreadable authentication state must never disable MFA implicitly."""


class TOTPThrottled(FlowError):
    """The team's persistent MFA attempt budget is exhausted."""


def _path(settings: Settings, team: TeamSettings) -> Path | None:
    directory = team.data_dir
    if not directory and settings.base_data_dir:
        directory = os.path.join(settings.base_data_dir, f"team_{team.team_id}")
    return Path(directory) / ".ui_totp.json" if directory else None


def _load(path: Path | None) -> _State:
    if path is None:
        return {"version": "", "secret": "", "last_step": -1, "failures": []}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _load(None)
    except (OSError, UnicodeError) as exc:
        raise TOTPStateError(
            "Cannot read UI 2FA state; check server file permissions."
        ) from exc
    try:
        state = json.loads(raw)
        valid = (
            isinstance(state, dict)
            and isinstance(state["version"], str)
            and re.fullmatch(r"[0-9a-f]{32}", state["version"])
            and isinstance(state["secret"], str)
            and (not state["secret"] or re.fullmatch(r"[A-Z2-7]{32}", state["secret"]))
            and type(state["last_step"]) is int
            and isinstance(state["failures"], list)
            and all(type(t) in (int, float) for t in state["failures"])
        )
        if not valid:
            raise ValueError("Invalid state")
    except (ValueError, KeyError, TypeError) as exc:
        raise TOTPStateError(
            "Invalid UI 2FA state; restore it or reset it with the server CLI."
        ) from exc
    return cast(_State, state)


def version(
    settings: Settings, team: TeamSettings, *, password_only: bool = False
) -> str:
    # Read the factor and its generation together. A concurrent enrollment must
    # not turn a password-only session into a session for the newly enabled MFA.
    state = _load(_path(settings, team))
    if password_only and state["secret"]:
        raise ValueError("TOTP verification is required before issuing a UI session.")
    return state["version"]


def enabled(settings: Settings, team: TeamSettings) -> bool:
    return bool(_load(_path(settings, team))["secret"])


@contextmanager
def _locked(settings: Settings, team: TeamSettings) -> Iterator[Path]:
    path = _path(settings, team)
    if path is None:
        raise TOTPStateError("A persistent data directory is required for UI 2FA.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _save(path: Path, state: _State) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".ui_totp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the rename as well as the contents (including replay state).
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _matching_step(secret: str, code: str, now: float) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    current = int(now // 30)
    totp = pyotp.TOTP(secret)
    for step in (current, current - 1, current + 1):
        if step < 0:
            continue
        # Always an explicit UTC instant: pyotp reads a bare timestamp as a
        # naive *local* datetime, which is ambiguous during a DST fall-back
        # hour and would shift every expected code by an hour on such a server.
        moment = datetime.fromtimestamp(step * 30, timezone.utc)
        if secrets.compare_digest(totp.at(moment), code):
            return step
    return None


def verify(settings: Settings, team: TeamSettings, code: str) -> str | None:
    """Consume a code and return the exact MFA version proved by this login.

    An empty secret means password-only login. None means a bad/replayed code.
    The version is passed to the cookie issuer, never re-read after verification,
    so a concurrent CLI reset/enrollment cannot upgrade an old authentication.
    """
    path = _path(settings, team)
    if path is None:
        return ""
    with _locked(settings, team) as path:
        state = _load(path)
        if not state["secret"]:
            return str(state["version"])
        now = time.time()
        failures = [t for t in state["failures"] if t > now - _WINDOW]
        if len(failures) >= _MAX_ATTEMPTS:
            raise TOTPThrottled("Too many failed attempts. Try again later.")
        step = _matching_step(state["secret"], code, now)
        if step is None or step <= state["last_step"]:
            state["failures"] = failures + [now]
            _save(path, state)
            return None
        state["last_step"] = step
        state["failures"] = []
        _save(path, state)
        return str(state["version"])


def enroll(settings: Settings, team: TeamSettings, secret: str, code: str) -> None:
    if not team.ui_password:
        raise FlowError("Set the team's ui_password before enabling UI 2FA.")
    if not re.fullmatch(r"[A-Z2-7]{32}", secret):
        raise FlowError("Invalid TOTP setup key.")
    step = _matching_step(secret, code, time.time())
    if step is None:
        raise FlowError("Invalid authenticator code. UI 2FA was not changed.")
    with _locked(settings, team) as path:
        if _load(path)["secret"]:
            raise FlowError(
                "UI 2FA is already enabled. Reset it before enrolling again."
            )
        _save(
            path,
            {
                "version": secrets.token_hex(16),
                "secret": secret,
                "last_step": step,
                "failures": [],
            },
        )


def reset(settings: Settings, team: TeamSettings) -> None:
    """Keep a new generation even when disabled, revoking all previous cookies."""
    with _locked(settings, team) as path:
        _save(
            path,
            {
                "version": secrets.token_hex(16),
                "secret": "",
                "last_step": -1,
                "failures": [],
            },
        )


def run_cli(settings: Settings, team: TeamSettings, action: str) -> None:
    """Interactive local administration, deliberately not an MCP/UI capability."""
    try:
        if action == "reset":
            answer = input(
                f"Disable UI 2FA and revoke UI sessions for team {team.team_id}? [y/N] "
            )
            if answer.strip().lower() != "y":
                print("Cancelled. UI 2FA was not changed.")
                return
            reset(settings, team)
            print("UI 2FA disabled. Operator sessions revoked; shared links unchanged.")
            return
        if not team.ui_password:
            raise FlowError("Set the team's ui_password before enabling UI 2FA.")
        if enabled(settings, team):
            raise FlowError(
                "UI 2FA is already enabled. Reset it before enrolling again."
            )
        import qrcode

        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=f"{team.hostname} / team {team.team_id}", issuer_name="Oduflow"
        )
        qr = qrcode.QRCode(border=4)
        qr.add_data(uri)
        print(
            "Scan this QR code with your authenticator app. Keep the setup key private."
        )
        qr.print_ascii(invert=True)
        print(f"Manual setup key: {secret}")
        code = getpass.getpass("Authenticator code: ").strip()
        enroll(settings, team, secret, code)
        print("UI 2FA enabled. Operator sessions revoked; shared links unchanged.")
        print("Wait for the next authenticator code before signing in.")
    except (EOFError, KeyboardInterrupt):
        raise FlowError("Cancelled. UI 2FA was not changed.") from None
    except OSError as exc:
        raise TOTPStateError(
            "Cannot update UI 2FA state; check server file permissions."
        ) from exc
