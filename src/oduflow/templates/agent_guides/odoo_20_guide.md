# Odoo 20 Development Guide

> **Constraint:** write everything to Odoo 20.0 conventions. 20 removes several APIs that
> 19 only deprecated (`odoo.osv`, the old `read_group`, `ir.model.access` / `ir.rule`,
> server-side `t-esc`) and moves the web client to Owl 3. Read the access-rights, ORM
> and JavaScript sections before writing code.

Quick orientation for an agent about to author or refactor a module on 20.0.

## Runtime baseline

- Python **3.12–3.14** (the minimum went up from 3.10). The 3.12+ rules are no longer
  optional: `datetime.utcnow()` is deprecated (use `fields.Datetime.now()`), and
  `distutils` (gone in 3.12) or `cgi`, `cgitb`, `telnetlib`, `pipes`, `aifc`, `sunau`,
  `audioop` (gone in 3.13) are hard import errors.
- PostgreSQL **16+** (the minimum went up from 13).

## Access rights: `ir.access` replaces `ir.model.access` and `ir.rule`

Both old models are gone. A module that still ships `security/ir.model.access.csv` or
`<record model="ir.rule">` does not install: the registry fails to load with
`KeyError: 'ir.model.access'` (or `KeyError: 'ir.rule'`).

Access lives in one file, `security/ir.access.csv`, listed in the manifest `data`:

```csv
id,name,model_id,group_id/id,operation,domain
access_library_book_user,library.book user,library.book,base.group_user,cru,"[('create_uid', '=', user.id)]"
access_library_book_manager,library.book manager,library.book,library.group_manager,crud,
```

- `model_id` takes the model name (`library.book`), not the `model_library_book` XML ID.
- `operation` is a subset of `crud`: create, read, update (write), delete.
- A line **with** a group is a *permission*. A user's permissions for an operation are
  OR-ed together; an empty `domain` means all records. One line therefore replaces an
  old ACL line plus its group record rule.
- A line **without** a group is a *restriction*, the old global rule. Restrictions are
  AND-ed on top of the permissions, for everyone.
- `domain` is evaluated with `user`, `company_id`, `company_ids` and `time`, as record
  rules were.
- In code, check with `records.check_access("write")` (raises) or
  `records.has_access("write")` (returns a bool). `check_access_rights()` and
  `check_access_rule()` no longer exist.
- Odoo ships a converter (`odoo upgrade_code --script 19.4-00-ir-access`), but in build
  20.0-20261015 it crashed on a plain test module. Convert security files by hand.

## Models / ORM

- **`odoo.osv` is gone**: `from odoo.osv import expression` raises `ModuleNotFoundError`.
  Use `Domain`:

  ```python
  from odoo.fields import Domain

  domain = Domain("state", "=", "draft") & Domain("partner_id", "!=", False)
  either = Domain.OR([domain, Domain("user_id", "=", self.env.uid)])
  ```

- **`read_group()` has a new signature**: `read_group(domain, groupby=(), aggregates=(),
  having=(), offset=0, limit=None, order=None)`. It returns a list of tuples, with
  many2one values as ids. In server code, use `_read_group()` with the same arguments.
  It returns records instead of ids. The old `read_group(domain, fields, groupby,
  lazy=...)` call breaks.

  ```python
  for partner, total in self.env["sale.order"]._read_group(
      [("state", "=", "sale")], ["partner_id"], ["amount_total:sum"]
  ):
      ...
  ```

- **`group_operator=` is ignored.** Odoo only logs `unknown parameter 'group_operator'`,
  and the column loses its aggregation. Use `aggregator="sum"`.
- **ORM cache**: import the decorator from `odoo.api` (`@api.ormcache(...)`);
  `from odoo.tools import ormcache` is deprecated. `ormcache_context` is removed, so put
  the context values the result depends on into the key, e.g.
  `@api.ormcache("self.env.lang")`. `self.env.registry.clear_cache()` is removed: call
  `self.env.transaction.invalidate_ormcache()`.
- `_sql_constraints` is still ignored with a warning, as in 19. Declare
  `models.Constraint`, `models.Index` or `models.UniqueIndex` attributes instead:

  ```python
  _isbn_unique = models.Constraint("UNIQUE(isbn)", "ISBN must be unique.")
  ```

- `_rec_names_search` is a tuple: `_rec_names_search = ("name", "isbn")`.
- `self._cr`, `self._uid` and `self._context` are deprecated (since 19). Use
  `self.env.cr`, `self.env.uid` and `self.env.context`.
- Unchanged from 19: `@api.model_create_multi` is the only accepted `create()`
  override; there is no `name_get()`, so use `_compute_display_name()` with
  `@api.depends`; never declare a `display_name` field.

## Data files

- Binary fields loaded from a file use `type="bytes"`; `type="base64"` is deprecated:

  ```xml
  <field name="image_1920" type="bytes" file="library/static/img/book.png"/>
  ```

## Views

- `<list>` only, as in 19, including XPath targets (`//list`) and `view_mode`.
- Search-view date filters no longer accept `start_month`, `end_month`, `start_year` or
  `end_year`, and the view fails validation. Write `<filter name="date" date="date"/>`.
- Calendar views no longer accept `date_delay`. Use `date_stop`.
- **`t-esc` is removed from server-side QWeb.** It renders an empty element and only logs
  `Unknown directives or unused attributes: {'t-esc'}`. Use `t-out`.
- **`t-call` takes its parameters as attributes.** A `t-set` inside the `t-call` body no
  longer reaches the called template; it is silently lost. The body is still available
  to the callee as `t-out="0"`. A plain attribute is a Python expression, and a `.f`
  suffix makes a format string:

  ```xml
  <t t-call="library.book_card" book="book" title.f="Book {{ book.name }}">
      <p>Extra content, rendered by t-out="0" in the card</p>
  </t>
  ```

- **No FontAwesome.** Its CSS is no longer loaded, so `fa fa-*` icons render as nothing,
  and the `test_lint` icon check rejects `fa`/`fa-*` classes. Use Material Symbols names
  (listed in `web/icons.py`) through `data-icon`: `<i class="oi" data-icon="person"/>`.
  Only modifier classes remain (`oi-fw`, `oi-spin`, `oi-lg`, ...). View `icon="..."`
  attributes take the same names: `icon="edit_square"`, not `icon="fa-pencil"`.

## JavaScript / Owl 3

20 runs **Owl 3** (`3.0.0-alpha.49` in build 20.0-20261015). The Owl 2 hooks below are no
longer exported by `@odoo/owl`:

| Owl 2 | Owl 3 in Odoo 20 |
| --- | --- |
| `useState({...})`, `reactive({...})` | `proxy({...})` |
| `useRef("input")` + `t-ref="input"` | class field `input = signal.ref()` + `t-ref="this.input"`; read it with `this.input()` |
| `static props = {...}` | class field `props = useProps({ record: t.object(), size: t.string().optional() })` (`t` from `@odoo/owl`) |
| `useExternalListener(target, event, handler)` | `useListener(target, event, handler)` |
| `useEffect(fn, () => deps)` | `useLayoutEffect` from `@web/owl2/utils` keeps the Owl 2 behaviour |
| `t-esc` | `t-out` |

- Templates reach component members through `this.`: `t-on-click="this.onClick"`,
  `t-out="this.state.count"`, `t-if="this.props.record"`.
- Signals: `count = signal(0)`; read `this.count()`, write `this.count.set(1)`.
- Unchanged: `useService(...)`, `onWillStart`, `onMounted`, `onWillUnmount`.
- jQuery is gone from every bundle, and so is `publicWidget`. Website and portal code is
  an `Interaction` (`@web/public/interaction`) registered in
  `registry.category("public.interactions")`.
- `odoo upgrade_code --script owl3-migration` exists but is unreliable in build
  20.0-20261015: it emits directives such as `t-custom-ref` and skips some rewrites.
  Review its diff by hand, or port by hand.

## Misc

- Domains are lists or `Domain` objects, never strings.

## Removed / replaced — quick map

| Old | Use instead |
| --- | --- |
| `security/ir.model.access.csv`, `ir.rule` records | `security/ir.access.csv` (`ir.access`) |
| `check_access_rights()` / `check_access_rule()` | `check_access()` / `has_access()` |
| `odoo.osv.expression` | `odoo.fields.Domain` |
| `read_group(domain, fields, groupby, lazy=...)` | `read_group(domain, groupby, aggregates)` (ids) or `_read_group(...)` (records) |
| `group_operator=` | `aggregator=` |
| `odoo.tools.ormcache`, `ormcache_context` | `api.ormcache` with the context values in the key |
| `registry.clear_cache()` | `env.transaction.invalidate_ormcache()` |
| `type="base64" file="..."` | `type="bytes" file="..."` |
| `t-esc` (server QWeb and Owl) | `t-out` |
| `t-set` inside a `t-call` body | parameters as `t-call` attributes |
| `start_month` / `end_year` on date filters, calendar `date_delay` | plain `date` filter, `date_stop` |
| `fa fa-*` icons | `<i class="oi" data-icon="..."/>` |
| `useState` / `reactive` / `useRef` / `static props` / `useExternalListener` | `proxy` / `proxy` / `signal.ref()` / `useProps` / `useListener` |
| jQuery, `publicWidget` | `Interaction` in `public.interactions` |
| `_sql_constraints` | `models.Constraint` / `models.Index` |
| `name_get()` (removed in 18) | `_compute_display_name()` (+ `@api.depends`) |
| `<tree>` / `//tree` xpath | `<list>` / `//list` |

## Migrating a module to Odoo 20

- Odoo ships codemods for the 19 → 20 changes. Run one at a time with
  `odoo upgrade_code --script <name> --addons-path <your addons>`: `19.1-00-t-call`,
  `19.3-00-base64-in-xml`, `19.4-00-ormcache-on-transaction`,
  `19.5-00-tuple-rec_names_search`, `20.0-00-search-date-filters`, `owl3-migration`.
  In build 20.0-20261015 they also walk the core addons, and on a read-only install some
  stop with `PermissionError`, sometimes before they reach your files. Run them in a
  throwaway container and review every diff. The access files need the manual
  conversion described above.
- Module upgrade scripts live in `migrations/<manifest-version>/` (e.g.
  `migrations/20.0.1.1.0/`) and run when you bump the manifest `version`; the folder
  name must match it exactly, or the scripts are silently skipped.
- `pre-migration.py` runs *before* the ORM loads the new schema, so use raw SQL there.
  `post-migration.py` runs *after* it, so reshape data through the ORM there.
- Always early-return on a fresh install (`version` is `None`), and never commit the
  cursor yourself, because Odoo owns the transaction.

```python
def migrate(cr, version):
    if not version:  # fresh install, nothing to port
        return
    cr.execute("ALTER TABLE library_book RENAME COLUMN isbn_code TO isbn")
```
