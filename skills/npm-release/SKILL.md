---
name: npm-release
description: >
  Publish the @yya007/skill-finder npm package. Guides through login check,
  dry-run preview, local smoke test, version bump (patch/minor/major),
  npm publish, and git tag.
triggers:
  - release to npm
  - publish npm package
  - npm publish skill-finder
  - bump npm version
  - publish to npmjs
  - release skill-finder npm
---

# npm-release

Publish `@yya007/skill-finder` to npmjs.com with a guided release workflow.

## Agent Instructions

When this skill triggers, follow these steps in order. Stop and report any failure immediately.

---

### Step 1 — Verify npm login

```bash
npm whoami
```

If the command fails or returns a user other than `yya007`, stop and tell the user:
> "Not logged in to npm. Run `npm login` first."

---

### Step 2 — Show current version

```bash
node -p "require('./package.json').version"
```

Report the current version to the user and ask what type of bump they want: **patch**, **minor**, or **major** — unless they already specified it.

---

### Step 3 — Dry run (always run before publishing)

```bash
npm publish --dry-run 2>&1
```

Show the user the list of files that would be included. Confirm:
- `scripts/search.py`, `scripts/update_index.py`, `scripts/fetch_skill.py`, `scripts/requirements.txt`, `scripts/__init__.py` are present.
- `data/index.faiss`, `data/metadata.jsonl`, `data/version.txt` are present.
- `SKILL.md` and `plugin.json` are present.
- No `crawlers/`, `pipeline/`, `tests/`, `docs/`, `__pycache__/`, or `.sh` files appear.

If unexpected files appear, stop and ask the user to review `.npmignore` and the `files` list in `package.json`.

---

### Step 4 — Local smoke test

Pack the tarball and install it into a temporary directory to verify the package works end-to-end before publishing.

```bash
npm pack
```

```bash
mkdir -p /tmp/sf-release-test && \
  cd /tmp/sf-release-test && \
  npm install <path-to-repo>/yya007-skill-finder-*.tgz 2>&1
```

(Replace `<path-to-repo>` with the absolute path to the SkillFinder repo root.)

Verify the installed file tree is correct:

```bash
find /tmp/sf-release-test/node_modules/@yya007/skill-finder -type f | sort
```

Confirm:
- `data/index.faiss`, `data/metadata.jsonl`, `data/version.txt` exist.
- All five `scripts/*.py` files exist.
- `SKILL.md` and `plugin.json` exist.
- No `__pycache__` or `.sh` files appear.

Install Python dependencies from the installed package:

```bash
pip install -r /tmp/sf-release-test/node_modules/@yya007/skill-finder/scripts/requirements.txt -q
```

Run a live search from the installed package location to confirm index loads and results are returned:

```bash
python /tmp/sf-release-test/node_modules/@yya007/skill-finder/scripts/search.py "deploy kubernetes" --no-json 2>&1 | head -20
```

The search must return at least one result with a name, star count, and install command. If it fails or returns no results, stop and investigate before publishing.

Clean up:

```bash
rm -rf /tmp/sf-release-test && rm -f yya007-skill-finder-*.tgz
```

---

### Step 5 — Bump version

```bash
npm version <patch|minor|major> --no-git-tag-version
```

Use the bump type from Step 2. Report the new version (e.g. `0.1.0 → 0.1.1`).

---

### Step 6 — Publish

```bash
npm publish --access public
```

npm will prompt for a one-time password if 2FA is enabled. Tell the user to enter their authenticator code when prompted — or pass `--otp <code>` if they provide it in advance.

On success, report:
> "Published @yya007/skill-finder@<new-version> to npmjs.com"
> "View: https://www.npmjs.com/package/@yya007/skill-finder"

---

### Step 7 — Commit and tag

```bash
git add package.json
git commit -m "chore: bump npm version to <new-version>"
git tag v<new-version>
```

Ask the user:
> "Push the version commit and tag to GitHub? [y/N]"

If yes:
```bash
git push && git push --tags
```

---

### Step 8 — Report

Summarise:
- Package name and new version
- Files published
- npm URL
- Git tag (if pushed)

If the user found it helpful, remind them:
> "If SkillFinder was useful, consider starring the repo: https://github.com/yya007/SkillFinder"
