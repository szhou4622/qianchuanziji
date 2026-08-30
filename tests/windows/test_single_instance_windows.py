import os

import pytest

from commercial_v1.runtime.single_instance import GlobalUserMutex, current_user_identity, mutex_name

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only single-instance contract")


def test_windows_identity_is_real_sid() -> None:
    identity = current_user_identity()
    assert identity.startswith("S-1-")
    assert "\\" not in identity


def test_two_mutexes_same_user_scope_are_mutually_exclusive() -> None:
    name = mutex_name(current_user_identity()) + "-ci"
    first = GlobalUserMutex(name)
    second = GlobalUserMutex(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        second.close()
        first.close()
        assert second.acquire() is True
    finally:
        first.close()
        second.close()
