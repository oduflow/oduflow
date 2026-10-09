"""Read, patch, validate and safely write oduflow.toml.

The engine behind the Server settings console (specs/0077). Every write goes
through the same pipeline, whichever way the candidate was produced (form
changes, a raw edit, a history restore):

1. **Patch** the current text with ``tomlkit``, so comments, ordering and the
   operator's formatting survive. A key the bundled template documents as a
   commented-out line (``# key = …``) is uncommented in place instead of being
   appended after unrelated comments.
2. **Validate** the candidate with the very parser the server boots with
   (``Settings.from_raw`` + ``validate``) plus the HTTP fail-closed startup
   checks, so the console cannot save a file the next start would refuse.
3. **Write** atomically under a lock, only if the file still has the revision
   the editor started from (otherwise :class:`ConflictError`: a hand edit or a
   second admin got there first). The previous and new texts go to a private
   history directory next to the config for diff and one-click revert.

It also computes what a change means for the running process: the leaf-level
diff between two documents, classified by :mod:`oduflow.config_schema`, and the
"live overlay" — the running document plus only the live-class changes — that
the server swaps in without a restart.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import tomlkit
from tomlkit.container import Container, OutOfOrderTableProxy
from tomlkit.items import AoT, Comment, InlineTable, Item, Null, Table

from oduflow import config_schema as schema
from oduflow.errors import ConfigError, ConflictError, PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_text
from oduflow.settings import Settings

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

logger = logging.getLogger("oduflow")

# What tomlkit can hand back for a table header. A super table whose
# children are not contiguous in the file ([team.1] … [route.api] …
# [team.2]) comes back as an OutOfOrderTableProxy, not a Table: a mapping
# over the scattered fragments, which reads, writes and deletes like one
# table. Every structural walk here must accept it or it would refuse
# perfectly valid files.
_TABLES = (Table, OutOfOrderTableProxy, tomlkit.TOMLDocument)

HISTORY_DIRNAME = ".oduflow-history"
HISTORY_KEEP = 50
MASK = "********"
MIN_ADMIN_PASSWORD = 12

_write_lock = threading.Lock()


# ── Snapshots ──────────────────────────────────────────────────────────────


def revision_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Snapshot:
    path: str  # resolved path of the real file (symlinks followed)
    text: str
    revision: str


def resolve_path(path: str) -> str:
    return os.path.realpath(path)


def read_snapshot(path: str) -> Snapshot:
    real = resolve_path(path)
    with open(real, encoding="utf-8") as f:
        text = f.read()
    return Snapshot(path=real, text=text, revision=revision_of(text))


# ── Changes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """Set ``path`` to ``value`` (browser JSON), or remove it (``unset``).

    A path naming a table instead of a key — a collection member such as
    ``("team", "3")`` or an optional table such as ``("backup",)`` — creates
    it from a dict of field values, or removes the whole table.
    """

    path: tuple[str, ...]
    value: Any = None
    unset: bool = False


def parse_changes(payload: Any) -> list[Change]:
    if not isinstance(payload, list) or not payload:
        raise ConfigError("Nothing to change.")
    changes: list[Change] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ConfigError("Each change must be an object.")
        path = item.get("path")
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(p, str) and p for p in path)
        ):
            raise ConfigError("Each change needs a non-empty path.")
        changes.append(
            Change(
                path=tuple(path),
                value=item.get("value"),
                unset=bool(item.get("unset", False)),
            )
        )
    return changes


# A secret the console never received: "keep what the file has".
KEEP = {"keep": True}


def _is_keep(value: Any) -> bool:
    return isinstance(value, dict) and value.get("keep") is True and len(value) == 1


def apply_changes(text: str, changes: list[Change]) -> str:
    """Return ``text`` with ``changes`` applied, formatting preserved."""
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:
        raise ConfigError(
            f"The current file is not valid TOML ({exc}); fix it in Raw TOML."
        ) from exc
    for change in changes:
        _apply_one(doc, change)
    return tomlkit.dumps(doc)


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _apply_one(doc: tomlkit.TOMLDocument, change: Change) -> None:
    path = change.path
    hit = schema.find_field(path)
    if hit is not None:
        group, f = hit
        if group.collection:
            _check_member_name(path, group)
        if f.apply == schema.LOCKED:
            raise ConfigError(
                f"{_dotted(path)} is read-only here: {f.note} Edit it in Raw "
                "TOML once the deployment is ready for it."
            )
        table = _table_at(doc, path[:-1], create=not change.unset)
        if table is None:
            return
        current = table.get(path[-1])
        value = None if change.unset else _coerce_with_keep(f, change.value, current)
        for alias in f.aliases:
            if alias in table:
                del table[alias]
        if value is None:
            if path[-1] in table:
                del table[path[-1]]
        else:
            _set_key(table, path[-1], value)
        return

    table_group = schema.find_group(path)
    if table_group is None or not (table_group.collection or table_group.optional):
        raise ConfigError(f"Unknown setting {_dotted(path)}.")
    if table_group.collection:
        _check_member_name(path, table_group)
    parent = _table_at(doc, path[:-1], create=not change.unset)
    if change.unset:
        if parent is not None and path[-1] in parent:
            del parent[path[-1]]
        return
    if parent is None:  # pragma: no cover - create=True always returns a table
        raise ConfigError(f"Cannot create {_dotted(path)}.")
    values = change.value if change.value is not None else {}
    if not isinstance(values, dict):
        raise ConfigError(f"{_dotted(path)}: expected an object of settings.")
    if path[-1] in parent:
        if table_group.collection:
            raise ConflictError(f"{_dotted(path)} already exists.")
        table = parent[path[-1]]
    else:
        table = tomlkit.table()
        parent[path[-1]] = table
    for key, raw_value in values.items():
        member_field = table_group.field(str(key))
        if member_field is None:
            raise ConfigError(f"Unknown setting {_dotted(path + (str(key),))}.")
        value = _coerce_with_keep(member_field, raw_value, None)
        if value is not None:
            _set_key(table, str(key), value)


def _check_member_name(path: tuple[str, ...], group: schema.Group) -> None:
    index = group.table.index("*")
    name = path[index]
    if not schema.COLLECTION_KEY_RE.match(name):
        raise ConfigError(
            f"Invalid {group.title.lower()} name {name!r}: use letters, digits, "
            "'-' or '_' (max 40)."
        )


def _coerce_with_keep(f: schema.Field, value: Any, current: Any) -> Any:
    """Coerce, resolving "keep the stored secret" markers against the file."""
    if f.kind == "env_map" and isinstance(value, dict):
        existing = current.unwrap() if hasattr(current, "unwrap") else current
        existing = existing if isinstance(existing, dict) else {}
        resolved: dict[str, Any] = {}
        for name, item in value.items():
            if _is_keep(item):
                if name not in existing:
                    raise ConfigError(f"{f.label}: {name} has no stored value to keep.")
                resolved[name] = str(existing[name])
            else:
                resolved[name] = item
        value = resolved
    try:
        return schema.coerce(f, value)
    except schema.CoercionError as exc:
        raise ConfigError(str(exc)) from None


def _table_at(doc: tomlkit.TOMLDocument, path: tuple[str, ...], *, create: bool) -> Any:
    """The tomlkit table at ``path``, creating missing tables when asked."""
    node: Any = doc
    for depth, part in enumerate(path):
        if part not in node:
            if not create:
                return None
            # Collection parents ([team], [route]) are "super tables": they
            # only hold [team.1], [route.api], … and never render a header.
            is_super = path[: depth + 1] in (("team",), ("route",))
            node[part] = tomlkit.table(is_super_table=is_super)
        node = node[part]
        if not isinstance(node, _TABLES):
            raise ConfigError(f"{_dotted(path[: depth + 1])} is not a table.")
    return node


def _to_item(value: Any) -> Item:
    if isinstance(value, dict):
        if not value:
            return tomlkit.inline_table()
        table = tomlkit.table()
        for key, item in value.items():
            table[key] = item
        return table
    scalar: Item = tomlkit.item(value)
    return scalar


def _container(table: Any) -> Container:
    return table if isinstance(table, Container) else table.value


_COMMENTED_KEY = "#\\s*{key}\\s*="


def _set_key(table: Any, key: str, value: Any) -> None:
    """Set ``key`` in ``table`` where a human reading the file expects it."""
    if key in table:
        # tomlkit keeps the existing line's inline comment on replacement.
        table[key] = _to_item(value)
        return
    item = _to_item(value)
    if isinstance(item, Table):
        table[key] = item
        return
    try:
        if _uncomment_in_place(table, key, item) or _insert_after_last_key(
            table, key, item
        ):
            return
    except (AttributeError, IndexError, KeyError, TypeError):
        # Container internals moved in a tomlkit release; plain append is
        # still correct TOML, only less tidy.
        logger.debug("tomlkit placement fallback for %s", key, exc_info=True)
    table[key] = item


def _uncomment_in_place(table: Any, key: str, item: Item) -> bool:
    """Replace a documented ``# key = …`` template line with the real key."""
    container = _container(table)
    pattern = re.compile(_COMMENTED_KEY.format(key=re.escape(key)))
    for index, (k, v) in enumerate(container.body):
        if k is not None or not isinstance(v, Comment):
            continue
        line = v.as_string().strip()
        if not pattern.match(line):
            continue
        # Keep the template's explanatory trailing comment on the live line,
        # in the column it was aligned to.
        remainder = line[1:].strip()
        comment = ""
        if "#" in remainder.split("=", 1)[1]:
            try:
                parsed = tomlkit.parse(remainder + "\n").item(key)
                comment = parsed.trivia.comment if isinstance(parsed, Item) else ""
            except Exception:
                comment = ""
        if comment:
            column = line.find("#", 1)
            width = len(f"{key} = {item.as_string()}")
            item.trivia.comment_ws = " " * max(2, column - width)
            item.trivia.comment = comment
        container._insert_at(index, key, item)
        container.body[index + 1] = (None, Null())
        return True
    return False


def _insert_after_last_key(table: Any, key: str, item: Item) -> bool:
    """Insert right after the table's last plain key, before trailing comments.

    Appending would land the key after the comment banner that introduces the
    *next* section of the file.
    """
    container = _container(table)
    last = -1
    for index, (k, v) in enumerate(container.body):
        if isinstance(v, (Table, AoT)) and not isinstance(v, InlineTable):
            break
        if k is not None:
            last = index
    if last < 0 or last == len(container.body) - 1:
        return False
    container._insert_at(last + 1, key, item)
    return True


# ── Validation ─────────────────────────────────────────────────────────────


def parse_text(text: str) -> dict[str, Any]:
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML syntax error: {exc}") from exc
    return data


def check_http_auth(settings: Settings) -> None:
    """The HTTP transport's fail-closed authentication rules.

    Shared with ``server._start_http`` so the console can refuse a config the
    next start would reject, instead of discovering it on restart.
    """
    if settings.allow_insecure_http:
        return
    # ``all`` (not ``any``): a single passwordless team can never log in, so
    # refuse rather than silently lock it out.
    if not settings.teams or not all(t.ui_password for t in settings.teams.values()):
        raise PrerequisiteNotMetError(
            "Refusing to start the HTTP transport with an unauthenticated web "
            "dashboard: set a [team.*] ui_password in oduflow.toml (the dashboard "
            "exposes interactive shells, SQL and privileged service creation for "
            "every environment). To run it open on purpose (e.g. behind your own "
            "auth proxy), set [server] allow_insecure_http = true."
        )
    if not any(t.auth_token or t.production_token for t in settings.teams.values()):
        raise PrerequisiteNotMetError(
            "Refusing to start the HTTP transport with no MCP authentication: "
            "set a [team.*] auth_token in oduflow.toml. To "
            "run unauthenticated on purpose (e.g. behind your own auth proxy), "
            "set [server] allow_insecure_http = true."
        )
    if len(settings.teams) > 1:
        tokenless = sorted(
            tid for tid, team in settings.teams.items() if not team.auth_token
        )
        if tokenless:
            raise PrerequisiteNotMetError(
                "HTTP transport with multiple teams requires an auth_token "
                f"for every team; missing for: {', '.join(tokenless)}."
            )


def validate_text(
    text: str, path: str, *, http: bool = True
) -> tuple[Settings, dict[str, Any]]:
    """Dry-run ``text`` through the boot parser; raise ConfigError if refused."""
    raw = parse_text(text)
    try:
        settings = Settings.from_raw(raw, path)
        settings.validate()
        if http:
            check_http_auth(settings)
    except PrerequisiteNotMetError as exc:
        raise ConfigError(str(exc)) from exc
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        raise ConfigError(str(exc)) from exc
    return settings, raw


def check_console_policy(old_raw: dict[str, Any], new_raw: dict[str, Any]) -> None:
    """Rules the console adds on top of the parser for values it writes."""
    old_pw = _get(old_raw, ("admin", "password"))
    new_pw = _get(new_raw, ("admin", "password"))
    if new_pw != old_pw:
        if not isinstance(new_pw, str) or not new_pw.strip():
            raise ConfigError(
                "The console password cannot be removed from the console itself; "
                "use `oduflow admin disable` on the server."
            )
        if len(new_pw.strip()) < MIN_ADMIN_PASSWORD:
            raise ConfigError(
                f"The console password must be at least {MIN_ADMIN_PASSWORD} "
                "characters."
            )


# ── Diff and classification ────────────────────────────────────────────────

_MISSING = object()


def _get(raw: Any, path: tuple[str, ...]) -> Any:
    node = raw
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@dataclass(frozen=True)
class LeafChange:
    path: tuple[str, ...]
    old: Any
    new: Any
    apply: str
    label: str
    note: str
    secret: bool
    presence: bool = False  # a whole table added/removed

    def to_json(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "label": self.label,
            "apply": self.apply,
            "note": self.note,
            "secret": self.secret,
            "presence": self.presence,
            "old": _display(self.old, self.secret, self.presence),
            "new": _display(self.new, self.secret, self.presence),
        }


def _display(value: Any, secret: bool, presence: bool) -> str:
    if value is None:
        return "(absent)" if presence else "(default)"
    if presence:
        return "configured"
    if secret:
        if isinstance(value, dict):
            names = ", ".join(sorted(value)) or "none"
            return f"{len(value)} variable(s): {names}"
        return "(set)" if value != "" else "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict) and not value:
        return "{}"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _label(path: tuple[str, ...], f: schema.Field | None) -> str:
    return _dotted(path) if f is None else f"{_dotted(path)} ({f.label})"


def diff_raw(
    old: dict[str, Any], new: dict[str, Any], boot: Settings | None = None
) -> list[LeafChange]:
    """Leaf-level differences between two parsed documents, classified.

    ``boot`` is the configuration the process *started* with, not the one it
    runs on now: a ``restart_if`` predicate asks about what startup built
    (threads, the route table), which live swaps never change.
    """
    out: list[LeafChange] = []

    def leaf(path: tuple[str, ...], a: Any, b: Any) -> None:
        hit = schema.find_field(path)
        f = hit[1] if hit else None
        if f is None and len(path) >= 2:
            # A legacy alias reports under the key that replaced it.
            group = schema.find_group(path[:-1])
            if group is not None:
                f = next((x for x in group.fields if path[-1] in x.aliases), None)
        apply = f.apply if f else schema.RESTART
        if (
            f is not None
            and apply == schema.LIVE
            and f.restart_if is not None
            and boot is not None
            and f.restart_if(boot)
        ):
            apply = schema.RESTART
        out.append(
            LeafChange(
                path=path,
                old=a,
                new=b,
                apply=apply,
                label=_label(path, f),
                note=f.note if f else "Not managed by the console form.",
                secret=schema.is_secret_path(path),
            )
        )

    def walk(prefix: tuple[str, ...], a: dict[str, Any], b: dict[str, Any]) -> None:
        for key in list(a) + [k for k in b if k not in a]:
            path = prefix + (key,)
            av, bv = a.get(key), b.get(key)
            if av == bv:
                continue
            if schema.find_field(path) is not None:
                leaf(path, av, bv)
                continue
            if isinstance(av, dict) or isinstance(bv, dict):
                group = schema.find_group(path)
                adding_or_removing = av is None or bv is None
                if (
                    group is not None
                    and (group.optional or group.collection)
                    and adding_or_removing
                ):
                    what = "added" if av is None else "removed"
                    out.append(
                        LeafChange(
                            path=path,
                            old=av,
                            new=bv,
                            apply=group.presence_apply,
                            label=f"{_dotted(path)} ({what})",
                            note=group.presence_note,
                            secret=False,
                            presence=True,
                        )
                    )
                    continue
                walk(
                    path,
                    av if isinstance(av, dict) else {},
                    bv if isinstance(bv, dict) else {},
                )
                continue
            leaf(path, av, bv)

    walk((), old, new)
    return out


def worst_apply(changes: list[LeafChange]) -> str:
    if not changes:
        return schema.LIVE
    return max((c.apply for c in changes), key=schema.APPLY_ORDER.index)


def live_overlay(
    running_raw: dict[str, Any], file_raw: dict[str, Any], boot: Settings
) -> tuple[dict[str, Any], list[LeafChange]]:
    """The running document plus only its live-class differences from the file.

    Applying just these keeps the process consistent: restart-class values
    stay as they were at boot until a restart, even when the file already
    carries their new value. ``boot`` classifies the changes, so it is the
    boot Settings (see :func:`diff_raw`), not the live-swapped ones.
    """
    result = copy.deepcopy(running_raw)
    applied: list[LeafChange] = []
    for change in diff_raw(running_raw, file_raw, boot):
        if change.apply != schema.LIVE:
            continue
        node = result
        for part in change.path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        if change.new is None:
            node.pop(change.path[-1], None)
        else:
            node[change.path[-1]] = copy.deepcopy(change.new)
        applied.append(change)
    return result, applied


# ── Masking ────────────────────────────────────────────────────────────────


def mask_raw(raw: Any, prefix: tuple[str, ...] = ()) -> Any:
    """A JSON-safe copy of a parsed document with every secret replaced."""
    if isinstance(raw, dict):
        if prefix and prefix[-1] == "agent_env":
            return {str(k): {"$secret": True, "set": bool(v)} for k, v in raw.items()}
        return {str(k): mask_raw(v, prefix + (str(k),)) for k, v in raw.items()}
    if isinstance(raw, str) and prefix and schema.is_secret_path(prefix):
        return {"$secret": True, "set": bool(raw)}
    if isinstance(raw, (datetime,)):
        return raw.isoformat()
    if isinstance(raw, list):
        return [mask_raw(v, prefix) for v in raw]
    if isinstance(raw, (str, int, float, bool)) or raw is None:
        return raw
    return str(raw)


def _changed_secret_paths(old_text: str, new_text: str) -> set[tuple[str, ...]]:
    try:
        old_raw = tomllib.loads(old_text)
        new_raw = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return set()
    changed: set[tuple[str, ...]] = set()

    def walk(prefix: tuple[str, ...], a: Any, b: Any) -> None:
        if isinstance(a, dict) or isinstance(b, dict):
            a = a if isinstance(a, dict) else {}
            b = b if isinstance(b, dict) else {}
            for key in set(a) | set(b):
                walk(prefix + (key,), a.get(key, _MISSING), b.get(key, _MISSING))
        elif a != b and prefix and schema.is_secret_path(prefix):
            changed.add(prefix)

    walk((), old_raw, new_raw)
    return changed


def mask_text(text: str, changed: set[tuple[str, ...]] | None = None) -> str:
    """``text`` with secret values replaced, formatting kept, for diffs."""
    try:
        doc = tomlkit.parse(text)
    except Exception:
        # Unparseable text cannot be masked structurally; mask whole lines.
        return "\n".join(
            re.sub(r"=.*", f'= "{MASK}"', line)
            if re.search(r"(password|token|secret|key)\s*=", line, re.I)
            else line
            for line in text.splitlines()
        )

    def walk(node: Any, prefix: tuple[str, ...]) -> None:
        for key in list(node.keys()):
            item = node[key]
            path = prefix + (str(key),)
            if isinstance(item, (*_TABLES, InlineTable)):
                walk(item, path)
            elif isinstance(item, str) and item and schema.is_secret_path(path):
                marker = f"{MASK} (changed)" if changed and path in changed else MASK
                node[key] = marker

    walk(doc, ())
    return tomlkit.dumps(doc)


def masked_diff(
    old_text: str,
    new_text: str,
    *,
    labels: tuple[str, str] = ("current", "new"),
    context: int = 2,
) -> str:
    changed = _changed_secret_paths(old_text, new_text)
    old_masked = mask_text(old_text)
    new_masked = mask_text(new_text, changed)
    return "".join(
        difflib.unified_diff(
            old_masked.splitlines(True),
            new_masked.splitlines(True),
            fromfile=f"oduflow.toml ({labels[0]})",
            tofile=f"oduflow.toml ({labels[1]})",
            n=context,
        )
    )


def unknown_keys(raw: dict[str, Any]) -> list[str]:
    """Keys in the file that no console form edits (edit them in Raw TOML)."""
    found: list[str] = []

    def walk(prefix: tuple[str, ...], node: dict[str, Any]) -> None:
        for key, value in node.items():
            path = prefix + (str(key),)
            if schema.find_field(path) is not None:
                continue
            if isinstance(value, dict):
                walk(path, value)
                continue
            group = schema.find_group(path[:-1]) if len(path) > 1 else None
            if group is not None and any(path[-1] in f.aliases for f in group.fields):
                continue
            found.append(_dotted(path))

    walk((), raw)
    return found


# ── Writing ────────────────────────────────────────────────────────────────


def history_dir(config_path: str) -> str:
    return os.path.join(os.path.dirname(resolve_path(config_path)), HISTORY_DIRNAME)


@contextmanager
def _file_lock(config_path: str) -> Iterator[None]:
    """Serialize writers across threads and processes (console, CLI)."""
    directory = history_dir(config_path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with _write_lock:
        fd = os.open(os.path.join(directory, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def replace_config_file(config_path: str, text: str) -> None:
    """Atomically replace the config, keeping its owner and owner-only mode.

    A chmod of the existing inode cannot revoke an already-open reader, so the
    new text (it carries secrets) is staged privately and swapped in with
    ``os.replace``; ownership and any stricter owner permissions (e.g. 0400)
    are preserved. A config symlink is followed, never replaced.
    """
    real = resolve_path(config_path)
    original = os.stat(real)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(real)}.", dir=os.path.dirname(real)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            staged = os.fstat(stream.fileno())
            if (staged.st_uid, staged.st_gid) != (original.st_uid, original.st_gid):
                os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            mode = original.st_mode & 0o600
            if staged.st_mode & 0o777 != mode:
                os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, real)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _new_history_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(2)}"


_HISTORY_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{4}$")


def write_config(
    config_path: str,
    new_text: str,
    base_revision: str,
    *,
    note: str,
    paths: list[str],
    actor: str = "",
) -> str:
    """Replace the file if it still has ``base_revision``; return the new one.

    Both texts are kept in the history directory so the change can be
    reviewed and reverted.
    """
    with _file_lock(config_path):
        current = read_snapshot(config_path)
        if current.revision != base_revision:
            raise ConflictError(
                "oduflow.toml changed since you opened it (edited by hand or by "
                "another admin). Reload to see the current file, then redo the "
                "change."
            )
        if current.text == new_text:
            return current.revision
        entry_id = _new_history_id()
        directory = history_dir(config_path)
        atomic_write_private_text(
            os.path.join(directory, f"{entry_id}.before.toml"), current.text
        )
        atomic_write_private_text(
            os.path.join(directory, f"{entry_id}.after.toml"), new_text
        )
        new_revision = revision_of(new_text)
        atomic_write_private_text(
            os.path.join(directory, f"{entry_id}.json"),
            json.dumps(
                {
                    "id": entry_id,
                    "ts": time.time(),
                    "note": note,
                    "paths": paths,
                    "actor": actor,
                    "before": current.revision,
                    "after": new_revision,
                },
                indent=2,
            )
            + "\n",
        )
        replace_config_file(config_path, new_text)
        _prune_history(directory)
    return new_revision


def _prune_history(directory: str) -> None:
    ids = sorted(
        name[: -len(".json")]
        for name in os.listdir(directory)
        if name.endswith(".json") and _HISTORY_ID_RE.match(name[: -len(".json")])
    )
    for entry_id in ids[:-HISTORY_KEEP] if len(ids) > HISTORY_KEEP else []:
        for suffix in (".json", ".before.toml", ".after.toml"):
            try:
                os.remove(os.path.join(directory, entry_id + suffix))
            except FileNotFoundError:
                pass


def list_history(config_path: str) -> list[dict[str, Any]]:
    directory = history_dir(config_path)
    if not os.path.isdir(directory):
        return []
    entries: list[dict[str, Any]] = []
    for name in sorted(os.listdir(directory), reverse=True):
        if not name.endswith(".json") or not _HISTORY_ID_RE.match(name[:-5]):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def read_history(config_path: str, entry_id: str) -> tuple[dict[str, Any], str, str]:
    """``(entry, before_text, after_text)`` of one history entry."""
    if not _HISTORY_ID_RE.match(entry_id):
        raise ConfigError("Unknown history entry.")
    directory = history_dir(config_path)
    try:
        with open(os.path.join(directory, f"{entry_id}.json"), encoding="utf-8") as f:
            entry = json.load(f)
        with open(
            os.path.join(directory, f"{entry_id}.before.toml"), encoding="utf-8"
        ) as f:
            before = f.read()
        with open(
            os.path.join(directory, f"{entry_id}.after.toml"), encoding="utf-8"
        ) as f:
            after = f.read()
    except (OSError, ValueError) as exc:
        raise ConfigError("Unknown history entry.") from exc
    return entry, before, after


# ── Running process ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigRuntime:
    """The server hooks the console needs, injected by ``server._start_http``.

    ``running`` returns the cached Settings and the parsed document it was
    built from; ``swap`` replaces both; ``restart`` stops serving and re-execs
    the process; ``busy`` lists operations holding locks right now; ``boot``
    is the Settings this process started from, which live swaps never touch.
    """

    running: Callable[[], tuple[Settings, dict[str, Any] | None]]
    swap: Callable[[Settings, dict[str, Any]], None]
    restart: Callable[[], None] | None = None
    busy: Callable[[], list[str]] | None = None
    boot: Settings | None = None
    boot_id: str = dataclasses.field(default_factory=lambda: secrets.token_hex(8))

    def boot_settings(self) -> Settings:
        """What startup built, for ``restart_if`` (see :func:`diff_raw`).

        Falls back to the live Settings when no snapshot was injected: worst
        case the console classifies a live key as restart-class, which only
        ever withholds a change until a restart.
        """
        return self.boot if self.boot is not None else self.running()[0]


def pending_changes(
    runtime: ConfigRuntime, file_raw: dict[str, Any]
) -> list[LeafChange]:
    """What the file says that the running process does not use yet."""
    _, running_raw = runtime.running()
    return diff_raw(running_raw or {}, file_raw, runtime.boot_settings())


def apply_live(
    runtime: ConfigRuntime, file_raw: dict[str, Any]
) -> tuple[list[LeafChange], str]:
    """Swap in the file's live-class values; ``(applied, problem)``.

    When the live values do not validate on their own against the running
    restart-class values (a cross-key rule spans both), nothing is swapped and
    everything waits for the restart.
    """
    running, running_raw = runtime.running()
    if running_raw is None:
        return [], ""
    overlay, applied = live_overlay(running_raw, file_raw, runtime.boot_settings())
    if not applied:
        return [], ""
    try:
        settings = Settings.from_raw(overlay, running.toml_path)
        settings.validate()
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        return [], (
            f"The live values could not be applied without the rest ({exc}); "
            "everything applies on restart."
        )
    runtime.swap(settings, overlay)
    return applied, ""
