# Contributing to docuconf-pydantic

## Commit messages and pull request titles

Pull requests are squash-merged, so each PR title becomes one commit message on `main`. Titles follow
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/), and the `pr-title` check enforces it:

```
<type>(<optional scope>): <summary>
```

For example `feat: add a duration type`, `fix(export): escape quotes in descriptions` or
`docs: explain contract-first mode`.

| Type | Use it for | Release | Changelog section |
|---|---|---|---|
| `feat` | a new feature | minor | Features |
| `fix` | a bug fix | patch | Bug Fixes |
| `perf` | a performance improvement | patch | Performance Improvements |
| `refactor` | a code change that is neither a fix nor a feature | patch | Code Refactoring |
| `docs` | documentation only | patch | Documentation |
| `revert` | reverting an earlier change | patch | Reverts |
| `build`, `ci`, `test`, `chore` | build, CI, tests and housekeeping | none | hidden |

A breaking change adds `!` after the type (`feat!: rename Export to ToCue`) or a `BREAKING CHANGE:` footer in the
PR description. It bumps the major version from 1.0 on; while the version is below 1.0, it bumps the minor
version. Commits on their own branches can say anything: only the PR title reaches `main`.

## How releases happen

Releases are automated with [release-please](https://github.com/googleapis/release-please).

1. Every push to `main` updates one open release PR, titled like `chore(main): release X.Y.Z`. It bumps the
   version from the commit types above, updates `__version__` in `src/docuconf/_version.py` (the package version, also written into exported contracts as `metadata.generator.version`), and adds the new entries to `CHANGELOG.md`.
2. A maintainer ships a release by merging the release PR. Nothing is released until then, and the PR can wait
   while more changes land: it updates itself.
3. Merging it tags the commit `vX.Y.Z` and creates the GitHub release with the changelog entries. `.github/workflows/release.yml` then runs the tests and publishes the sdist and wheel to PyPI.

See [RELEASING.md](RELEASING.md) for the one-time setup.
