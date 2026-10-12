# Publishing the sanitized commit history safely

This release intentionally retains the actual development timeline while rewriting sensitive content and author metadata. **Only push the sanitized copy; do not push your original working repository.**

## Pre-publish verification

1. Extract the prepared archive into a new folder (not on top of your local working project).
2. Run `python scripts/check_public_release.py` and `python scripts/check_public_history.py`.
3. Review `git log --all --graph --oneline --decorate` and `git status --short`.
4. Run `python -m pytest -q`, `ruff check .`, and `mypy lead_radar` if the tools are installed.
5. Verify there is no existing remote: `git remote -v` should be empty.

## Publish all 20 original development commits

Create a new **empty** GitHub repository, without GitHub's automatic README, license, or `.gitignore` init. Then run in the **sanitized** folder:

```powershell
git remote add origin https://github.com/SerDrozd/intentscout.git
git push -u origin main
git push origin archive/rss-alternative archive/roi-alternative
```

The default branch shows the 18 sequential engineering commits, a public-release commit, and the IntentScout branding commit. The two archived experiments remain available under their respective branches. `git log --all` shows every development commit.

Do not use `git push --mirror` or `git push --force` on an existing repository. Use a genuinely empty new repository to avoid modifying unrelated history.

## Important limits

- A successful pattern scan is not a guarantee against an unrecognized secret. Manually inspect future changes before committing them.
- `.gitignore` applies only to newly staged files; it does not remove already committed private data from history.
- No real RSS URLs or credentials should ever be pasted into public issues or PRs.
- If a real key is believed to have been exposed previously, revoke/rotate it independently of this sanitization.
