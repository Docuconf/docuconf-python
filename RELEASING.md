# Releasing docuconf-pydantic

Releases are published to PyPI by `.github/workflows/release.yml` when a version tag is pushed. It uses
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/): GitHub Actions proves its identity to PyPI
with OIDC, so no API token is stored anywhere, and `pypa/gh-action-pypi-publish` uploads digital attestations
(PEP 740) with each file.

## One-time setup

1. **PyPI project.** PyPI lets you register a *pending* trusted publisher before the project exists, so no manual
   first upload is needed. Signed in to pypi.org as a maintainer (with 2FA), open *Your projects → Publishing → Add
   a new pending publisher* and enter:
   - PyPI project name: `docuconf-pydantic`
   - Owner: `docuconf`, repository: `docuconf-python`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The first successful run of the workflow creates the project. Afterwards, add the other maintainers as owners.
2. **GitHub environment.** In the repository settings, create an environment named `pypi`. Limit its deployment
   branches and tags to `v*`, and add required reviewers if a human should approve each release.

## Each release

Releases are automated with [release-please](https://github.com/googleapis/release-please); see
[CONTRIBUTING.md](CONTRIBUTING.md#how-releases-happen) for the commit conventions it reads.

1. Merge the open release PR (`chore(main): release X.Y.Z`). It already bumps `__version__` in
   `src/docuconf/_version.py` and updates `CHANGELOG.md`. The golden files and example contracts do not need
   regenerating: their comparisons ignore `metadata.generator.version`.
2. release-please tags the merge commit `vX.Y.Z` and creates the GitHub release with the changelog entries.
3. `.github/workflows/release.yml` runs on the tag: it runs the tests (including `cue vet` against the docuconf-go
   meta-schema), checks that the tag matches the package version, builds the sdist and wheel, checks them with
   `twine check`, and publishes.

If the release PR was created with `GITHUB_TOKEN` (no release GitHub App configured), the tag does not trigger
`release.yml` by itself, so `.github/workflows/release-please.yml` starts it with `gh workflow run`. To redo a
release by hand: `gh workflow run release.yml --ref vX.Y.Z`.

To test the pipeline without touching the real index, point a copy of the publish job at TestPyPI
(`repository-url: https://test.pypi.org/legacy/`) with its own pending publisher there.
