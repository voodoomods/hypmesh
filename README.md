# hypmesh

Coming soon from the makers of [HypGraph](https://hypgraph.com) and [HypSwarm](https://hypswarm.com).

This repository and the `hypmesh` package on PyPI are reserved for the project. Release 0.0.1 has no functionality
yet; it holds the name.

[hypmesh.com](https://hypmesh.com)

## Releasing

Releases are published to PyPI by GitHub Actions with trusted publishing (no API token). To release:

1. Bump `__version__` in `src/hypmesh/__init__.py` and `version` in `pyproject.toml` to the same value.
2. Merge, then tag the merge commit `v<version>` and push the tag.
3. `.github/workflows/release.yml` checks the tag against `__version__`, builds the sdist and wheel, installs the
   wheel in a clean venv, and publishes from the `pypi` environment.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
