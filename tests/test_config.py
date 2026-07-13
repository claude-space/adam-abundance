"""Config: brand facts + the auth allowlist/domain gate (PRD §9.1). Needs the
stack installed (pydantic-settings) but no DB."""

from switchboard.config import AuthConfig, get_settings


def test_brand_facts():
    s = get_settings()
    hc = s.brand("hotcars")
    assert hc.short_code == "HC"
    assert hc.discover_name == "HotCars"
    assert hc.sentinel_property_id == "www.hotcars.com"
    assert hc.gsc_table == "gsc.hotcars_com_searchdata_url_impression"


def test_auth_allows_domain_and_allowlist():
    auth = AuthConfig(allowed_domains=("valnetinc.com",), allowlist=("a@valnetinc.com",))
    assert auth.is_allowed("a@valnetinc.com", hd="valnetinc.com") is True
    # right domain but not on allowlist:
    assert auth.is_allowed("b@valnetinc.com", hd="valnetinc.com") is False
    # wrong domain:
    assert auth.is_allowed("a@evil.com", hd="evil.com") is False


def test_auth_domain_only_when_no_allowlist():
    auth = AuthConfig(allowed_domains=("valnetinc.com",), allowlist=())
    assert auth.is_allowed("anyone@valnetinc.com") is True
    assert auth.is_allowed("x@other.com") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
