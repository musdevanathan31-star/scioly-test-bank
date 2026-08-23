# Project notes for Claude

## Git commit identity

All commits in this repository must be authored with the repository-approved
identity.

Set this via local (repo-scoped) git config — `git config --local user.name` /
`user.email` — not the global config, so it only applies here. If a fresh
clone or worktree of this repo is ever used, re-apply it:

```
git config --local user.name "<approved-name>"
git config --local user.email "<approved-email>"
```

Do not commit under any other name/email in this repository, even if the
global git config points elsewhere.
