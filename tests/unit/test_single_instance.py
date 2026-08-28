from commercial_v1.runtime.single_instance import GlobalUserMutex, mutex_name


def test_mutex_name_is_stable_and_does_not_expose_identity() -> None:
    identity = "DOMAIN\\alice"
    first = mutex_name(identity)
    second = mutex_name(identity)
    assert first == second
    assert first.startswith("Global\\QCSCKP-commercial-v1-")
    assert "alice" not in first.lower()
    assert "DOMAIN" not in first


def test_different_users_get_different_mutex_names() -> None:
    assert mutex_name("DOMAIN\\alice") != mutex_name("DOMAIN\\bob")


def test_non_windows_mutex_lifecycle() -> None:
    mutex = GlobalUserMutex("test-mutex")
    assert mutex.acquire() is True
    assert mutex.acquire() is True
    mutex.close()
    assert mutex.acquire() is True
    mutex.close()
