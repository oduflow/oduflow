# 0003 — Database templates + filestore isolation (copy vs fuse-overlayfs)

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `6f85183` "DB load" (2026-02-06), `3a26c70` "Load filestore" (2026-02-06)
**Key code today:** `docker_ops/system_ops.py` (template build/import/reload/list), `docker_ops/env_ops.py` (`_mount_filestore`, `remount_template_overlays`), `settings.overlay_threshold_mb`, `naming.get_template_db_name`

## Context

An environment ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]) is
a PostgreSQL database plus an Odoo filestore. Creating one from a bare
`-i base` install is slow and gives an empty system — not the realistic,
module-rich state an agent needs to exercise a branch against. Environments are
also **ephemeral and recreated often**, so paying the full install cost on every
create is unacceptable.

Forces at play:
- Environment creation must be **fast** and start from a known-good DB state
  (the customer's modules already installed, demo/seed data present).
- Many environments share the *same* starting point but must not share *mutable*
  state — one branch's writes must never leak into another's.
- Filestores range from a few MB to many GB. Copying a multi-GB filestore for
  every environment wastes disk and time; copying a tiny one is trivial and
  safer than a fuse mount.

## Decision

Introduce **templates**: reusable, named snapshots of a `(database dump +
filestore)` that environments clone from at create time. Isolate each
environment's filestore from the template's with a **size-tiered strategy**:
plain copy for small templates, **fuse-overlayfs** above a threshold.

- **Template = DB dump + filestore + metadata.** A template lives in the team's
  data dir as a SQL/`pgdump` dump, a `filestore/` tree, and a `metadata.json`
  (Odoo image/version, sizes, `use_overlay`). Its database is restored once into
  a PostgreSQL *template database* (`oduflow_template_{id}_{name}`), and new
  environment DBs are created from it.
- **Tiered filestore isolation.** On create, if the template's filestore is
  below `overlay_threshold_mb` (default 50 MB) it is **copied**; at or above the
  threshold it is mounted **copy-on-write via fuse-overlayfs** — the template
  filestore is the read-only *lower* layer, the environment's writes go to its
  own *upper* layer, and the environment sees the merged view.
- **Template-less environments are allowed.** Passing no template initialises a
  fresh Odoo with `-i base`, for cases where a clean slate is wanted.

## How it works (macro)

- **Building a template.** Templates are created by saving an existing
  environment as a template, or by **importing from a running Odoo** instance
  (`import_template_from_odoo` / `import-template`): download a backup, extract
  `dump` + `filestore`, read `manifest.json` to record the Odoo version/image,
  and load the dump into the template DB.
- **Metadata-driven, not rescanned.** The `use_overlay` decision is computed once
  (from filestore size vs threshold) and **stored in template metadata**, so env
  creation never re-walks a large filestore to decide copy-vs-overlay; old
  templates without the flag fall back to a size scan.
- **Cloning at create time.** Creating an environment makes its DB from the
  template DB and provisions its filestore by the tiered strategy above. Each
  overlay environment gets its own `upper`/`work`/`merged` dirs, keeping its
  changes private.
- **Non-destructive template update.** Because each overlay env keeps its changes
  in a separate upper layer, the template's read-only lower can be swapped under
  *live* environments without losing their data. A reusable
  `remount_template_overlays()` context manager unmounts affected envs (keeping
  their uppers), lets the caller mutate the template filestore, and remounts them
  against the new lower. This makes import/save-as-template/reload safe for live
  envs, and powers a `refresh_template` tool that re-applies a template's current
  filestore to its running overlay environments.

## Consequences

- Environment creation is **fast and realistic**: a clone of a known-good DB +
  filestore instead of a from-scratch install, which is what makes the
  per-branch ephemeral model practical at scale.
- The copy/overlay split trades a small amount of complexity (a fuse dependency,
  mount lifecycle) for large disk and time savings on big customer databases,
  while keeping small templates dependency-free.
- Templates became the unit of "shared, persistent state" in the system —
  persistence lives in templates and the per-branch DB, not in long-lived
  servers — reinforcing the disposable-environment design.
- The overlay layering enabled later **non-destructive** template operations,
  but also coupled template mutation to the set of live mounts, requiring the
  remount dance to avoid yanking the lower out from under running envs.

## History

- `6f85183` (2026-02-06) — load a DB dump into PostgreSQL as the environment's
  starting state.
- `3a26c70` (2026-02-06) — load the template filestore alongside the DB.
- `8b43721` (2026-02-14) — move bundled templates into `src/`, support per-addon
  branches, and introduce template **metadata**.
- `ab69289` (2026-02-11) — **template-less** environments (`template_name=None`
  → `-i base` from scratch).
- `a6d53fc` (2026-02-15) — `import_template_from_odoo`: build a template from a
  running Odoo backup, auto-detecting version and DB name from the manifest.
- `d2fc703` (2026-02-14) — `ODUFLOW_OVERLAY_THRESHOLD_MB`: copy filestore below
  the threshold, fuse-overlayfs above it.
- `e393c44` (2026-02-16) — store `use_overlay` in template metadata instead of
  scanning the filestore on every environment creation.
- `c3bee0f` (2026-02-20) — rename the template DB prefix to
  `oduflow_template_{id}_{name}` and make `template_name` required.
- `1edafff` (2026-06-05) — non-destructive template update for fuse-overlayfs
  envs: `remount_template_overlays()` context manager + `refresh_template` tool,
  preserving each environment's filestore delta across template mutation.
- (2026-11-19) — template dumps are tenant input: they load as a one-off
  non-superuser role from a throwaway client container, and the template
  database belongs to a per-team owner role instead of the superuser. Rationale
  and trade-offs in [[0013-per-environment-db-credentials-and-sanitization]].
