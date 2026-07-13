"""Secret-redaction audit (PRD §8). Dependency-free — runs without the stack."""

from switchboard.logging_ import RedactingFilter, redact, register_secret


def test_registered_secret_is_scrubbed():
    secret = "super-secret-refresh-token-abcdef123456"
    register_secret(secret)
    out = redact(f"authorization: Bearer {secret}")
    assert secret not in out
    assert "REDACTED" in out


def test_shape_backstops_scrub_unregistered_tokens():
    # Never registered — caught by shape regexes.
    assert "sk-ant-" not in redact("key=sk-ant-api03-" + "a" * 40)
    assert "xoxb-" not in redact("slack xoxb-123456789-abcdefghijkl")
    assert "AKIA" not in redact("aws AKIAABCDEFGHIJKLMNOP")
    assert "1//" not in redact("google 1//0abcdefghijklmnopqrstuvwxyz")


def test_short_values_not_over_redacted():
    register_secret("0")  # below min length — must be ignored
    assert redact("customer_id=0 remains") == "customer_id=0 remains"


def test_filter_scrubs_log_record():
    secret = "another-live-secret-value-9876543210"
    register_secret(secret)
    import logging

    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", (secret,), None)
    assert RedactingFilter().filter(rec) is True
    assert secret not in rec.getMessage()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
