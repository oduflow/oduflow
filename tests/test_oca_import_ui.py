"""Exercise the OCA import dialog's state logic without GitHub or a server."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
_DASHBOARD = Path(__file__).parents[1] / "src/oduflow/templates/dashboard.html"


def _run(scenario):
    source = _DASHBOARD.read_text()
    source = source[source.index("// The import runs on the server") :]
    source = source[: source.index("async function loadExtraRepos()")]
    harness = """
      const assert = require('node:assert/strict');
      const elements = new Map();
      const document = {activeElement: null, getElementById(id) {
        if (!elements.has(id)) elements.set(id, {
          value: '', checked: false, textContent: '', style: {}, dataset: {}, handlers: {},
          contains() { return false; }, focus() {},
          addEventListener(name, fn) { this.handlers[name] = fn; }
        });
        return elements.get(id);
      }};
      const API_EXTRA_REPOS = '/api/extra-repos';
      let extraLoads = 0;
      async function readResult(r) { return r; }
      function isAmbiguousGatewayStatus(status) { return [502, 504].includes(status); }
      async function loadExtraRepos() { extraLoads++; }
      function showModal() {}
      function hideModal() {}
      function esc(s) { return String(s || ''); }
      function escHtmlAttr(s) { return String(s || ''); }
      const tick = () => new Promise(resolve => setImmediate(resolve));
      function cat(name, extra = {}) {
        return Object.assign({name, extra_name: 'oca-' + name,
          repo_url: 'https://github.com/OCA/' + name + '.git', description: '',
          stars: 0, archived: false, existing_as: '', name_taken: false}, extra);
      }
      function repo(name, extra = {}) {
        return Object.assign(cat(name), {branches: ['18.0', '19.0'], desired: ['18.0'],
          branchState: 'loaded', branchError: '', selected: true}, extra);
      }
      function setRows(rows) {
        rows.forEach((row, idx) => { row.idx = idx; });
        ocaRows = rows;
        ocaByName = new Map(rows.map(row => [row.name, row]));
      }
      function job(status, items, id = 'oca-1') { return {id, status, items}; }
      function item(name, status, error = '', extra = {}) {
        return Object.assign({name, extra_name: 'oca-' + name,
          repo_url: 'https://github.com/OCA/' + name + '.git',
          branches: ['18.0'], status, error}, extra);
      }
    """
    stubs = """
      let renders = 0, rowUpdates = 0, polls = 0;
      renderOcaRepos = function() { renders++; };
      updateOcaRow = function() { rowUpdates++; };
      updateOcaSummary = function() {};
      scheduleOcaPoll = function() { if (ocaRunning()) polls++; };
    """
    result = subprocess.run(
        ["node", "-"],
        input=harness
        + source
        + stubs
        + "(async () => {\n"
        + scenario
        + "\nconsole.log(JSON.stringify({ok: true}));\n})().catch(e => {console.error(e); process.exit(1);});",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_import_sends_each_rows_branches_and_blocks_missing_ones():
    _run("""
      let calls = [];
      fetch = async (url, options) => {
        calls.push(JSON.parse(options.body));
        return {ok: true, job: job('running', [])};
      };
      const row = repo('tools', {desired: ['18.0', '20.0']});
      setRows([row]);
      await startOcaImport(false);
      assert.equal(calls.length, 0);
      assert.deepEqual(ocaMissingBranches(row), ['20.0']);
      row.desired = ['18.0'];
      const web = repo('web', {desired: []});
      setRows([row, web]);
      assert.match(ocaRowStatus(web)[0], /first use/);
      await startOcaImport(false);
      assert.deepEqual(calls[0], {repos: [
        {name: 'tools', branches: ['18.0']}, {name: 'web', branches: []}
      ]});
      assert.equal(ocaRunning(), true);
      assert.equal(polls, 1);
      await startOcaImport(false); // a second click must not start another job
      assert.equal(calls.length, 1);
    """)


def test_reload_resumes_a_running_job_and_reconciles_when_it_finishes():
    _run("""
      let catalogue = {ok: true, stale: false, repos: [cat('web'), cat('tools')],
        job: job('running', [item('web', 'adding'), item('tools', 'queued')])};
      const finished = job('finished', [item('web', 'done'), item('tools', 'error', 'Clone failed')]);
      fetch = async url => url.endsWith('/oca') ? catalogue : {ok: true, job: finished};
      await loadOcaCatalog(); // a freshly loaded page: nothing selected locally
      assert.equal(ocaRunning(), true);
      assert.equal(polls, 1);
      assert.equal(ocaRowLocked(ocaByName.get('web')), true);
      assert.deepEqual(ocaRowStatus(ocaByName.get('tools')), ['Queued', false]);
      catalogue = {ok: true, stale: false, job: finished,
        repos: [cat('web', {existing_as: 'oca-web'}), cat('tools')]};
      await pollOcaJob();
      assert.equal(extraLoads, 1);
      assert.equal(ocaRunning(), false);
      assert.deepEqual(ocaRowStatus(ocaByName.get('web')), ['Already added as oca-web', false]);
      assert.deepEqual(ocaRowStatus(ocaByName.get('tools')), ['Import failed: Clone failed', true]);
      assert.equal(ocaRowLocked(ocaByName.get('tools')), false);
      assert.deepEqual(ocaFailedItems().map(i => i.name), ['tools']);
    """)


def test_retry_resends_failed_items_from_the_server_job():
    _run("""
      let calls = [];
      fetch = async (url, options) => {
        calls.push(JSON.parse(options.body));
        return {ok: true, job: job('running', [], 'oca-2')};
      };
      setRows(['web', 'tools', 'sale', 'taken'].map(name => repo(name, {selected: false})));
      ocaByName.get('taken').name_taken = true;
      applyOcaJob(job('finished', [
        item('web', 'error', 'x', {branches: []}),
        item('tools', 'error', 'y', {branches: ['17.0']}),
        item('sale', 'done'),
        item('taken', 'error', 'Name already in use: oca-taken'),
      ]), true);
      await startOcaImport(true);
      assert.deepEqual(calls, [{repos: [
        {name: 'web', branches: []}, {name: 'tools', branches: ['17.0']}
      ]}]);
    """)


def test_deleted_repository_can_be_selected_again():
    _run("""
      let catalogue = {ok: true, repos: [cat('web', {existing_as: 'oca-web'})],
        job: job('finished', [item('web', 'done')])};
      fetch = async () => catalogue;
      await loadOcaCatalog();
      const row = ocaByName.get('web');
      assert.equal(ocaRowLocked(row), true);
      catalogue = {ok: true, repos: [cat('web')], job: job('finished', [item('web', 'done')])};
      await loadOcaCatalog();
      assert.equal(ocaByName.get('web'), row);
      assert.equal(ocaRowLocked(row), false);
      assert.deepEqual(ocaRowStatus(row), ['', false]);
    """)


def test_reselecting_a_repository_applies_the_current_versions():
    _run("""
      fetch = async () => ({ok: true, branches: ['18.0', '19.0']});
      setRows([repo('web', {selected: false, desired: [], branches: null, branchState: ''})]);
      const row = ocaRows[0];
      const change = document.getElementById('oca-repos').handlers.change;
      const select = checked => change({target: {dataset: {row: '0', action: 'select'}, checked}});
      document.getElementById('oca-versions').value = '18.0';
      select(true);
      await tick();
      assert.deepEqual(row.desired, ['18.0']);
      select(false);
      document.getElementById('oca-versions').value = '19.0';
      applyOcaVersions(); // only reaches rows that are selected right now
      select(true);
      assert.deepEqual(row.desired, ['19.0']);
      assert.equal(row.branchState, 'loaded');
    """)


def test_async_updates_patch_rows_without_rebuilding_the_list():
    _run("""
      fetch = async url => url.endsWith('/ls-remote')
        ? {ok: true, branches: ['18.0']}
        : {ok: true, job: job('running', [item('tools', 'adding')])};
      setRows([repo('web', {branches: null, branchState: ''}), repo('tools')]);
      applyOcaJob(job('running', [item('tools', 'queued')]), false);
      queueOcaBranches(ocaRows[0]);
      await tick();
      await tick();
      await pollOcaJob();
      assert.equal(ocaRows[0].branchState, 'loaded');
      assert.equal(ocaRowStatus(ocaRows[1])[0], 'Adding…');
      assert.equal(renders, 0);
      assert.ok(rowUpdates > 0);
    """)


def test_lost_start_response_follows_only_a_new_server_job():
    _run("""
      let posts = 0, current = job('finished', [], 'oca-old'), started = false;
      fetch = async (url, options) => {
        if (options && options.method === 'POST') {
          posts++;
          if (started) current = job('running', [item('web', 'queued')], 'oca-new');
          throw Error('Connection lost');
        }
        return {ok: true, job: current};
      };
      setRows([repo('web')]);
      applyOcaJob(current, true);
      await startOcaImport(false);
      assert.match(document.getElementById('oca-error').textContent, /Connection lost/);
      assert.equal(ocaRunning(), false);
      started = true;
      await startOcaImport(false);
      assert.equal(posts, 2);
      assert.equal(ocaJob.id, 'oca-new');
      assert.equal(ocaRunning(), true);
      assert.equal(document.getElementById('oca-error').textContent, '');
    """)


def test_rejected_start_is_reported_without_guessing():
    _run("""
      let gets = 0;
      fetch = async (url, options) => {
        if (options && options.method === 'POST') return {ok: false, error: 'Bad batch', status: 400};
        gets++;
        return {ok: true, job: null};
      };
      setRows([repo('web')]);
      await startOcaImport(false);
      assert.equal(document.getElementById('oca-error').textContent, 'Bad batch');
      assert.equal(gets, 0);
      assert.equal(ocaStarting, false);
    """)


def test_branch_loading_is_bounded_and_preserves_new_version_selection():
    _run("""
      let pending = [], active = 0, peak = 0;
      fetch = (url, options) => {
        peak = Math.max(peak, ++active);
        return new Promise(resolve => pending.push(() => {
          active--; resolve({ok: true, branches: ['18.0', '19.0']});
        }));
      };
      setRows(['a', 'b', 'c', 'd', 'e'].map(name => repo(name, {branches: null, branchState: ''})));
      ocaRows.forEach(queueOcaBranches);
      assert.equal(pending.length, 3);
      document.getElementById('oca-versions').value = '19.0, 19.0';
      applyOcaVersions();
      ocaRows[3].selected = false; // queued but no longer wanted
      while (pending.length) { pending.shift()(); await tick(); }
      assert.equal(peak, 3);
      assert.equal(ocaBranchActive, 0);
      assert.deepEqual(ocaRows[0].desired, ['19.0']);
      assert.equal(ocaRows[3].branchState, '');
      assert.equal(ocaRows[4].branchState, 'loaded');
    """)


def test_branch_discovery_can_be_retried_after_failure():
    _run("""
      setRows([repo('tools', {branches: null, branchState: ''})]);
      let calls = 0;
      fetch = async () => ++calls === 1
        ? {ok: false, error: 'Network unavailable'}
        : {ok: true, branches: ['18.0']};
      queueOcaBranches(ocaRows[0]);
      await tick();
      assert.equal(ocaRows[0].branchState, 'error');
      assert.equal(ocaReady(ocaRows[0]), false);
      document.getElementById('oca-repos').handlers.click(
        {target: {dataset: {row: '0', action: 'reload-branches'}}});
      await tick();
      assert.equal(ocaReady(ocaRows[0]), true);
      assert.equal(calls, 2);
    """)
