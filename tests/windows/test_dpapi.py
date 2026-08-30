import os

import pytest

from commercial_v1.security.dpapi import DPAPIUnavailableError, protect_text, unprotect_text


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_dpapi_round_trip_current_windows_user() -> None:
    encrypted = protect_text("top-secret-中文")
    assert "top-secret" not in encrypted
    assert unprotect_text(encrypted) == "top-secret-中文"


def test_non_windows_dpapi_fails_closed() -> None:
    if os.name == "nt":
        pytest.skip("non-Windows behavior")
    with pytest.raises(DPAPIUnavailableError):
        protect_text("secret")
