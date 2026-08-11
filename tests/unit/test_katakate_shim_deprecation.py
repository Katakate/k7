"""Spec 11a: ``katakate`` PyPI shim re-exports ``k7_sdk`` with a deprecation warning."""

import warnings


def test_katakate_shim_warns_on_import():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import katakate  # noqa: F401

    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert any("k7-sdk" in str(w.message) for w in caught)


def test_katakate_client_is_k7_sdk_client():
    import katakate
    from k7_sdk import Client

    assert katakate.Client is Client
