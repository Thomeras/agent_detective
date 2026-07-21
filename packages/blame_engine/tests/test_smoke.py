"""Smoke test: the package imports. Real tests land in milestone M1."""


def test_package_imports() -> None:
    import blame_engine

    assert blame_engine.__version__
