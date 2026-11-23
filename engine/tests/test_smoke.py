from importlib.metadata import version

import pl_intel


def test_engine_is_installed() -> None:
    assert pl_intel.__name__ == "pl_intel"
    assert version("pl-intel") == "0.1.0"
