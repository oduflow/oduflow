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
    assert "var CHAT_V = '7'" in dashboard.text


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
    assert "setAllExtraRepos('.cr-extra-cb', true)" in dashboard
    assert "setAllExtraRepos('.tset-extra-cb', false)" in dashboard


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_extra_repo_bulk_actions_stay_inside_their_own_picker():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in ("setAllExtraRepos", "setExtraRepoActionsEnabled")
    )
    harness = (
        functions
        + r"""
var lists = {
  '.cr-extra-cb': [{checked: false}, {checked: false}],
  '.tset-extra-cb': [{checked: true}]
};
var buttons = {
  'cr-extra-select-all': {disabled: true},
  'cr-extra-clear': {disabled: true}
};
var document = {
  querySelectorAll: function (selector) { return lists[selector] || []; },
  getElementById: function (id) { return buttons[id]; }
};

setAllExtraRepos('.cr-extra-cb', true);
var selected = lists['.cr-extra-cb'].map(function (cb) { return cb.checked; });
setExtraRepoActionsEnabled('cr-extra', true);
var enabled = {
  selectAll: buttons['cr-extra-select-all'].disabled,
  clear: buttons['cr-extra-clear'].disabled
};
// The template picker's buttons are absent from this harness: a modal that is
// not on screen must not break the one that is.
setExtraRepoActionsEnabled('tset-extra', true);
setAllExtraRepos('.cr-extra-cb', false);

process.stdout.write(JSON.stringify({
  selected: selected,
  cleared: lists['.cr-extra-cb'].map(function (cb) { return cb.checked; }),
  untouched: lists['.tset-extra-cb'].map(function (cb) { return cb.checked; }),
  enabled: enabled
}));
"""
    )

    result = _run_node(harness)

    assert result == {
        "selected": [True, True],
        "cleared": [False, False],
        "untouched": [True],
        "enabled": {"selectAll": False, "clear": False},
    }


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
