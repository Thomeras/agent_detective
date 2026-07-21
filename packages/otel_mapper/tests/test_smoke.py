"""Smoke test: the package imports. Real tests land in milestone M2."""


def test_package_imports() -> None:
    import otel_mapper

    assert otel_mapper.__version__
