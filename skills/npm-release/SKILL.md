---
name: npm-release
description: >
  Publish the @yya007/skill-finder npm package. Guides through login check,
  OSS precheck (secrets scan + README accuracy), dry-run preview, local
  smoke test, version bump (patch/minor/major), npm publish, and git tag.
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

### Step 3 — OSS precheck

Run this before touching the package to catch anything that shouldn't ship.

**3a — Secrets scan:** search all tracked files for credentials.

```bash
git ls-files | xargs grep -l "ghp_[a-zA-Z0-9]\{36\}\|npm_[a-zA-Z0-9]\{36\}\|sk-[a-zA-Z0-9]\{32\}" 2>/dev/null
```

```bash
git ls-files | xargs grep -rn "password\s*=\s*['\"][^'\"]\|api_key\s*=\s*['\"][^'\"]\|secret\s*=\s*['\"][^'\"]" 2>/dev/null
```

If any real secrets are found (not placeholders like `ghp_your_token_here` or `fake-token`), stop immediately and tell the user.

**3b — Sensitive files check:** confirm no `.env`, credential, or key files are tracked.

```bash
git ls-files | grep -E "\.(env|key|pem|p12|pfx|crt|secret)$|credentials|\.npmrc$"
```

Any output here is a blocker — remove those files and purge from git history before proceeding.

**3c — README accuracy check:**

Check that the skill count in the README matches `data/version.txt`:

```bash
python3 -c "
import re, sys
readme = open('README.md').read()
version = open('data/version.txt').read()

# Extract actual count from version.txt
actual = int(re.search(r'skill_count:\s*(\d+)', version).group(1))

# Extract the rounded/displayed count from stats marker
m = re.search(r'stats:skill-count:start -->(.*?)<!--', readme)
displayed = m.group(1).strip() if m else 'NOT FOUND'

print(f'data/version.txt skill_count: {actual:,}')
print(f'README stats marker:          {displayed}')

# Check filter logic description is accurate
if 'Listed in a curated registry' in readme or 'SkillHub rank S or A' in readme:
    print('WARN: README still contains outdated filter rules (curated registry / SkillHub rank exemptions)')
    sys.exit(1)
else:
    print('OK: filter logic description looks current')
"
```

If the skill count in the README is stale (off by more than a minor rounding), update the stats marker and the tagline count, then commit the fix before proceeding.

If outdated filter rules are detected, fix README's "What gets indexed" section before proceeding.

---

### Step 4 — Dry run (always run before publishing)

```bash
npm publish --dry-run 2>&1
```

Show the user the list of files that would be included. Confirm:
- `scripts/search.py`, `scripts/requirements.txt`, `scripts/__init__.py` are present.
- `scripts/fetch_skill.py` and `scripts/update_index.py` are **not** present (developer-only tools, excluded from the npm package — available in the git repo).
- `data/index.faiss`, `data/metadata.jsonl`, `data/version.txt` are present.
- `SKILL.md` and `plugin.json` are present.
- No `crawlers/`, `pipeline/`, `tests/`, `docs/`, `__pycache__/`, or `.sh` files appear.

If unexpected files appear, stop and ask the user to review `.npmignore` and the `files` list in `package.json`.

---

### Step 5 — Local smoke test

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

### Step 6 — Bump version

```bash
npm version <patch|minor|major> --no-git-tag-version
```

Use the bump type from Step 2. Report the new version (e.g. `0.1.0 → 0.1.1`).

---

### Step 7 — Publish

```bash
npm publish --access public
```

npm will prompt for a one-time password if 2FA is enabled. Tell the user to enter their authenticator code when prompted — or pass `--otp <code>` if they provide it in advance.

On success, report:
> "Published @yya007/skill-finder@<new-version> to npmjs.com"
> "View: https://www.npmjs.com/package/@yya007/skill-finder"

---

### Step 8 — Stamp the npm version on the release log, then commit and tag

Record the published npm version against the current index release in the
release-history log (idempotent; keyed by `data/version.txt` date):

```bash
python pipeline/update_release_log.py --npm-version <new-version>
```

Then commit the bump + the log update and tag:

```bash
git add package.json data/release_log.jsonl docs/release-log.md
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

### Step 9 — Report

Summarise:
- Package name and new version
- Files published
- npm URL
- Git tag (if pushed)

If the user found it helpful, remind them:
> "If SkillFinder was useful, consider starring the repo: https://github.com/yya007/SkillFinder"
