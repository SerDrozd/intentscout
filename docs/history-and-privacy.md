# Historical Git provenance and privacy

## Scope

- `main`: 18 genuine development commits from **6–9 October 2026**, in their original parent order, plus a separate public-release commit.
- `archive/rss-alternative` and `archive/roi-alternative`: 2 historical alternative commits branching off the main development lineage. These are preserved but are not merged into the default branch.
- The original dates, subjects and ancestry are retained. The author/committer identity in the public history uses the GitHub profile handle and no-reply email.

## Why commit SHAs changed

Git SHA values cover the complete commit object and underlying trees, including author details. Removing personal data throughout historical source revisions and commit metadata necessarily changes those IDs. The changes do **not** invent development commits or falsely backdate the public-release step.

## What was excluded or redacted

- Personal SQLite databases, state, logs, backups and local machine environments.
- Credential files, private feed configuration and account-specific Google Alerts URLs.
- Personal name/email mentions and local workstation paths in historical text and code.
- Old patch-export directories and duplicate snapshots (not product source).

The public build ships `scripts/check_public_release.py` for the working tree and `scripts/check_public_history.py` to inspect **all reachable commits across all included branches**. These checks cannot prove that no sensitive value exists beyond their detection rules; review before publication and rotate any credential suspected of prior exposure.

## Integrity checks

From the prepared repository:

```bash
python scripts/check_public_release.py
python scripts/check_public_history.py
git rev-list --all --count
git log --all --graph --oneline --decorate
```

Do not merge the original private history into this repository, add a remote pointing to a personal mirror with private Git objects, or push from the local private working repository.
