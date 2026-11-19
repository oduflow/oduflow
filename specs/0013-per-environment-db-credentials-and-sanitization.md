# 0013 — Per-environment PostgreSQL credentials + two-tier database sanitization

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `cd0b9ec` "two-tier database sanitization" (2026-02-24), `e4949b0` "per-environment PostgreSQL credentials" (2026-02-26)
**Key code today:** `env_credentials.py` (generate / persist / load / delete per-env creds), `sanitizer.py` (`neutralize_environment` native baseline + two-tier script runner), `docker_ops/env_ops.py` (`_create_pg_role`, post-clone ownership fixup), `docker_ops/system_ops.py` (shared DB, role helpers, `provision_scoped_role` for a database that predates its role), `server.py` (`_inject_db_password` — bootstrap superuser secret)

## Context

All environments share **one PostgreSQL server** container, with one database per
environment. Two data-safety problems followed from how that started out, and
from where the databases come from.

1. **Shared superuser.** Every environment connected as the same global
   `odoo`/`odoo` user. That account owns *every* environment's database, so a
   single leaked or misused credential — or a sloppy script — reaches across all
   tenants and branches. There was no isolation at the database layer to match
   the container/filestore isolation already in place.
2. **Production-derived data.** Templates ([[0003-database-templates-and-filestore-isolation]])
   are frequently imported from a real production Odoo. A clone of production
   carries **live mail servers, real customer data, scheduled jobs** — exactly
   the things that must *not* fire from a throwaway dev environment. An agent
   spinning up a branch must not be able to email real customers.

A complication sits underneath both: PostgreSQL's `CREATE DATABASE ... TEMPLATE`
copies objects but leaves them **owned by the original (superuser) role**, so a
freshly created per-environment role can't run the DDL Odoo needs and can't even
see some objects in the catalog — which surfaced as confusing "relation already
exists" / "role cannot be dropped" failures.

## Decision

Two related decisions, both about confining blast radius at the database layer.

- **Per-environment PostgreSQL role.** Each environment gets its **own login role
  and password**, generated at create time, persisted to
  `workspace/env_credentials.json`, and used as the **owner** of that
  environment's database. Odoo connects as this role. The global superuser is
  retained only for **administrative** operations (CREATE/DROP DATABASE, template
  loads, GC, orphan cleanup). Old environments without a credentials file fall
  back to the global user, so the change is backward compatible.
- **Two-tier sanitization.** After provisioning (default on, `sanitize=True`),
  Oduflow scrubs the database in two tiers: a **system-wide** baseline owned by
  the team admin, then **per-repo** rules owned by the developer. Both tiers are
  directories of `.sql` and `.py` scripts run in alphabetical order, so the
  baseline is consistent across the fleet while each project can add its own
  rules.

## How it works (macro)

- **Credential lifecycle.** `create_credentials` derives a role name from the
  team + environment (`u_<team>_<env>`, capped at PostgreSQL's 63-char limit) and
  a random password, writes them next to the environment, and `_create_pg_role`
  creates the role as DB owner. Tests, ad-hoc DB queries, the dashboard SQL
  terminal, and the sanitizer all `load_credentials` so they connect as the
  per-env role, not the superuser. On delete/rebuild the role is dropped and the
  file removed; orphan-role detection cleans up stragglers.
- **Post-clone ownership fixup.** When a database is created from a template, the
  fixup transfers ownership of the `public` schema and every table, sequence, and
  view to the new per-env role (DDL requires ownership; `GRANT ALL` only covers
  DML), and drops Odoo's signaling sequences carried over from the template so
  Odoo can recreate them cleanly on first start. This is the load-bearing glue
  that makes a cloned DB usable under a non-superuser owner.
- **Sanitization tiers.** `sanitize_environment` runs scripts from the team's
  `{data_dir}/odoo_sanitize/` first (seeded with a bundled
  `01_disable_mail.sql`), then from the active checkout's
  `.oduflow/odoo_sanitize/` folder. The checkout is resolved from
  `oduflow.local_path` for live-mount environments and from the managed workspace
  otherwise. `.sql` runs against the env DB; `.py` runs *inside* the Odoo
  container using the per-env credentials, so repo rules can use the Odoo ORM.
  Failures are logged as warnings, not fatal — sanitization is best-effort
  hardening, not a gate.

## Consequences

- A compromised or misbehaving environment is **confined to its own database**:
  its login owns only its own data, so the database layer now matches the
  isolation that containers, ports, and filestores already provide. Tenancy
  isolation ([[0014-team-based-multi-tenancy]]) gains a real DB-level boundary.
- Production-derived templates become **safe to clone** for development: mail
  servers are removed and risky data neutralised before the agent touches the
  environment, so a dev branch cannot email real customers or trigger live jobs.
- **Two tiers** put each rule where it belongs — the operator owns the
  fleet-wide baseline, the developer owns project-specific scrubbing — mirroring
  the same deployment-vs-repo split as the configuration model
  ([[0016-configuration-model]]).
- The non-superuser ownership model is **more correct but more fragile** at the
  clone boundary: it required a string of fixes (ownership reassignment strategy,
  role-membership grants, `--no-owner` template restores) because PostgreSQL's
  template-clone and role-drop semantics interact in non-obvious ways.

## Evolution

The clone-time ownership transfer was reworked several times as edge cases
surfaced:

- `31df320` (2026-02-26) — iterate tables/sequences and `ALTER ... OWNER TO` the
  new role after a template clone, so the env role can see them (fixes spurious
  "relation already exists" on signaling sequences).
- `c6878e4` (2026-02-27) — replace the per-type loops with a single
  `REASSIGN OWNED BY` covering all object types.
- `5c7b737` (2026-02-27) — `REASSIGN OWNED BY` fails on system objects owned by a
  superuser; instead **grant the env role membership** in the superuser role so it
  inherits ownership for DDL. (The current code keeps an explicit
  schema/table/sequence/view `ALTER OWNER` sweep plus a signaling-sequence drop.)

A later hardening closed the **shared-superuser** gap flagged in Context point 1
— the part the per-env role decision left untouched, since the superuser is still
needed for admin operations:

- `#74` (2026-06-17) — **auto-generate the shared superuser password.** The
  bundled `oduflow.toml` no longer ships a hardcoded `password = "odoo"`; on first
  init the bootstrap injects a random `secrets.token_urlsafe(24)` into the
  generated config's `[database]` section, so every install gets a unique
  superuser secret instead of a published default. The trade-off chosen was to
  keep the secret in the instance's own `oduflow.toml`
  ([[0016-configuration-model]]) rather than a sidecar credentials file — the
  config is already the single source of truth, loaded each start, and generating
  at config-creation time means an existing user config is never rewritten. An
  explicit `[database].password` still overrides, and pre-existing installs (whose
  volume was initialised under `odoo`) are left untouched, so it is fully backward
  compatible. The matching admin connections rely on local trust auth inside the
  container, so they were unaffected by the change.

The per-env role confines **SQL**; it does not confine the **client** that sends
it, and that turned out to be the weaker half of the boundary:

- (2026-11-19) — **caller-written SQL no longer runs `psql` inside the shared
  PostgreSQL container.** The dashboard SQL console and `run_db_query` both
  picked the scoped role correctly, but executed `psql` via `docker exec` in the
  shared DB container, as root. `psql` is a client program: `\!` there is a root
  shell beside every team's data files, and the container's local socket uses
  trust auth, so `\c <db> <superuser>` (or `\! psql -U <superuser>`) reached the
  whole cluster regardless of the role the session started with. Filtering
  meta-commands was rejected as the fix — it is a denylist over a client with
  many ways to shell out, and it would not help an interactive session at all.
  Instead the client moved to where the caller is already trusted: both surfaces
  now run `psql` in the **environment's own Odoo container**, as the image's
  unprivileged `odoo` user, connecting over the Docker network like Odoo does,
  with the scoped role's password in `PGPASSWORD` (never argv). There every
  connection is password-authenticated, so `\c` to another role needs a password
  the caller does not have, and `\!` lands in a container they already have a
  shell in. The costs were accepted knowingly: the environment must be running
  (`run_db_query` now wakes a stopped one, like the other container-level
  tools), and the Odoo image must ship `psql` (the official ones do). An
  ephemeral client container was considered as a fallback for those two cases
  and not taken — it would add a second, rarely exercised transport plus image
  and cleanup concerns for something a clear error handles; there is
  deliberately **no** fallback to the DB container. Queries the server composes
  itself from fixed text and validated values (module-state checks, the
  module listing, Stack plans) keep running next to PostgreSQL, so they still
  work on a stopped or legacy environment; that path refuses a leading
  backslash and is not reachable with caller-written text. What the boundary
  rests on from here is the superuser's **password** (network auth is always
  password-based): an install initialised before `#74` with the published
  default should rotate it, since any tenant container can reach the server.

- (2026-11-19) — **`reset_admin_password` stops trusting the tenant's container
  and stops using the superuser.** It asked a `python3` *inside the
  environment's Odoo container* for the passlib hash, pasted that output into
  an `UPDATE res_users ...` string, and ran it with `psql -U <superuser>` in
  the shared PostgreSQL container. The Odoo container is the tenant's (root via
  `run_odoo_command`), so "the hash" was tenant-written SQL executed as the
  cluster superuser: `x' WHERE false; ALTER ROLE <own role> SUPERUSER; --`.
  Both halves were wrong independently, so both changed. The hash is now
  computed **in the Oduflow process** (`odoo_ops.hash_odoo_password`:
  `hashlib.pbkdf2_hmac` + passlib's adapted base64, emitting the
  `$pbkdf2-sha512$<rounds>$<salt>$<checksum>` string Odoo's `CryptContext`
  reads; 600,000 rounds, the value Odoo 15–20 hard-code, so Odoo does not
  rewrite it on first login) — passlib was not added as a dependency for one
  KDF call the standard library already has. And the `UPDATE` runs as the
  environment's **scoped role** through the server-built-query path
  (`run_internal_db_query`), after the hash is matched against its exact
  shape. Running as the scoped role matters even with a trustworthy hash: the
  tenant owns `res_users`, and a trigger on it would otherwise fire with
  superuser rights. A legacy environment without a credentials file is
  **refused** rather than served with the superuser; it has to be recreated.
  The general rule this instance stands for: output of a tenant container is
  untrusted input, and the superuser should not touch tenant-owned objects.
  Fixed-SQL superuser reads of environment databases that predate this rule
  (the "Connect as" user picker, template manifest checks) remain to be moved
  to the scoped role.

- (2026-11-19) — **template dumps are no longer executed by the superuser, nor
  inside the shared PostgreSQL container.** "Template loads" sat on the
  Decision's list of administrative operations the superuser keeps, and
  `reload_template` ran every dump as it, in that container: `psql -U
  <superuser> -f` for plain SQL, `pg_restore --no-owner -U <superuser>` for
  archives. But a dump is tenant input — `import_template` fetches it from a
  URL, bucket or path the caller names, and `save_as_template` dumps a database
  whose tenant defines its functions. Plain SQL could simply say `ALTER ROLE
  ... SUPERUSER`, `COPY ... FROM PROGRAM` or `\!`; an archive needs one more
  step (a function in a CHECK constraint runs during `COPY`, as the restoring
  user) and gets to the same place. And `--no-owner` as the superuser left
  every restored view and SECURITY DEFINER function *owned* by the superuser
  in a database any role can connect to. So both the executing role and the
  resulting owner had to change, and the client had to leave the container
  for the same reason as in the SQL-console change above.
  A load now uses three roles. The superuser only prepares and hands over: it
  creates a one-off **loader** role (`oduflow_load_<random>`, no privileges
  beyond owning the new, empty template database and its `public` schema), and
  afterwards issues `REASSIGN OWNED` / `DROP OWNED` — utility statements that
  run no code from the database — and drops the loader. The loader executes
  the dump, from a throwaway **restore client** container (the cluster's own
  image, team network, no capabilities, dump mounted read-only, loader
  password in `PGPASSWORD`), so `\!` lands in a container holding nothing and
  `\c` to another role needs a password. The result is owned by a per-team
  **template owner** (`oduflow_tplown_<team>`): an ordinary role that can log
  in only through the local socket (no password, and `VALID UNTIL 'epoch'`
  because a role may set its own). Oduflow's own reads of a freshly loaded
  template (table count, version and module list) run as that owner, since a
  query over tenant-defined objects resolves the tenant's operators and
  functions with the caller's rights. Cloning works as before: `CREATE DATABASE
  ... TEMPLATE` and the per-object reassignment to the environment's role
  treat the template owner exactly like the superuser they used to find there.
  But the clone is the same tenant content, and two superuser steps right
  after it were reachable from a dump: the ownership-reassignment and
  signaling-sequence blocks compare catalog names with `LIKE`, and with
  `public` on the search path an operator `public.!~~(name, name)` planted by
  the dump is a *better* match than the built-in `(name, text)` one. Both
  blocks now start with `SET LOCAL search_path = pg_catalog` and name what
  they touch with its schema; the create-time module-version check reads the
  clone as the environment's role. (That is the "template manifest checks"
  item from the previous entry; the "Connect as" user picker is still open.)
  Alternatives weighed: *filtering the dump* (backslash lines, statement
  denylist) — rejected as the only defence for the same reason as before, and
  useless against archives; *reassigning the result to the superuser* to keep
  the old end state — rejected, it is the view/SECURITY DEFINER hole again;
  *a role per template* — rejected for its lifecycle (rename, delete while
  clones still hold objects of it, scheduled generations) when a per-team
  owner plus a per-load loader gives the same confinement with nothing to
  clean up; *the loader's client inside the PostgreSQL container* — would have
  kept the in-place read through `pg_exchange`, but trust auth there makes
  the role a suggestion for `psql`, and one transport for both formats is
  simpler than two. *Extensions* needed no superuser step after all:
  `pg_trgm`, `unaccent` and the rest of what Odoo uses are *trusted*
  extensions on every supported PostgreSQL (15+), so the loader, as database
  owner, creates them itself and owns them (which `COMMENT ON EXTENSION` in a
  dump requires — a superuser-pre-created extension would make every archive
  restore fail on that comment, since `ALTER EXTENSION ... OWNER` does not
  exist). The accepted cost: a dump that needs an untrusted extension
  (PostGIS, `pg_stat_statements`) or anything else superuser-only now fails —
  loudly for archives, statement-by-statement for plain SQL — unless the
  operator's PostgreSQL image marks that extension trusted. Templates loaded
  before this change keep their superuser-owned objects until reloaded. Still
  open, same class: the other `pg_restore`-as-superuser paths (production
  snapshot restore, seeding a production from a dev database, the PostgreSQL
  upgrade's whole-cluster restore).

- (2026-11-19) — **the remaining `pg_restore`-as-superuser paths: production
  data and the PostgreSQL upgrade.** The three paths the previous entry left
  open were the same hole in the production cluster, where it matters most:
  `create_production(template_name | from_environment)` and
  `restore_production(from_environment)` dumped a *development* database and
  ran `pg_restore -U <superuser> --no-owner` inside the production PostgreSQL
  container, so a dev tenant's CHECK-constraint function ran as the production
  cluster's superuser; `restore_production(snapshot_id)` did the same with the
  production's own snapshot; and the major-version upgrade restored every
  production and service database with `pg_restore --create` as the superuser
  of the new cluster.
  *Production data* now reuses the restricted load, generalised: the restore
  client takes the cluster as a parameter (the production cluster is on every
  team network too), and the role is not a one-off loader but the
  **production's own scoped role** — it already exists, already has a
  password on record, and is the role the content will run as once Odoo
  starts, so restoring as it grants the content nothing new and needs no
  `REASSIGN OWNED` afterwards. That required one reordering:
  `create_production` used to seed first and create the role after; the role
  now comes first and owns the database (and its `public` schema) before any
  of the seed is loaded, and a restore creates its staged `…__restore`
  database with that owner. A side effect worth knowing: a restored
  production database used to stay owned by the superuser (only the objects
  inside were reassigned); it now belongs to the production's role like a
  freshly created one. The pinned-`search_path` reassignment and
  signaling-sequence blocks still run afterwards as a no-op safety net.
  `load_credentials`' superuser fallback is refused on these paths
  (`production_db_credentials`): with it, a production missing its
  credentials file would have had its content restored *and owned* by the
  superuser. Flags are otherwise unchanged (tolerant restore that fails on
  any error, privileges kept, `-j 2` for snapshots). The dump staged for the
  client lives under the team's data directory, because the Docker daemon —
  not the Oduflow process, which may itself be a container — resolves the
  mount path. Accepted cost, same as for templates: content only a superuser
  can create (an untrusted extension such as PostGIS, an event trigger, a
  `FOR ALL TABLES` publication) no longer loads, *including from snapshots
  taken earlier*; the remedy is an image that marks the extension trusted.
  Cluster PITR is physical and unaffected.
  *The upgrade* could not copy that design as is, because it must keep what
  `--create` restores (locale, tablespace, `ALTER DATABASE ... SET`, database
  privileges) and may not fail halfway for reasons the operator cannot fix.
  Splitting by section — schema as the superuser, data as the owner — was
  rejected: DDL alone executes tenant code (`CREATE AGGREGATE ... INITCOND`
  and a partition bound both run the input function of their type, and a
  domain's input function evaluates its CHECK constraints). So each database
  is restored in two passes: the superuser runs `pg_restore --create` filtered
  to a schema that cannot exist (`-n pg_…`; the `pg_` prefix is reserved),
  which leaves exactly the database-level entries, then makes the role *on
  Oduflow's records* the owner — the records, not `datdba`, since databases
  restored by older versions are superuser-owned — and that role restores the
  whole archive with `--no-owner --exit-on-error`. It connects through the
  cluster's own network namespace like before (loopback, where pg_hba trusts);
  that is acceptable here and was not for templates, because the client is
  `pg_restore` reading an archive Oduflow wrote itself, not `psql` reading a
  caller's SQL. Two refusals move to the plan, while every old cluster is
  intact: a carried database without a scoped role on record, and an extension
  of a carried database that the *new image* does not mark trusted (read from
  its control files). An automatic superuser fallback on failure was rejected
  — content could fail for its own role on purpose — in favour of an explicit
  operator switch, `ODUFLOW_PG_UPGRADE_SUPERUSER_RESTORE=<db,…>`, which also
  is the way out of a restore that stops on superuser-only content after the
  old cluster is gone. Ownership inside a carried database is no longer
  replayed: everything belongs to its role, which is what a managed database
  looked like anyway. Still open, same family but not `pg_restore`: `pg_dump`
  runs as the superuser against tenant databases (it pins `search_path` and
  relies on current minor releases), the "Connect as" user picker reads tenant
  tables as the superuser, and legacy environments without a credentials file
  still fall back to it for fixed reads.

- (2026-11-19) — **the superuser stops resolving names a tenant controls,
  and stops sharing databases with every role.** The previous entries moved
  *content* (dumps, restores) away from the superuser; what remained was the
  superuser *querying* inside tenant databases. Fixed SQL is not safe there:
  the tenant owns what the names in it mean. `res_users` can be a view over
  the tenant's function, an operator `public.!~~(name, name)` beats the
  built-in `LIKE` for a `name` column, and `ALTER DATABASE ... SET
  search_path` decides where any unqualified name is looked up — each of them
  tenant code run with the rights of whoever sent the query. Three rules came
  out of auditing every `psql` the server starts.
  *Tenant tables are read as the tenant's role.* The "Connect as" user picker
  (`list_env_users`) ran its `SELECT` over `res_users` / `res_partner` as the
  superuser for every environment; it now goes through
  `run_internal_db_query`, like the module checks, and its JSON answer is
  treated as data the tenant shaped (non-list answers and rows without a
  login are dropped). The superuser **fallback for legacy environments** on
  that path is gone, which withdraws a line from the SQL-console entry above
  ("so they still work on a stopped *or legacy* environment"): the module
  state checks and the installed-modules list refuse an environment without
  a credentials file with `MissingCredentialsError`. Degrading was weighed —
  skip the check, keep installing — and rejected for the same reason
  `reset_admin_password` refuses: such an environment has to be recreated
  anyway, and a verification that silently does not run is worse than an
  error. `install_odoo_modules` checks for the role *before* running Odoo, so
  a finished installation is never reported as a failure; `upgrade all`
  reads nothing and still works; `pull_and_apply`'s plan narrowing was
  best-effort already and its apply step reports the refusal.
  *What the superuser still runs, it runs with a pinned `search_path`.*
  `system_ops._exec_sql` now starts every superuser `psql` with a statement
  of its own, `SET search_path = pg_catalog, pg_temp`, ahead of the caller's
  (`psql -q -v ON_ERROR_STOP=1 … -c '<pin>' -tAc '<sql>'`). It is a
  statement rather than a connection option on purpose: database and role
  settings take effect while connecting, and a `SET` issued afterwards, in a
  session where nothing of the tenant's has run yet, replaces them whatever
  they say. Two `-c` rather
  than one prefixed string keeps `CREATE DATABASE` and friends outside a
  transaction block, which is what allowed pinning *every* superuser
  statement instead of only those inside tenant databases — the maintenance
  database is safe today because PostgreSQL 15+ lets nobody create in its
  `public`, and with the pin it no longer depends on that. What is left
  inside tenant databases is catalog-only or utility: the ownership and
  signaling-sequence blocks (pinned since the template entry),
  `ALTER SCHEMA public OWNER`, `REASSIGN OWNED` / `DROP OWNED`, and the
  upgrade plan's `pg_extension` read and bootstrap-database object count.
  Statements that run as a scoped role (`user=`) are deliberately not
  pinned; they are the tenant's and expect its schemas.
  *A database accepts its own role only.* PostgreSQL grants `CONNECT` and
  `TEMPORARY` on every new database to `PUBLIC`, pg_hba admits every role
  from every team network, and only service databases revoked it. So any
  tenant role could open another team's database: not its tables, but its
  catalog (table and column names, view and function source), whatever it
  leaves executable by `PUBLIC`, temporary tables against the other team's
  tablespace — and a session held on a *template* blocks every
  `CREATE DATABASE ... TEMPLATE` of it. `restrict_database_access` now issues
  `REVOKE ALL ... FROM PUBLIC` right after each `CREATE DATABASE`
  (environments, the staged copy of a scheduled sync, template loads,
  productions and their staged restores). It is repeated per database on
  purpose: a copy does **not** inherit its template's privileges. The owner's
  rights are not `PUBLIC`'s and follow the database through `ALTER DATABASE
  ... OWNER` and `REASSIGN OWNED`, so cloning, the loader hand-over and the
  template owner's reads work unchanged. For existing databases a startup
  reconcile (`restrict_managed_databases`, from `init_system`) was chosen
  over a one-shot step in `migrations.py`: migrations run before the
  PostgreSQL containers are ensured, so a cluster that is down at that
  moment (production hosting switched off, a held WAL guard) would be
  skipped for good, and a cluster PITR brings old privileges back long after
  a one-shot step is stamped. The reconcile is one catalog query per cluster
  once nothing is left. It touches only the `oduflow_` namespace, and it
  grants `CONNECT, TEMPORARY` to the role on record before revoking, in one
  transaction — a production restored by an older version is still *owned by
  the superuser* and its Odoo reached it through `PUBLIC` alone. Accepted
  cost: a role an operator added by hand loses access to environment
  databases until granted `CONNECT` explicitly.
  Still open, same family: `pg_dump` as the superuser, the superuser
  credential inside legacy tenant containers (`update_environment`,
  `reconfigure_production`), `init_template`'s build database, and event
  triggers in a template loaded as the superuser before the template entry
  above (they fire on the superuser's DDL in its clones until the template
  is reloaded).

- (2026-11-19) — **the superuser's password stops being handed to
  containers, and the backward-compatible fallback from the Decision is
  withdrawn.** "Old environments without a credentials file fall back to the
  global user" meant, concretely, that `update_environment` recreated such
  an environment's container with `USER` / `PASSWORD` set to the cluster
  superuser, and `reconfigure_production` did the same for a production
  whose credentials file was gone. A tenant is root in its Odoo container,
  and from there every connection is a network connection authenticated by
  password — so that one variable undid, for the tenants concerned, every
  boundary the entries above draw. Refusing to recreate was weighed and
  rejected: it would have left "recreate the environment", which rebuilds
  the database from its template, as the only remedy, for environments
  that are by definition the oldest and most lived-in.
  Instead the recreate **provisions the role the environment never had**
  (`system_ops.provision_scoped_role`): `REVOKE ... FROM PUBLIC`, create the
  role, make it the owner of the database, hand over everything inside with
  the pinned `reassign_db_ownership`, and only then write the credentials
  file — last, so that a failure leaves no record of a role that does not
  own its database and a retry starts over (every statement is idempotent).
  The end state is the one `create_environment` produces; the signaling
  sequences are *not* dropped as after a clone, because that fixup exists
  for sequences the new role could not see, and here it owns them.
  *Where* it runs was decided by failure, not convenience: between the stop
  of the old container and its removal. Before the stop, the ownership
  block would take an exclusive lock on every relation under a live Odoo;
  after the removal, a failure would leave the environment with no
  container and no way to describe one (its configuration lives in the
  container's labels). In between, a failure restarts the old container if
  it was running and raises — the old container is the state the operator
  already had, and no *new* container is created with the superuser's login
  under any outcome. Every path that recreates goes through it
  (`update_environment`, `switch_branch`, the extra-checkout switch of
  `pull_and_apply`); `recreate_environment` is delete + create and always
  made a role.
  *Productions* never lacked a role by design, so "legacy" there means a
  lost file, and usually a container still running as the role. Two things
  follow. A missing record is **drift**, so a plain `reconfigure_production`
  repairs it instead of reporting a no-op — as is a container that an older
  version already recreated as the superuser, since what Odoo created
  meanwhile is the superuser's. And the running container's own `USER` /
  `PASSWORD` are **kept** when the name is the derived one: a fresh password
  would lock that container out the moment it is set, and it is the
  container a failed provisioning falls back to. (Not so for development
  environments, where `USER` / `PASSWORD` can be overridden by the tenant's
  own variables and are therefore not evidence of anything; they always get
  a new password.) The WAL guard is checked again right before the
  production goes down, and a held guard also keeps the fallback from
  restarting anything. `_container_spec` refuses the superuser as a last
  line, so the two callers cannot drift apart.
  Two smaller placements went the same way. `init_template`'s build
  container ran the operator-chosen image — which may be one a tenant built
  — as the superuser; it now gets a one-off role that owns the build
  database, reusing the template loader. And `load_credentials` itself no
  longer returns the superuser's password: its fallback names the role and
  nothing else, which turns "no caller may pass this on" from a convention
  into something the API cannot do. What still reads `Settings.db_password`
  is pinned by a test to four functions: the two that initialise a cluster,
  the upgrade's dump/restore helper, and the check below.
  *Existing containers* are the part a code change cannot reach: a
  container's environment is fixed at creation. An automatic repair on
  start was weighed against the reconcile that closes databases to
  `PUBLIC`, and rejected — that one is a catalog statement nobody notices,
  this one stops a container, rewrites the ownership of a database and
  recreates the container, with failure modes (an image that is gone, a
  failing dependency install) an operator has to see, and it would run for
  every old environment at once. So the start **reports**
  (`superuser_credential_holders`: environment and production containers
  whose `USER` and `PASSWORD` are the superuser's) and the next recreate
  repairs. `start_environment` and the restarts are left alone on purpose:
  they start a container that already holds the password and place nothing.
  The consequence is spelled out for operators rather than hidden: once the
  listed containers are recreated, the superuser's password has to be
  changed, because it has been readable there all along. That costs nothing
  in Oduflow itself, which reaches both clusters through their local
  sockets. The tools' refusals now name the real remedy —
  `update_environment`, which d408fb1 had to stop suggesting while it did
  not provision a role, or `reconfigure_production`.
  Still open, same family: `pg_dump` as the superuser, the `postgres:17`
  conversion helper, and event triggers in a template loaded as the
  superuser before the template entry above.

A later layer was added **beneath** the two custom-script tiers — Odoo's own
native neutralization, rather than re-implementing what Odoo already ships:

- (2026-07-07) — **run `odoo-bin neutralize` as the baseline sanitization layer.**
  Odoo 16+ ships a modular neutralization framework: each installed module carries
  a `data/neutralize.sql`, and `odoo-bin neutralize -d <db>` applies them all in
  one transaction — deactivating outgoing mail servers and crons (except
  autovacuum), disabling payment providers, scrubbing third-party connector
  credentials to `dummy`, neutralizing webhooks, and setting the
  `database.is_neutralized` flag. Oduflow now invokes it (`neutralize_environment`)
  as the **first** step of the sanitize gate, before the team/repo scripts, so the
  custom tiers become PII-anonymization *on top of* a database that Odoo itself has
  already made outbound-safe. The decisive design constraint is **`addons_path`
  completeness**: `get_neutralization_queries` silently skips (`suppress(FileNotFoundError)`)
  any module whose code isn't on the path, so the command is run **inside the
  serving container** — which sees the full base-image + repo + extra-addons path —
  and not at template-import time, where custom modules would be missing and quietly
  under-neutralized. It also runs **after `auto_install_modules`** (and before the
  post-install registry restart): Odoo's neutralize gathers SQL only from
  *already-installed* modules, so neutralizing earlier would skip an auto-installed
  module's `neutralize.sql` and leave the crons / payment providers / connector
  credentials it creates active. Two properties were deliberately *not* taken on: neutralization
  leaves `database.uuid` and `database.enterprise_code` **untouched** (Odoo regenerates
  the uuid via a separate `ir.config_parameter.init(force=True)` on copy, decoupled
  from neutralize), so the licensing phone-home stops only because its cron is
  disabled, not because the identity changed. Kept lean (no new config knob): it
  rides the existing `sanitize=True` + template-based gate. Failures are logged as
  warnings, never fatal — consistent with the best-effort stance of the tiers.

## History

- `cd0b9ec` (2026-02-24) — two-tier sanitization: drop hardcoded built-in queries;
  bundle `01_disable_mail.sql`; create `/etc/oduflow/odoo_sanitize/` on init; run
  system-wide then per-repo sanitization scripts; add `sanitize` param to
  `create_environment` (default True).
- `e4949b0` (2026-02-26) — per-environment PostgreSQL credentials: `env_credentials.py`,
  `_create_pg_role`/`_drop_pg_role`, env role as DB owner, global user kept for
  admin ops; tests/queries/sanitizer/SQL terminal use per-env creds; orphan-role
  cleanup; backward-compatible fallback.
- `31df320`, `c6878e4`, `5c7b737` (2026-02-26 → 02-27) — clone-time ownership
  fixups so a non-superuser env role owns the cloned DB (see Evolution).
- `4f729b1` (2026-03-02) — **delete** mail servers (`fetchmail_server`,
  `ir_mail_server`) instead of merely disabling them; disabling still let Odoo
  send in some cases.
- `#74` (2026-06-17) — auto-generate the shared PostgreSQL superuser password
  on first init; the bundled `oduflow.toml` ships without a hardcoded password and
  the bootstrap injects a random secret into the generated config (see Evolution).
- (2026-07-07) — **native neutralization baseline:** `neutralize_environment` runs
  `odoo-bin neutralize` inside the serving container as the first step of the
  sanitize gate, ahead of the team/repo custom-script tiers (see Evolution).
- (2026-11-19) — **SQL console and `run_db_query` run `psql` in the environment's
  Odoo container** (`odoo_ops.ScopedPsql`), not in the shared PostgreSQL
  container, closing the `\!` / `\c` escalation to cluster superuser (see
  Evolution).
- (2026-11-19) — **`reset_admin_password` hashes in the Oduflow process and
  updates as the scoped role**, closing an SQL injection into a superuser
  `psql` through the output of the tenant's Odoo container (see Evolution).
- (2026-11-19) — **template dumps load as a one-off non-superuser role from a
  throwaway client container** and end up owned by a per-team template owner
  (`system_ops.reload_template`, `_run_restore_client`), closing superuser
  code execution through `import_template` / `save_as_template` dumps (see
  Evolution).
- (2026-11-19) — **production data and the PostgreSQL upgrade restore as the
  database's own role** (`system_ops.restore_archive_as_role`,
  `production_ops.production_db_credentials`,
  `postgres_migration._restore_database`), closing superuser code execution in
  the production cluster through a seeded, promoted or snapshot-restored
  database, and in a new cluster through the upgrade (see Evolution).
- (2026-11-19) — **tenant tables are read as the tenant's role, every
  superuser statement pins `search_path`, and managed databases refuse
  `PUBLIC`** (`odoo_ops.list_env_users` / `run_internal_db_query`,
  `system_ops._superuser_psql`, `restrict_database_access`,
  `restrict_managed_databases`), closing superuser code execution through
  names a tenant controls and cross-tenant connections to another team's
  database; legacy environments without a credentials file are refused on
  those paths (see Evolution).
- (2026-11-19) — **no Odoo container is created with the superuser's login**:
  an environment or production without a role of its own gets one when its
  container is recreated (`system_ops.provision_scoped_role`, from
  `env_ops.update_environment` and `production_ops.reconfigure_production`),
  the template build runs as a one-off role, `load_credentials` stops
  returning the superuser's password, and the start reports the containers
  that still hold it (`superuser_credential_holders`); this withdraws the
  Decision's fallback for old environments (see Evolution).
