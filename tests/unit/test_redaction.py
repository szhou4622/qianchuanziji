from commercial_v1.security.redaction import REDACTED, redact, sanitize_text


def test_redact_nested_sensitive_fields() -> None:
    source = {
        "advertiser_id": "123",
        "access_token": "token-value",
        "nested": {"app-secret": "secret-value", "name": "ok"},
        "items": [{"device_credential": "credential", "value": 1}],
    }
    result = redact(source)
    assert result["advertiser_id"] == "123"
    assert result["access_token"] == REDACTED
    assert result["nested"]["app-secret"] == REDACTED
    assert result["items"][0]["device_credential"] == REDACTED


def test_sanitize_text_hides_common_secret_assignments() -> None:
    text = "Authorization: Bearer abc123 access_token=xyz app_secret=hello"
    sanitized = sanitize_text(text)
    assert "abc123" not in sanitized
    assert "xyz" not in sanitized
    assert "hello" not in sanitized
