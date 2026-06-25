# Project notes for Claude

## Git commit identity

All commits in this repository must be authored as:

```
Mukund Devanathan <musdevanathan31@students.cumberlandschools.org>
```

This is set via local (repo-scoped) git config — `git config --local user.name`/`user.email` — not the global config, so it only applies here. If a fresh clone or worktree of this repo is ever used, re-apply it:

```
git config --local user.name "Mukund Devanathan"
git config --local user.email "musdevanathan31@students.cumberlandschools.org"
```

Do not commit under any other name/email in this repository, even if the global git config points elsewhere.
