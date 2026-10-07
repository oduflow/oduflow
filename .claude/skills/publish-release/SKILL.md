---
name: publish-release
description: >-
  Prepare and ship a new Oduflow release end to end — analyze commits since the
  last tag, propose a semver bump and an English changelog section, get explicit
  sign-off, then bump pyproject.toml, commit, push to main, tag, publish the
  GitHub Release (the step that actually ships to PyPI + Docker Hub), and verify
  the artifacts. Use this whenever the user wants to cut, prepare, or publish an
  Oduflow release — including "/publish-release", "make a release", "prepare the
  next release", "cut a release" — even if they don't name the exact steps.
  Triggers for the oduflow project (pyproject.toml `name = "oduflow"`, publish.yml/docker.yml
  on `release: published`).
---

# Publish an Oduflow release

This skill drives the full release of the `oduflow` package. The authoritative
process lives in the repo's **`AGENTS.md` → "Publishing a New Version"** and
"Publishing Docker Image" sections — read them at the start of every run in case
the process has evolved; this skill is the operational harness around them, not a
replacement. If the two ever disagree, `AGENTS.md` wins.

## The one fact that trips everyone up

**A pushed tag does not publish anything. Publishing the GitHub Release does.**
Both `.github/workflows/publish.yml` (PyPI) and `.github/workflows/docker.yml`
(Docker Hub) trigger on `release: [published]` — *not* on the tag push. So a tag
with no published GitHub Release ships nothing. The GitHub Release in Phase 2 is
the irreversible "go live" moment; everything before it is reversible.

Two more non-obvious conventions, learned from prior releases:

- **The Release *title* is a short human-readable phrase, never the version
  number.** e.g. `Per-team coding agent: Agent CLI & Chat`, not `v1.61.0`. The
  version already lives in the tag.
- **The changelog is written in English**, even when the working conversation is
  in another language.

## Phase 1 — Analyze and propose (then STOP)

Do all of this read-only work first, present it, and **wait for explicit
approval before touching anything.** Publishing is irreversible (PyPI/Docker/
GitHub), so the human signs off on version + changelog + title before Phase 2.

1. **Sync to `origin/main`.** Releases are cut from `main`. `git fetch`, then note
   whether the current branch is behind `origin/main`. The release commit must
   land on `main`, so plan to fast-forward (or PR) as needed. Never build a
   release off a feature branch's unmerged state.
2. **Find the last release** and the delta:
   ```bash
   git fetch --tags
   LAST=$(git tag --sort=-v:refname | grep -E '^v[0-9]' | head -1)
   git log --oneline "$LAST"..origin/main
   ```
   Read the merged PRs (`gh pr list --state merged --base main --limit 30`, or the
   commit messages) to understand what actually changed — enough to write real
   release notes, not just a list of PR numbers.
3. **Propose the version.** The project uses **minor bumps for feature releases**
   (new capability, config mode, subsystem) and **patch bumps for fix-only**
   releases. Current version is in `pyproject.toml`. Recommend one, with a
   one-line rationale — but the final number is the user's call (they have
   deliberately jumped versions before).
4. **Draft the changelog section** in English, titled `## vX.Y.Z`, grouped under
   `### Features` / `### Bug Fixes` / `### Documentation` / `### Security` /
   `### Dashboard` (use only the groups that apply). Match the voice of existing
   sections in `docs/changelog.md`: each bullet is a bolded lead-in + a concrete
   sentence or two on *what changed and why it matters*, ending with the PR
   ref(s) like `(#103)`. Fold any existing `## Unreleased` content in.
5. **Propose the Release title** — a short descriptive phrase capturing the
   headline change.
6. **Present** the version, the full drafted changelog section, the title, and
   the execution plan. Then stop and ask for confirmation (a plain "go" is
   enough). Adjust if the user changes anything.

## Phase 2 — Execute (only after explicit "go")

Follow the AGENTS.md order exactly; the version bump must be on `main` *before*
the tag, because the publish workflows build from the tagged commit.

1. **Fast-forward** the local branch to `origin/main` if it was behind.
2. **`pyproject.toml`** → set `version = "X.Y.Z"`.
3. **`docs/changelog.md`** → insert the approved `## vX.Y.Z` section at the top
   (just under `# Changelog`), replacing/absorbing `## Unreleased` if present.
4. **Commit and push to `main`:**
   ```bash
   git add pyproject.toml docs/changelog.md
   git commit -m "Release vX.Y.Z"
   git push origin HEAD:main      # fast-forward when the branch already == origin/main
   ```
   If `main` is protected and rejects the push, open a PR (`gh pr create --base
   main`) and get it merged before continuing — the tag must point at the commit
   that is actually on `main`.
5. **Tag the release commit and push the tag:**
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
6. **Publish the GitHub Release — this ships the package.** Extract this version's
   changelog section verbatim for the body (replace `X.Y.Z` and `PREV` with the
   real numbers):
   ```bash
   awk 'NR>1 && /^## vPREV$/{exit} /^## vX.Y.Z$/{f=1} f' docs/changelog.md > /tmp/release-notes.md
   gh release create vX.Y.Z \
     --title "<short phrase>" \
     --notes-file /tmp/release-notes.md \
     --latest --verify-tag
   ```
   (Drop `--latest` and add `--prerelease` for a pre-release; `:latest` Docker tag
   is skipped for pre-releases.)

Publishing the Release fires three automations off the `release: published`
event: **PyPI** (`publish.yml`), **Docker Hub multi-arch** `oduist/oduflow:X.Y.Z`
+ `:latest` (`docker.yml`), and the docs redeploy (from the `docs/` change landing
on `main`). No manual `pip publish` or `docker push` is needed.

## Phase 3 — Verify and confirm

Watch the triggered workflows to completion and confirm the artifacts really
landed, then report:

```bash
gh run list --limit 8                          # watch Publish to PyPI / Docker until success
gh run watch <run-id>                          # optional: block on a specific run
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/project/oduflow/X.Y.Z/
gh release view vX.Y.Z --web                    # or print the URL
```

Confirm: PyPI page for `X.Y.Z` returns 200, Docker Hub has the `X.Y.Z` (and
`latest`) tags, the GitHub Release is live, and the docs site rebuilt. Report the
concrete URLs and the workflow statuses. If a workflow fails, surface the log —
don't claim success. (A known-benign note: Docker builds may warn about Node.js
action deprecation; it doesn't affect publication.)

## Guardrails

- **Never publish the Release before the version bump + changelog commit is on
  `main`.** The workflows build from the tagged commit.
- **Confirm before Phase 2.** The publish is irreversible; the human approves
  version, changelog, and title first.
- **Don't run `mkdocs gh-deploy` by hand**, and don't `docker push`/`pip publish`
  manually — the merge-to-main + Release automations handle docs, PyPI, and
  Docker. Manual pushes are fallback-only (see AGENTS.md).
- **Don't add `Co-Authored-By` trailers** to the release commit.
