"""Regression tests for dashboard static asset cache versioning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_DASHBOARD = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "dashboard.html"
)


def _js_function(source: str, name: str) -> str:
    """Extract one top-level dashboard function for a focused Node harness."""
    start = source.index(f"function {name}(")
    async_start = start - len("async ")
    if async_start >= 0 and source[async_start:start] == "async ":
        start = async_start
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function {name}")


def _run_node(source: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "-"],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _client(tmp_path) -> TestClient:
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_cleanup_requires_preview_image_opt_in_and_prevents_duplicate_confirm():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    source = dashboard.split("var cleanupPreview = null;", 1)[1].split(
        "async function loadStats()", 1
    )[0]
    harness = (
        "var cleanupPreview = null;"
        + source
        + r"""
const assert = require('node:assert/strict');
const fields = {};
global.document = {getElementById: id => fields[id] || (fields[id] = {
  disabled: false, checked: false, textContent: '', setAttribute() {}
})};
var refreshes = 0;
function loadEnvironments() { refreshes++; }
function loadStats() {}
function fmtBytes(n) { return n + ' B'; }
async function readResult(r) { return r; }
const image = {id: 'sha256:' + 'a'.repeat(64), tags: ['<test-image>'], size_bytes: 100};
const requests = [];
let finishCleanup;
let preview = {dry_run: true, images: [image]};
global.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  requests.push(body);
  if (!body.force) return {ok: true, result: preview};
  return new Promise(resolve => { finishCleanup = resolve; });
};
const noOrphans = {orphan_databases: [], orphan_workspaces: [], orphan_ports: [], orphan_roles: []};
(async () => {
  await confirmCleanup();
  assert.equal(requests.length, 0);
  await scanCleanup();
  assert.equal(requests[0].force, false);
  assert.equal(fields['cleanup-confirm'].disabled, true);
  assert.equal(fields['cleanup-images'].checked, false);
  assert.match(fields['cleanup-output'].textContent, /<test-image>/);
  fields['cleanup-images'].checked = true;
  updateCleanupConfirm();
  assert.equal(fields['cleanup-confirm'].disabled, false);
  const pending = confirmCleanup();
  await confirmCleanup();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1], {force: true, orphans: noOrphans, image_ids: [image.id]});
  assert.equal(fields['cleanup-confirm'].disabled, true);
  finishCleanup({ok: true, result: {dry_run: false, images: {
    removed: [], skipped: [{id: image.id, reason: 'Now used by a container.'}], errors: []
  }}});
  await pending;
  assert.match(fields['cleanup-output'].textContent, /Now used by a container/);
  assert.equal(fields['cleanup-confirm'].disabled, true);
  assert.equal(refreshes, 1);
  // A fresh scan is required after completion or a lost response.
  await confirmCleanup();
  assert.equal(requests.length, 2);
  // Confirm names exactly the reviewed orphans, so the server removes no others.
  preview = {dry_run: true, orphan_databases: ['oduflow_1_old'], images: [image]};
  await scanCleanup();
  assert.equal(fields['cleanup-images'].checked, false);
  assert.equal(fields['cleanup-confirm'].disabled, false);
  const orphanCleanup = confirmCleanup();
  assert.deepEqual(requests[3], {
    force: true, orphans: {...noOrphans, orphan_databases: ['oduflow_1_old']}, image_ids: []
  });
  finishCleanup({ok: true, result: {dry_run: false, orphan_databases: ['oduflow_1_old'], images: {
    removed: [], skipped: [], errors: []
  }}});
  await orphanCleanup;
  assert.match(fields['cleanup-output'].textContent, /oduflow_1_old/);
  process.stdout.write(JSON.stringify({ok: true}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    )
    assert _run_node(harness) == {"ok": True}


def test_chat_assets_share_one_positive_integer_cache_version(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")
    assert dashboard.status_code == 200

    # chat.js and acp-client.js are an interdependent pair; the dashboard holds
    # ONE shared cache version (CHAT_V) so a single bump busts both at once.
    match = re.search(r"var CHAT_V = '([1-9][0-9]*)'", dashboard.text)
    assert match is not None, "CHAT_V positive integer cache version not found"
    version = match.group(1)

    for filename in ("chat.js", "acp-client.js"):
        # Both assets must reference the shared CHAT_V, not a hardcoded number,
        # so they can never drift out of sync.
        assert re.search(
            rf"/static/{re.escape(filename)}\?v='\s*\+\s*CHAT_V", dashboard.text
        ), f"{filename} does not use the shared CHAT_V cache version"

        versioned = client.get(f"/static/{filename}?v={version}")
        unversioned = client.get(f"/static/{filename}")

        assert versioned.status_code == 200
        assert versioned.headers["content-type"].startswith("application/javascript")
        assert versioned.headers["cache-control"] == "public, max-age=86400"
        assert versioned.content == unversioned.content


def test_environment_metadata_shows_live_mount_path(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "env.local_path ? '<span>Live-mount:" in dashboard.text


def test_environment_name_is_copyable(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert (
        '<span class="branch-name copy-db" role="button" tabindex="0" '
        in dashboard.text
    )
    assert (
        'title="Copy environment name" aria-label="Copy environment name" '
        in dashboard.text
    )
    assert "copyToClipboard(\\'' + escAttr(env.branch)" in dashboard.text


def test_connect_modal_emphasizes_primary_action(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert (
        '<button class="btn" onclick="closeConnect()">Close</button>' in dashboard.text
    )
    assert (
        '<button class="btn-create" id="connect-submit" '
        'onclick="submitConnect()">Connect</button>' in dashboard.text
    )


def test_dashboard_uses_safe_json_response_reader(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert re.search(r"await\s+\w+\.json\(\)", dashboard.text) is None
    assert "return r.json()" not in dashboard.text
    assert "[408, 502, 503, 504, 524]" in dashboard.text
    assert "The operation may still be running on the server" in dashboard.text


def test_save_as_template_shows_elapsed_progress(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert 'id="new-tpl-progress" role="status" aria-live="polite"' in dashboard.text
    assert "Saving database and filestore…" in dashboard.text
    assert 'id="new-tpl-elapsed" aria-hidden="true"' in dashboard.text
    assert "elapsed.textContent = _fmtElapsed" in dashboard.text
    assert "progress.textContent = 'Saving database and filestore" not in dashboard.text
    assert "setBusy(branch, 'Saving template')" in dashboard.text


def test_dynamic_modals_use_the_show_class(tmp_path):
    """P-H15: only .modal-overlay.show has display:flex. Dialogs created with a
    'modal-overlay active' class never rendered — Create/Delete/Restore
    Production were dead and promptDialog's promise never settled."""
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert "modal-overlay active" not in dashboard.text
    assert "'modal-overlay show'" in dashboard.text
    # The dynamically created production modal is Escape-closable like the rest.
    assert "'create-prod-modal':" in dashboard.text


def test_feedback_modal_is_registered_for_escape_key(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert re.search(r"'feedback-modal':\s*closeFeedbackModal", dashboard.text)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_scoped_preview_filters_environment_data_in_the_browser():
    source = _DASHBOARD.read_text(encoding="utf-8")
    function = _js_function(source, "loadEnvironments")
    result = _run_node(
        f"""
        var API = '/api/environments';
        var SCOPED_ENV = 'feature/x';
        var cachedEnvs = [];
        var rendered = [];
        async function fetch() {{ return {{}}; }}
        async function readResult() {{
          return {{ok: true, environments: [
            {{env_name: 'feature/x'}}, {{env_name: 'other'}}
          ]}};
        }}
        function renderEnvironments(envs) {{ rendered = envs; }}
        function showToast() {{}}
        {function}
        loadEnvironments().then(function () {{
          console.log(JSON.stringify({{
            cached: cachedEnvs.map(function (env) {{ return env.env_name; }}),
            rendered: rendered.map(function (env) {{ return env.env_name; }})
          }}));
        }});
        """
    )

    assert result == {"cached": ["feature/x"], "rendered": ["feature/x"]}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_share_modal_ignores_stale_loads_and_blocks_unsafe_actions():
    source = _DASHBOARD.read_text(encoding="utf-8")
    names = (
        "_setShareActions",
        "_renderSharePending",
        "_renderShareError",
        "openShare",
        "renderShare",
        "_shareRequest",
        "loadShare",
        "shareCreate",
        "shareRotate",
    )
    functions = "\n".join(_js_function(source, name) for name in names)
    result = _run_node(
        f"""
        var elements = {{}};
        function element(id) {{
          if (!elements[id]) elements[id] = {{
            id: id, style: {{display: ''}}, disabled: false, value: 'stale',
            type: 'text', textContent: '', attributes: {{}},
            setAttribute: function (key, value) {{ this.attributes[key] = value; }}
          }};
          return elements[id];
        }}
        var document = {{getElementById: element}};
        var API = '/api/environments';
        var _shareBranch = '';
        var _shareRequestGeneration = 0;
        var _shareBusy = false;
        var _shareResult = null;
        var pending = [];
        function fetch(url) {{
          return new Promise(function (resolve) {{ pending.push({{url: url, resolve: resolve}}); }});
        }}
        async function readResult(response) {{ return response; }}
        function showModal() {{}}
        function showToast() {{}}
        function formatDate(value) {{ return value; }}
        {functions}

        (async function () {{
          var first = openShare('first');
          var loading = {{
            status: element('share-loading').style.display,
            create: element('share-create').style.display,
            rotate: element('share-rotate').style.display,
            revoke: element('share-revoke').style.display
          }};
          var second = openShare('second');
          pending[1].resolve({{ok: true, result: {{shared: false, url: null}}}});
          await second;
          pending[0].resolve({{
            ok: true,
            result: {{shared: true, url: 'https://stale.invalid', created_at: 'old'}}
          }});
          await first;

          var beforeCreateRequests = pending.length;
          var create = shareCreate();
          var duplicate = shareCreate();
          var mutation = {{
            disabled: element('share-create').disabled,
            requestCount: pending.length - beforeCreateRequests,
            duplicateIgnored: duplicate === undefined
          }};
          pending[2].resolve({{ok: false, error: 'failed'}});
          await create;
          await shareRotate();

          console.log(JSON.stringify({{
            loading: loading,
            branch: _shareBranch,
            url: element('share-url').value,
            createDisplay: element('share-create').style.display,
            createDisabledAfterError: element('share-create').disabled,
            rotateDisplay: element('share-rotate').style.display,
            requestCountAfterInvalidRotate: pending.length,
            mutation: mutation
          }}));
        }})();
        """
    )

    assert result["loading"] == {
        "status": "",
        "create": "none",
        "rotate": "none",
        "revoke": "none",
    }
    assert result["branch"] == "second"
    assert result["url"] == ""
    assert result["createDisplay"] == ""
    assert result["createDisabledAfterError"] is False
    assert result["rotateDisplay"] == "none"
    assert result["requestCountAfterInvalidRotate"] == 3
    assert result["mutation"] == {
        "disabled": True,
        "requestCount": 1,
        "duplicateIgnored": True,
    }


def test_share_modal_discloses_agent_chat_workspace_access(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert "Agent Chat runs in the team's shared coder container" in dashboard.text
    assert "may read other team checkouts and Git credentials" in dashboard.text


def test_dashboard_accepts_opencode_default_and_labels_it(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert "data.default === 'opencode'" in dashboard.text
    assert "(agentType === 'opencode' ? 'OpenCode' : 'Claude')" in dashboard.text


def test_agent_chat_sits_next_to_connect_as(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    connect = dashboard.index('<button class="btn btn-connect" title="Log in as a user')
    chat = dashboard.index(
        '<button class="btn btn-chat" title="Structured chat with the coding agent'
    )
    sync = dashboard.index('<button class="btn btn-sync"', connect)
    # Agent Chat sits right of Connect As, ahead of the routine actions...
    assert connect < chat < sync
    assert ">Connect As</button>" in dashboard[connect:chat]
    # ...and is no longer duplicated inside the More menu.
    assert 'role="menuitem" title="Structured chat' not in dashboard
    # Both stay plain outline actions (DESIGN.md: one primary per view).
    assert ".btn-chat:hover, .btn-chat:focus-visible {" in dashboard
    assert ".btn-main" not in dashboard


def test_minimized_window_dock_has_group_semantics_and_restores_focus(tmp_path):
    dashboard = _client(tmp_path).get("/").text
    assert 'id="min-dock" role="group"' in dashboard

    # closeMinimized must capture returnFocus BEFORE calling closer() (otherwise
    # closer() would tear down the chip and the captured element would already
    # be detached), and the focus call must happen AFTER closer() (so the
    # trigger element is still in the DOM). Asserting on the literal text does
    # not catch a reorder; pin down the order of the three statements.
    match = re.search(
        r"function closeMinimized\(id\) \{(.*?)\}\s*$",
        dashboard,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "closeMinimized function not found in dashboard"
    body = match.group(1)
    capture_at = body.find("returnFocus = _minimized")
    closer_at = body.find("closer()")
    focus_at = body.find("returnFocus.focus()")
    assert 0 <= capture_at < closer_at < focus_at, (
        f"closeMinimized statement order is wrong: "
        f"capture={capture_at}, closer={closer_at}, focus={focus_at}"
    )


def test_odoo_sh_import_exposes_best_effort_addon_policy(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    assert 'id="import-best-effort" disabled' in dashboard
    assert "Continue if some addons are unavailable" in dashboard
    assert (
        "addon_error_policy: document.getElementById('import-best-effort').checked "
        "? 'best_effort' : 'strict'" in dashboard
    )


def test_templates_tab_exposes_pull_import_from_odoo(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    assert 'onclick="openImportFromOdooModal()">Import from Odoo</button>' in dashboard
    assert 'id="import-from-odoo-modal"' in dashboard
    assert 'id="pull-import-url"' in dashboard
    assert 'id="pull-import-master-pwd"' in dashboard
    assert 'id="pull-import-db-name"' in dashboard
    assert 'id="pull-import-tpl-name"' in dashboard
    assert 'id="pull-import-without-filestore"' in dashboard
    assert "API_TEMPLATES + '/import-from-odoo'" in dashboard
    assert "HTTP URLs are allowed" in dashboard
    assert re.search(r"'import-from-odoo-modal':\s*closeImportFromOdooModal", dashboard)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_upgrade_module_picker_discards_stale_environment_responses():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "openUpgradeModules",
            "_upgradeModulesRequestIsCurrent",
            "closeUpgradeModules",
            "_disableUpgradePickerActions",
        )
    )
    harness = (
        functions
        + r"""
var API = '/api/environments';
var _upgradeModulesEnv = null;
var _upgradeModules = [];
var _upgradeSelected = new Set();
var _upgradeModulesRequest = 0;
var modalShown = false;
var pending = {};
var renders = [];
var elements = {
  'upgrade-modules-title': {textContent: ''},
  'upgrade-modules-filter': {value: '', focus: function () {}},
  'upgrade-modules-list': {innerHTML: ''},
  'upgrade-modules-count': {textContent: ''},
  'upgrade-modules-shown': {textContent: ''},
  'upgrade-modules-select-all': {disabled: false},
  'upgrade-modules-clear': {disabled: false}
};
var document = {getElementById: function (id) { return elements[id]; }};
function showModal() { modalShown = true; }
function hideModal() { modalShown = false; }
function _modalShown() { return modalShown; }
function fetch(url) {
  return new Promise(function (resolve) { pending[url] = resolve; });
}
async function readResult(value) { return value; }
function renderUpgradeModules() {
  renders.push({
    env: _upgradeModulesEnv,
    modules: _upgradeModules.map(function (item) { return item.name; })
  });
}

(async function () {
  var first = openUpgradeModules('alpha');
  closeUpgradeModules();
  var second = openUpgradeModules('beta');

  pending['/api/environments/beta/modules']({
    ok: true,
    modules: [{name: 'beta_module', version: '1'}]
  });
  await second;
  pending['/api/environments/alpha/modules']({
    ok: true,
    modules: [{name: 'alpha_module', version: '1'}]
  });
  await first;

  process.stdout.write(JSON.stringify({
    env: _upgradeModulesEnv,
    modules: _upgradeModules.map(function (item) { return item.name; }),
    renders: renders
  }));
})();
"""
    )

    result = _run_node(harness)

    assert result == {
        "env": "beta",
        "modules": ["beta_module"],
        "renders": [{"env": "beta", "modules": ["beta_module"]}],
    }


def test_upgrade_picker_offers_select_all_and_clear(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    assert 'id="upgrade-modules-select-all"' in dashboard
    assert 'id="upgrade-modules-clear"' in dashboard
    assert "upgrades them with <code>odoo -u all</code>" in dashboard


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_upgrade_picker_sends_odoo_all_only_for_a_whole_selection():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "selectAllUpgradeModules",
            "clearUpgradeModules",
            "_upgradeSelectionIsEverything",
            "toggleUpgradeModule",
            "updateUpgradeCount",
            "confirmUpgradeModules",
        )
    )
    harness = (
        functions
        + r"""
var _upgradeModulesEnv = 'feature-x';
var _upgradeModules = [{name: 'base'}, {name: 'sale'}, {name: 'crm'}];
var _upgradeSelected = new Set();
var elements = {
  'upgrade-modules-count': {textContent: ''},
  'upgrade-modules-select-all': {disabled: false},
  'upgrade-modules-clear': {disabled: false}
};
var document = {getElementById: function (id) { return elements[id]; }};
var applied = [];
var toasts = [];
function renderUpgradeModules() { updateUpgradeCount(); }
function showToast(message) { toasts.push(message); }
function closeUpgradeModules() {}
async function applyModules(branch, action, modules) {
  applied.push({branch: branch, action: action, modules: modules});
}

(async function () {
  selectAllUpgradeModules();
  var whole = {
    count: elements['upgrade-modules-count'].textContent,
    selectAllDisabled: elements['upgrade-modules-select-all'].disabled,
    clearDisabled: elements['upgrade-modules-clear'].disabled
  };
  await confirmUpgradeModules();

  clearUpgradeModules();
  var cleared = {
    count: elements['upgrade-modules-count'].textContent,
    selectAllDisabled: elements['upgrade-modules-select-all'].disabled,
    clearDisabled: elements['upgrade-modules-clear'].disabled
  };

  toggleUpgradeModule({checked: true, value: 'sale'});
  await confirmUpgradeModules();

  process.stdout.write(JSON.stringify({
    whole: whole,
    cleared: cleared,
    partialCount: elements['upgrade-modules-count'].textContent,
    applied: applied,
    toasts: toasts
  }));
})();
"""
    )

    result = _run_node(harness)

    assert result == {
        "whole": {
            "count": "All 3 modules selected \u2014 runs odoo -u all",
            "selectAllDisabled": True,
            "clearDisabled": False,
        },
        "cleared": {
            "count": "No modules selected",
            "selectAllDisabled": False,
            "clearDisabled": True,
        },
        "partialCount": "1 module selected",
        # The whole list collapses to Odoo's own keyword; a partial selection
        # still travels as explicit module names.
        "applied": [
            {"branch": "feature-x", "action": "upgrade", "modules": "all"},
            {"branch": "feature-x", "action": "upgrade", "modules": "sale"},
        ],
        "toasts": [],
    }


def test_extra_addon_pickers_offer_select_all_and_clear(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    assert 'id="cr-extra-select-all"' in dashboard
    assert 'id="cr-extra-clear"' in dashboard
    assert 'id="tset-extra-select-all"' in dashboard
    assert 'id="tset-extra-clear"' in dashboard
    assert "setAllExtraRepos('cr-extra', true)" in dashboard
    assert "setAllExtraRepos('tset-extra', false)" in dashboard


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_extra_repo_bulk_actions_stay_inside_their_own_picker():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "_extraRepoRows",
            "updateExtraRepoStatus",
            "setAllExtraRepos",
            "setExtraRepoActionsEnabled",
        )
    )
    harness = (
        functions
        + r"""
var EXTRA_REPO_PICKERS = {
  'cr-extra': {list: 'cr-extra-addons-checkboxes', checkbox: '.cr-extra-cb'},
  'tset-extra': {list: 'tset-extra-addons', checkbox: '.tset-extra-cb'}
};
function row(checked, hidden) {
  var cb = {checked: checked, type: 'checkbox'};
  var r = {hidden: !!hidden, cb: cb, querySelector: function () { return cb; }};
  cb.closest = function () { return r; };
  return r;
}
// The third create-modal repo is checked and hidden by the name filter.
var crRows = [row(false), row(false), row(true, true)];
var tsetRows = [row(true)];
var lists = {
  '.cr-extra-cb': crRows.map(function (r) { return r.cb; }),
  '.tset-extra-cb': tsetRows.map(function (r) { return r.cb; })
};
var els = {
  'cr-extra-addons-checkboxes': {querySelectorAll: function () { return crRows; }},
  'cr-extra-select-all': {disabled: true},
  'cr-extra-clear': {disabled: true},
  'cr-extra-status': {hidden: true, textContent: ''}
};
var document = {
  querySelectorAll: function (selector) { return lists[selector] || []; },
  getElementById: function (id) { return els[id]; }
};

setAllExtraRepos('cr-extra', true);
var selected = crRows.map(function (r) { return r.cb.checked; });
setExtraRepoActionsEnabled('cr-extra', true);
var enabled = {
  selectAll: els['cr-extra-select-all'].disabled,
  clear: els['cr-extra-clear'].disabled
};
// The template picker's controls are absent from this harness: a modal that is
// not on screen must not break the one that is.
setExtraRepoActionsEnabled('tset-extra', true);
setAllExtraRepos('cr-extra', false);

process.stdout.write(JSON.stringify({
  selected: selected,
  cleared: crRows.map(function (r) { return r.cb.checked; }),
  untouched: tsetRows.map(function (r) { return r.cb.checked; }),
  enabled: enabled,
  status: els['cr-extra-status'].textContent,
  statusHidden: els['cr-extra-status'].hidden
}));
"""
    )

    result = _run_node(harness)

    assert result == {
        "selected": [True, True, True],
        # Clear leaves the hidden row checked, so the status line has to say so:
        # the repo is invisible but still mounted.
        "cleared": [False, False, True],
        "untouched": [True],
        "enabled": {"selectAll": False, "clear": False},
        "status": (
            "1 selected repo is hidden by the filter and still mounted; "
            "Select all and Clear only touch visible rows."
        ),
        "statusHidden": False,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_extra_repo_filter_and_default_branch():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    assert 'id="tset-extra-filter"' in dashboard
    assert 'id="cr-extra-default-branch"' in dashboard
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "escHtmlAttr",
            "_tsetBranchOptions",
            "extraRepoBranchChoices",
            "extraRepoBranchNames",
            "_extraRepoRows",
            "updateExtraRepoStatus",
            "filterExtraRepos",
            "revealExtraRepos",
            "initExtraRepoTools",
            "applyDefaultExtraBranch",
        )
    )
    harness = (
        functions
        + r"""
// The dashboard's esc() goes through a DOM node; the attribute escaper is an
// equivalent stand-in for these plain branch names.
var esc = escHtmlAttr;
var EXTRA_REPO_PICKERS = {'tset-extra': {list: 'tset-extra-addons', checkbox: '.tset-extra-cb'}};
var cachedExtraRepos = [
  {name: 'connect-addons', repo_url: 'https://example.test/connect.git',
   branches: ['17.0'], available_branches: ['17.0', 'feature-x']},
  {name: 'odusfera-addons', repo_url: 'https://example.test/odusfera.git',
   branches: [], available_branches: ['origin/17.0', '9.0']},
  {name: 'open-up', repo_url: 'https://example.test/OpenUpgrade.git',
   branches: ['16.0'], available_branches: []}
];
function row(name, url, branch) {
  var cb = {value: name, checked: false};
  var input = {value: branch};
  return {
    hidden: false,
    textContent: name + ' (' + url + ')',
    cb: cb,
    input: input,
    querySelector: function (sel) { return sel === '.branch-input' ? input : cb; }
  };
}
var rows = cachedExtraRepos.map(function (r) { return row(r.name, r.repo_url, ''); });
rows.push(row('legacy', 'missing', 'keep-me'));
// A repo the filter will hide, checked and therefore still mounted.
rows[0].cb.checked = true;
var els = {
  'tset-extra-addons': {querySelectorAll: function () { return rows; }},
  'tset-extra-filter': {value: 'stale'},
  'tset-extra-default-branch': {value: '', innerHTML: ''},
  'tset-extra-status': {hidden: true, textContent: ''}
};
var document = {getElementById: function (id) { return els[id]; }};

initExtraRepoTools('tset-extra');
var options = els['tset-extra-default-branch'].innerHTML;
var afterInit = {filter: els['tset-extra-filter'].value, hidden: rows.map(function (r) { return r.hidden; })};

els['tset-extra-filter'].value = 'UP';
filterExtraRepos('tset-extra');
var filtered = rows.map(function (r) { return r.hidden; });
var statusWhenSome = els['tset-extra-status'].textContent;

els['tset-extra-filter'].value = 'nothing-matches';
filterExtraRepos('tset-extra');
var statusWhenNone = els['tset-extra-status'].textContent;

// Filtered-out rows still receive the branch.
els['tset-extra-default-branch'].value = '17.0';
applyDefaultExtraBranch('tset-extra');
// One-shot: the control returns to its placeholder, so picking the same branch
// again is a fresh change event rather than a no-op.
var pickerAfterApply = els['tset-extra-default-branch'].value;
rows[0].input.value = 'hand-edited';
els['tset-extra-default-branch'].value = '17.0';
applyDefaultExtraBranch('tset-extra');

// Anything the form must act on is revealed again, filter and all.
revealExtraRepos('tset-extra');

process.stdout.write(JSON.stringify({
  options: options,
  afterInit: afterInit,
  filtered: filtered,
  statusWhenSome: statusWhenSome,
  statusWhenNone: statusWhenNone,
  pickerAfterApply: pickerAfterApply,
  revealed: rows.map(function (r) { return r.hidden; }),
  statusAfterReveal: {text: els['tset-extra-status'].textContent, hidden: els['tset-extra-status'].hidden},
  branches: rows.map(function (r) { return r.input.value; }),
  checked: rows.map(function (r) { return r.cb.checked; })
}));
"""
    )

    result = _run_node(harness)

    hidden_selection = (
        "1 selected repo is hidden by the filter and still mounted; "
        "Select all and Clear only touch visible rows."
    )
    assert result == {
        "options": (
            '<option value="">Set branch\u2026</option>'
            '<option value="9.0">9.0 (1 repo)</option>'
            '<option value="16.0">16.0 (1 repo)</option>'
            '<option value="17.0">17.0 (2 repos)</option>'
            '<option value="feature-x">feature-x (1 repo)</option>'
        ),
        "afterInit": {"filter": "", "hidden": [False, False, False, False]},
        "filtered": [True, True, False, True],
        "statusWhenSome": hidden_selection,
        "statusWhenNone": "No repos match the filter. " + hidden_selection,
        "pickerAfterApply": "",
        "revealed": [False, False, False, False],
        "statusAfterReveal": {"text": "", "hidden": True},
        # Only repos that have the branch get it; nothing gets ticked. The
        # second apply restores the branch edited away by hand.
        "branches": ["17.0", "17.0", "", "keep-me"],
        "checked": [True, False, False, False],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_create_modal_branch_suggestions_match_the_branch_picker():
    """Both suggestion lists offer mountable names, not raw remote refs."""
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "escAttr",
            "_tsetBranchOptions",
            "extraRepoBranchChoices",
            "extraRepoBranchNames",
            "extraRepoBranchDatalist",
        )
    )
    harness = (
        functions
        + r"""
// A repo cloned before on-demand registration lists refs/remotes/, so its
// downloaded branches read "origin/17.0" and include origin/HEAD.
var legacy = {name: 'legacy', branches: ['origin/17.0', 'origin/HEAD', '17.0'],
              available_branches: ['18.0']};
process.stdout.write(JSON.stringify({
  datalist: extraRepoBranchDatalist(legacy),
  picker: extraRepoBranchNames(legacy)
}));
"""
    )

    result = _run_node(harness)

    assert result == {
        "datalist": (
            '<datalist id="extra-branches-legacy">'
            '<option value="17.0"></option>'
            '<option value="18.0"></option>'
            "</datalist>"
        ),
        "picker": ["17.0", "18.0"],
    }


def test_branch_required_error_reveals_filtered_extra_repos():
    """An error naming repos must not leave any of them behind the filter."""
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    errors = [
        m.start()
        for m in re.finditer(r"Branch is required for extra addon\(s\)", dashboard)
    ]
    assert len(errors) == 2  # create modal + template settings
    for start in errors:
        assert "revealExtraRepos(" in dashboard[start - 300 : start]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_template_settings_form_round_trips_attribute_and_prototype_key_values():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "escHtmlAttr",
            "parseEnvLines",
            "_tsetNormalizeExtras",
            "_tsetCollectForm",
        )
    )
    harness = (
        functions
        + r"""
var fields = {
  'template-settings-error': {style: {}},
  'tset-image': {value: 'odoo:19.0'},
  'tset-repo': {value: 'https://example.test/addons.git'},
  'tset-git-user': {value: "O'Reilly"},
  'tset-auto-install': {value: ''},
  'tset-env-vars': {value: '__proto__=polluted\nWORKERS=2', readOnly: false},
  'tset-local-path': {value: ''},
  'tset-overlay': {value: 'auto'}
};
function checkbox(value, branch) {
  return {
    value: value,
    closest: function (selector) {
      if (selector !== '.check-item') throw new Error('Unexpected row selector');
      return {
        querySelector: function (inputSelector) {
          if (inputSelector !== '.tset-extra-branch') {
            throw new Error('Unexpected input selector');
          }
          return {value: branch};
        }
      };
    }
  };
}
global.document = {
  querySelectorAll: function () {
    return [
      checkbox('__proto__', "feature'quote"),
      checkbox('missing"repo\\name', 'release\\candidate')
    ];
  },
  querySelector: function () {
    throw new Error('Repository names must not be interpolated into selectors');
  },
  getElementById: function (id) { return fields[id]; }
};
var _templateSettingsMeta = JSON.parse(
  '{"__proto__":{"keep":true},"custom":{"preserved":true}}'
);
var normalized = _tsetNormalizeExtras(JSON.parse(
  '{"__proto__":"main","constructor":"stable"}'
));
var collected = _tsetCollectForm();
process.stdout.write(JSON.stringify({
  escaped: escHtmlAttr("feature'quote&\"<>"),
  normalized: normalized,
  collected: collected
}));
"""
    )

    assert _run_node(harness) == {
        "escaped": "feature'quote&amp;&quot;&lt;&gt;",
        "normalized": {"__proto__": "main", "constructor": "stable"},
        "collected": {
            "__proto__": {"keep": True},
            "custom": {"preserved": True},
            "odoo_image": "odoo:19.0",
            "repo_url": "https://example.test/addons.git",
            "git_user": "O'Reilly",
            "auto_install_modules": "",
            "env_vars": {"__proto__": "polluted", "WORKERS": "2"},
            "extra_addons": {
                "__proto__": "feature'quote",
                'missing"repo\\name': "release\\candidate",
            },
            "use_overlay": None,
        },
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_create_template_env_vars_refresh_and_preserve_multiline_inheritance():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in ("parseEnvLines", "formatEnvLines", "fillCreateTemplateEnvVars")
    )
    harness = (
        functions
        + r"""
var CREATE_ENV_HINT = 'Environment variables';
var fields = {
  'cr-env-vars': {value: ''},
  'cr-env-vars-hint': {textContent: ''}
};
global.document = {
  getElementById: function (id) { return fields[id]; }
};
fillCreateTemplateEnvVars({env_vars: {TOKEN: 'customer-a', WORKERS: '2'}});
var first = fields['cr-env-vars'].value;
fields['cr-env-vars'].value = 'TOKEN=manual-edit';
fillCreateTemplateEnvVars({env_vars: {TOKEN: 'customer-b', LIMIT: '600'}});
var switched = fields['cr-env-vars'].value;
fillCreateTemplateEnvVars({env_vars: {
  CERT: '-----BEGIN-----\nbase64=payload\n-----END-----',
  WORKERS: '4'
}});
process.stdout.write(JSON.stringify({
  first: first,
  switched: switched,
  multilineField: fields['cr-env-vars'].value,
  multilineParsed: parseEnvLines(fields['cr-env-vars'].value),
  multilineHint: fields['cr-env-vars-hint'].textContent
}));
"""
    )

    assert _run_node(harness) == {
        "first": "TOKEN=customer-a\nWORKERS=2",
        "switched": "TOKEN=customer-b\nLIMIT=600",
        "multilineField": "WORKERS=4",
        "multilineParsed": {"WORKERS": "4"},
        "multilineHint": (
            "Environment variables 1 multiline template value is inherited "
            "unchanged and omitted here."
        ),
    }


def test_chat_markdown_is_sanitized_with_vendored_dompurify(tmp_path):
    """Regression for the bypassable hand-rolled sanitizer (entity-encoded
    scheme whitespace like ``java&Tab;script:``, SVG ``xlink:href``): marked's
    raw-HTML output must go through the vendored DOMPurify with the HTML-only
    profile, keep a plain-text fallback when DOMPurify is unavailable, and the
    dashboard must load purify.min.js before chat.js. (Static wiring checks:
    the repo has no DOM test harness — jsdom is not available — so a
    behavioral render test cannot run here.)"""
    chat = (_DASHBOARD.parent / "static" / "chat.js").read_text(encoding="utf-8")

    # marked output is sanitized by DOMPurify, HTML profile only (no
    # SVG/MathML — kills xlink:href), with style/form forbidden.
    assert "window.DOMPurify.sanitize(html," in chat
    assert "USE_PROFILES: { html: true }" in chat
    assert "FORBID_TAGS: ['style', 'form']" in chat
    # Missing-DOMPurify (or marked failure) fallback: plain escaped text, never
    # unsanitized HTML.
    assert "if (html == null || !window.DOMPurify)" in chat
    assert "d.textContent = text;" in chat
    # Every kept anchor is forced external-safe.
    assert "afterSanitizeAttributes" in chat
    assert "node.setAttribute('rel', 'noopener noreferrer');" in chat

    client = _client(tmp_path)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    # purify.min.js loads before chat.js (renderMarkdown needs it at parse
    # time), and the vendored asset is actually served with its license intact.
    purify_at = dashboard.text.index("loadScript('/static/purify.min.js')")
    chat_at = dashboard.text.index("/static/chat.js?v=")
    assert purify_at < chat_at
    purify = client.get("/static/purify.min.js")
    assert purify.status_code == 200
    assert b"DOMPurify" in purify.content
    assert b"@license DOMPurify" in purify.content


def test_prompt_dialog_escape_is_handled_on_the_overlay(tmp_path):
    """The typed-confirmation prompt (Delete/Restore Production) has no id, so
    the global MODAL_CLOSERS Escape handler cannot close it. Escape must be
    handled on the overlay itself — the input-only listener stopped working as
    soon as focus moved to the Cancel/confirm buttons."""
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert re.search(
        r"overlay\.addEventListener\('keydown',\s*function\(e\)\s*\{\s*"
        r"if \(e\.key === 'Escape'\) \{ e\.stopPropagation\(\); close\(null\); \}",
        dashboard.text,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_secret_editor_masks_values_and_keeps_update_and_replace_independent():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "clearSecretKeyResult",
            "canUpdateSecretKey",
            "secretKeyPath",
            "updateSecretValueMode",
            "validateSecretKey",
            "validateSecretValue",
            "openSecretModal",
            "closeSecretModal",
            "submitSecret",
        )
    )
    harness = (
        functions
        + r"""
const assert = require('node:assert/strict');
var API_SECRETS = '/api/secrets';
var secretTypes = {config: 'json', plain: 'text'};
var secretEditingName = '';
var secretSaving = '';
var fields = {};
var resultWrites = [];
var document = {getElementById: function(id) {
  if (id === 'secret-key-result' && !fields[id]) {
    var state = {hidden: true, text: ''};
    fields[id] = {
      get hidden() { return state.hidden; },
      set hidden(v) { state.hidden = v; resultWrites.push('hidden=' + v); },
      get textContent() { return state.text; },
      set textContent(v) { state.text = v; resultWrites.push('text=' + (v ? 'set' : 'cleared')); }
    };
  }
  if (!fields[id]) fields[id] = {
    value: '', checked: false, disabled: false, hidden: false, textContent: '',
    style: {}, setAttribute: function(k, v) { this[k] = v; }, focus: function() {}
  };
  return fields[id];
}};
function field(id) { return document.getElementById('secret-' + id); }
var visible = false;
function showModal() { visible = true; }
function hideModal() { visible = false; }
function showToast() {}
async function loadSecrets() {}
async function readResult(r) { return r; }
var requests = [];
var reply = {ok: true, result: {created: false, updated: true}};
async function fetch(url, options) {
  requests.push({url: url, body: JSON.parse(options.body)});
  return reply;
}
(async function() {
  openSecretModal('config');
  assert.equal(field('name').value, 'config');
  assert.equal(field('name').readOnly, true);
  assert.equal(field('hide-value').checked, true);
  assert.equal(field('hide-value').disabled, false);
  assert.equal(field('key-value').type, 'password');
  assert.equal(field('json-fields').hidden, false);
  assert.equal(field('submit').hidden, true);
  field('key').value = '/database/password';
  field('key-value').value = 'true';
  field('json-value').value = '{invalid replacement';
  validateSecretValue();
  assert.equal(field('update-submit').disabled, false);
  assert.equal(field('replace-submit').disabled, true);
  await submitSecret('update');
  assert.deepEqual(requests.pop(), {
    url: '/api/secrets/config/update-json',
    body: {path: '/database/password', value: 'true'}
  });
  assert.equal(visible, true);
  assert.equal(field('key-value').value, '');
  assert.equal(field('key').value, '/database/password');
  assert.equal(field('json-value').value, '{invalid replacement');
  assert.equal(field('key-result').hidden, false);
  assert.ok(field('key-result').textContent.startsWith('Key updated: /database/password.'));
  // aria-live regions only announce mutations while rendered: unhide, then write.
  assert.deepEqual(resultWrites.slice(-2), ['hidden=false', 'text=set']);
  assert.equal(field('update-submit').disabled, true);

  field('key').value = '  environment.OPENROUTER_API_KEY  ';
  field('key-value').value = 'new-private';
  validateSecretKey();
  assert.equal(field('update-submit').disabled, false);
  reply = {ok: true, result: {created: true, updated: false}};
  await submitSecret('update');
  assert.deepEqual(requests.pop(), {
    url: '/api/secrets/config/update-json',
    body: {path: 'environment.OPENROUTER_API_KEY', value: 'new-private'}
  });
  assert.equal(visible, true);
  assert.ok(field('key-result').textContent.startsWith('Key created: environment.OPENROUTER_API_KEY.'));
  assert.equal(field('key').value, 'environment.OPENROUTER_API_KEY');
  assert.ok(!field('key-result').textContent.includes('new-private'));
  assert.equal(field('key-value').value, '');
  clearSecretKeyResult();
  assert.equal(field('key-result').hidden, true);
  assert.equal(field('key-result').textContent, '');
  closeSecretModal();
  ['value', 'key', 'key-value', 'json-value'].forEach(id => assert.equal(field(id).value, ''));

  openSecretModal('config');
  field('key').value = '   ';
  field('key-value').value = 'must-not-submit';
  assert.equal(validateSecretKey(), false);
  field('key').value = 'invalid..path';
  field('key-value').value = 'must-not-submit';
  field('json-value').value = '{"number":123}';
  validateSecretValue();
  assert.equal(field('update-submit').disabled, true);
  assert.equal(field('replace-submit').disabled, false);
  await submitSecret();
  assert.deepEqual(requests.pop(), {
    url: '/api/secrets/config/set', body: {value: '{"number":123}', value_type: 'json'}
  });

  openSecretModal('plain');
  field('value').value = 'masked-draft';
  field('is-json').checked = true;
  updateSecretValueMode();
  assert.equal(field('json-value').value, '');
  assert.equal(field('key').disabled, true);
  assert.equal(field('update-submit').disabled, true);
  field('hide-value').checked = false;
  updateSecretValueMode();
  assert.equal(field('key-value').type, 'text');
  assert.equal(field('json-fields').hidden, false);
  field('is-json').checked = false;
  updateSecretValueMode();
  assert.equal(field('value').value, 'masked-draft');
  assert.equal(field('value').type, 'text');
  assert.equal(field('submit').hidden, false);
  await submitSecret();
  assert.deepEqual(requests.pop(), {
    url: '/api/secrets/plain/set', body: {value: 'masked-draft', value_type: 'text'}
  });

  openSecretModal();
  field('name').value = 'new-secret';
  field('is-json').checked = true;
  updateSecretValueMode();
  assert.equal(field('update-submit').disabled, true);
  assert.equal(field('replace-submit').textContent, 'Save JSON');
  closeSecretModal();

  openSecretModal('config');
  field('key').value = '/database/password';
  field('key-value').value = 'retry-me';
  reply = {ok: false, error: 'Rejected'};
  await submitSecret('update');
  assert.equal(visible, true);
  assert.equal(field('key-value').value, 'retry-me');
  assert.equal(field('error').textContent, 'Rejected');
  assert.equal(field('update-submit').disabled, false);
  assert.equal(secretSaving, '');
  requests = [];
  var finish;
  fetch = function(url, options) {
    requests.push({url: url, body: JSON.parse(options.body)});
    return new Promise(resolve => { finish = resolve; });
  };
  var pending = submitSecret('update');
  assert.equal(field('update-submit').textContent, 'Saving…');
  assert.equal(field('replace-submit').disabled, true);
  assert.equal(field('is-json').disabled, true);
  await submitSecret('update');
  closeSecretModal();
  assert.equal(visible, true);
  assert.equal(requests.length, 1);
  finish({ok: true, result: {created: false, updated: true}});
  await pending;
  assert.equal(visible, true);
  assert.ok(field('key-result').textContent.startsWith('Key updated:'));
  closeSecretModal();
  assert.equal(visible, false);
  assert.equal(field('key-result').hidden, true);
  process.stdout.write(JSON.stringify({ok: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    assert _run_node(harness) == {"ok": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_long_extra_repo_list_folds_and_filters_without_losing_state():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "extrasHtml",
            "_extrasQuery",
            "applyExtrasState",
            "measureExtras",
            "pruneExtrasState",
            "extrasFilterInput",
            "extrasFilterKey",
            "toggleExtras",
        )
    )
    harness = (
        functions
        + r"""
var _extrasOpen = Object.create(null);
var _extrasFilter = Object.create(null);
function getComputedStyle() { return {lineHeight: '20px'}; }
// Stand-in for the dashboard's DOM-based esc(): enough to inspect the markup.
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}
function classes() {
  var set = new Set();
  return {
    toggle: function (name, on) { if (on) set.add(name); else set.delete(name); },
    add: function (name) { set.add(name); },
    contains: function (name) { return set.has(name); }
  };
}
function makeBox(branch, repos, fullHeight) {
  var attrs = {'data-extras-for': branch};
  var items = repos.map(function (repo) {
    var attrs = {'data-search': (repo[0] + ' (' + repo[1] + ')').toLowerCase()};
    return {hidden: false, getAttribute: function (key) { return attrs[key]; }};
  });
  var nodes = {
    '.extras-filter': {value: ''},
    '.extras-toggle': {
      hidden: false, textContent: '', attrs: {},
      setAttribute: function (k, v) { this.attrs[k] = v; }
    },
    '.extras-match': {textContent: '', classList: classes()},
    '.extras-list': {hidden: false},
    // The wrapper is never clamped, so it is what carries the full height.
    '.extras-items': {
      scrollHeight: fullHeight,
      getClientRects: function () { return [1]; }
    }
  };
  var box = {
    classList: classes(),
    getAttribute: function (k) { return attrs[k]; },
    querySelector: function (sel) { return nodes[sel]; },
    querySelectorAll: function () { return items; }
  };
  nodes['.extras-filter'].closest = function () { return box; };
  nodes['.extras-toggle'].closest = function () { return box; };
  box.nodes = nodes;
  box.items = items;
  return box;
}
function snapshot(box) {
  return {
    overflow: box.classList.contains('has-overflow'),
    clamped: box.classList.contains('is-clamped'),
    visible: box.items.filter(function (i) { return !i.hidden; }).length,
    listHidden: box.nodes['.extras-list'].hidden,
    match: box.nodes['.extras-match'].textContent,
    toggleHidden: box.nodes['.extras-toggle'].hidden,
    toggle: box.nodes['.extras-toggle'].textContent,
    expanded: box.nodes['.extras-toggle'].attrs['aria-expanded']
  };
}

var out = {};
// The rendered item is "name (branch)" and both halves are searchable.
var markup = extrasHtml('work', {'web-OCA': '16.0'}, 3);
out.markup = {
  search: /data-search="([^"]*)"/.exec(markup)[1],
  wrapped: markup.indexOf('<div class="extras-items">') !== -1
};

var short = makeBox('short', [['web-OCA', '16.0'], ['queue-OCA', '16.0']], 40);
applyExtrasState(short);
out.short = snapshot(short);

var repos = [
  ['account-payment-OCA', '17.0'],
  ['account-invoicing-OCA', '17.0'],
  ['web-OCA', '16.0'],
  ['queue-OCA', '16.0']
];
var long = makeBox('work', repos, 200);
applyExtrasState(long);
out.folded = snapshot(long);

toggleExtras(long.nodes['.extras-toggle']);
out.unfolded = snapshot(long);
toggleExtras(long.nodes['.extras-toggle']);

var input = long.nodes['.extras-filter'];
input.value = 'Account';
extrasFilterInput(input);
out.filtered = snapshot(long);

input.value = '16.0';
extrasFilterInput(input);
out.byBranch = snapshot(long);

input.value = 'stock';
extrasFilterInput(input);
out.missing = snapshot(long);

// A re-render builds a fresh box: the stored filter is applied back to it.
var rerendered = makeBox('work', repos, 200);
applyExtrasState(rerendered);
out.rerenderValue = rerendered.nodes['.extras-filter'].value;

var stopped = false;
extrasFilterKey({
  key: 'Escape',
  preventDefault: function () {},
  stopPropagation: function () { stopped = true; }
}, input);
out.escape = {stopped: stopped, value: input.value, state: snapshot(long)};

// A deleted environment leaves no state behind: recreating it under the same
// name must not inherit the old fold/filter.
_extrasFilter['gone'] = 'web';
_extrasOpen['gone'] = true;
pruneExtrasState(['work', 'short']);
out.pruned = {
  filter: 'gone' in _extrasFilter,
  open: 'gone' in _extrasOpen,
  kept: 'work' in _extrasFilter
};

process.stdout.write(JSON.stringify(out));
"""
    )

    result = _run_node(harness)

    assert result["markup"]["search"] == "web-oca (16.0)"
    assert result["markup"]["wrapped"] is True
    assert result["short"]["overflow"] is False
    assert result["short"]["visible"] == 2
    assert result["folded"] == {
        "overflow": True,
        "clamped": True,
        "visible": 4,
        "listHidden": False,
        "match": "",
        "toggleHidden": False,
        "toggle": "Show all (4)",
        "expanded": "false",
    }
    assert result["unfolded"]["clamped"] is False
    assert result["unfolded"]["toggle"] == "Collapse"
    assert result["unfolded"]["expanded"] == "true"
    # Matches are shown in full even though the list is folded.
    assert result["filtered"]["visible"] == 2
    assert result["filtered"]["clamped"] is False
    assert result["filtered"]["match"] == "2 of 4"
    assert result["filtered"]["toggleHidden"] is True
    # A branch is as good a filter as a repo name.
    assert result["byBranch"]["visible"] == 2
    assert result["byBranch"]["match"] == "2 of 4"
    assert result["missing"]["visible"] == 0
    assert result["missing"]["listHidden"] is True
    assert result["missing"]["overflow"] is True
    assert "not included" in result["missing"]["match"]
    assert result["rerenderValue"] == "stock"
    assert result["escape"]["stopped"] is True
    assert result["escape"]["value"] == ""
    assert result["escape"]["state"]["visible"] == 4
    assert result["escape"]["state"]["clamped"] is True
    assert result["pruned"] == {"filter": False, "open": False, "kept": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_production_env_vars_forms():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in (
            "parseEnvLines",
            "formatEnvLines",
            "_parseProductionEnvLines",
            "_parseKvLines",
            "prodApplySettings",
            "submitCreateProduction",
            "_prodSettingsHtml",
            "_prodServerModeSelect",
            "esc",
            "escAttr",
            "escHtmlAttr",
        )
    )
    result = _run_node(
        functions
        + r"""
var fields = {};
global.document = {
  getElementById: id => fields[id],
  createElement: () => ({textContent: '', get innerHTML() {
    return this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }})
};
global.window = {_teamBaseDomain: 'example.com'};
var _prodSettingsDirty = {}, API_PRODUCTIONS = '/api/productions';
var sent = [], errors = [];
function showToast(msg, error) { if (error) errors.push(msg); }
async function confirmDialog() { return true; }
async function _prodPost(url, body) { sent.push(body); return null; }
['cp-name', 'cp-repo', 'cp-branch', 'cp-domain', 'cp-xdomains', 'cp-image',
 'cp-template', 'cp-fromenv', 'cp-auto', 'cp-mode', 'cp-set-envvars', 'cp-envvars',
 'ps-domain-erp', 'ps-xdomains-erp', 'ps-image-erp', 'ps-repo-erp',
 'ps-branch-erp', 'ps-gituser-erp', 'ps-extras-erp', 'ps-envvars-erp'].forEach(
 id => fields[id] = {value: '', defaultValue: '', checked: false, disabled: false});
fields['cp-name'].value = 'erp';
fields['cp-fromenv'].value = 'dev';
(async () => {
  await submitCreateProduction();
  fields['cp-set-envvars'].checked = true;
  await submitCreateProduction();
  fields['cp-envvars'].value = 'TOKEN=secret:api\nOPTIONS=a,b,X=c';
  await submitCreateProduction();
  fields['cp-envvars'].value = 'BROKEN';
  await submitCreateProduction();
  fields['cp-envvars'].value = 'KEY = value';
  await submitCreateProduction();
  fields['cp-envvars'].value = 'KEY= value';
  await submitCreateProduction();
  fields['cp-envvars'].value = 'OPTIONS=a b,c';
  await submitCreateProduction();
  var env = fields['ps-envvars-erp'];
  env.value = env.defaultValue = 'TOKEN=secret:api';
  await prodApplySettings('erp');
  env.value = '';
  await prodApplySettings('erp');
  env.value = 'TOKEN=secret:replacement';
  await prodApplySettings('erp');
  env.disabled = true;
  await prodApplySettings('erp');
  var rendered = _prodSettingsHtml({name: 'erp', env_vars: {
    TOKEN: 'secret:api', HTML: '</textarea><script>bad</script>'
  }});
  var multiline = _prodSettingsHtml({name: 'erp', env_vars: {CERT: 'a\nb'}});
  process.stdout.write(JSON.stringify({
    variables: sent.map(body => Object.hasOwn(body, 'env_vars') ? body.env_vars : null),
    errors: errors,
    referenceShown: rendered.includes('TOKEN=secret:api'),
    monoClass: rendered.includes('<textarea class="mono-input" id="ps-envvars-erp"'),
    escaped: rendered.includes('&lt;/textarea&gt;&lt;script&gt;bad&lt;/script&gt;'),
    multilineDisabled: /id="ps-envvars-erp"[^>]*disabled/.test(multiline)
  }));
})();
"""
    )
    assert result == {
        # A refused line sends nothing at all, so only the accepted submissions
        # (and the four Settings applies) appear here.
        "variables": [
            None,
            {},
            {"TOKEN": "secret:api", "OPTIONS": "a,b,X=c"},
            {"OPTIONS": "a b,c"},
            None,
            {},
            {"TOKEN": "secret:replacement"},
            None,
        ],
        # Spaces around the "=" are refused instead of ending up inside the
        # value, which would disable the textarea for good.
        "errors": [
            "Environment variables: each line must be KEY=VALUE, with no "
            'space around the "="'
        ]
        * 3,
        "referenceShown": True,
        "monoClass": True,
        "escaped": True,
        "multilineDisabled": True,
    }
    # .form-group textarea would otherwise override the class on font-family.
    assert (
        ".form-group textarea.mono-input { font-family: var(--font-mono);" in dashboard
    )
