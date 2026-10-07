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

1. Update `__version__` in `src/docuconf/_version.py` (the single source of the package version) and commit.
   If the sample export changes, refresh the golden files: `UPDATE_GOLDEN=1 pytest tests/test_export.py` and
   `docuconf export examples.app.settings:Settings --out examples/app/contract.cue`.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. The workflow runs the tests (including `cue vet` against the docuconf-go meta-schema), checks that the tag
   matches the package version, builds the sdist and wheel, checks them with `twine check`, and publishes.
   Pre-release versions such as `0.2.0b1` (tag `v0.2.0b1`) are only installed by `pip install --pre`.

To test the pipeline without touching the real index, point a copy of the publish job at TestPyPI
(`repository-url: https://test.pypi.org/legacy/`) with its own pending publisher there.

## GitHub Packages and Releases

GitHub Packages has no Python registry, so the GitHub copy of each release is the GitHub Release. The `github` job in
`.github/workflows/release.yml` takes the sdist and wheel that the `build` job made (after `test` passed), creates the
GitHub Release for the tag if it does not exist, and attaches both files. It does not depend on the PyPI `publish`
job, so it works before the PyPI project, trusted publisher and `pypi` environment exist. It uses only the workflow's
own `GITHUB_TOKEN` (`contents: write`); there are no secrets or accounts to set up, and nothing to configure beyond the
`Docuconf` organization allowing `GITHUB_TOKEN` write access (it does unless restricted under Organization settings >
Actions).

### Installing from a GitHub Release

No token is needed for a public repository. Install the wheel straight from the Release:

```sh
pip install "docuconf-pydantic @ https://github.com/Docuconf/docuconf-python/releases/download/v0.1.0/docuconf_pydantic-0.1.0-py3-none-any.whl"
```

or let pip pick the file from the Release page:

```sh
pip install docuconf-pydantic --find-links https://github.com/Docuconf/docuconf-python/releases/expanded_assets/v0.1.0
```

The same URLs work in `requirements.txt` and in `pyproject.toml` dependencies.

## docuconf-go version

docuconf-go owns the spec, the CUE meta-schema (`spec/cue`), the conformance suite (`conformance/cases.json`) and the
`docuconf` CLI. This SDK is tested against one docuconf-go commit, pinned in `.github/docuconf-go.ref` (a full SHA).

- **CI** checks out that commit on pushes and pull requests. The nightly scheduled run uses docuconf-go `main` instead,
  so a spec change that breaks this SDK shows up within a day. To try another docuconf-go commit or branch, run the CI
  workflow by hand (Actions, CI, Run workflow) with `docuconf_go_ref` set. Releases always build against the pinned commit.
- **Bump PRs.** `.github/workflows/docuconf-go-bump.yml` opens (or updates) a `build(deps): bump docuconf-go to <sha>`
  pull request from the `docuconf-go-bump` branch whenever docuconf-go `main` moves: immediately when docuconf-go sends
  a `docuconf-go-updated` dispatch (this needs the release GitHub App), otherwise on its daily schedule. CI on that PR
  is the compatibility check; merge it when it is green, or fix the SDK on the same branch. It can also be run by hand
  with a specific `sha`.
- **`scripts/conformance.sh`** runs only the docuconf-go-facing checks (the conformance suite and the `cue vet` of
  exported contracts) against any checkout: `DOCUCONF_GO_DIR=../docuconf-go scripts/conformance.sh`. CI runs it, and
  so does docuconf-go's downstream workflow, which runs it against every docuconf-go pull request that touches the spec,
  the conformance suite or the CLI. It needs Python 3.10+ and `cue` on `PATH`, and installs the SDK into a throwaway virtualenv.

Without the release App (secrets `RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY`) the bump workflow uses
`GITHUB_TOKEN`: the repository setting "Allow GitHub Actions to create and approve pull requests" must be on, and
because a PR opened that way triggers no workflows, the bump workflow starts CI on the branch itself
(`workflow_dispatch`, whose checks show on the PR).
