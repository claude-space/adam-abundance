"""Plagiarism providers (§13.16) — pure parsers, config gates, signal text."""

from switchboard import plagiarism as P


class FakeCreds:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def resolve(self, key, *, required=False, secret=True):
        return self.values.get(key)


def test_configured_gates_need_identifier_plus_key():
    assert not P.copyscape_configured(FakeCreds({"COPYSCAPE_API_KEY": "k"}))
    assert P.copyscape_configured(FakeCreds({"COPYSCAPE_API_KEY": "k", "COPYSCAPE_USERNAME": "u"}))
    assert not P.copyleaks_configured(FakeCreds({"COPYLEAKS_API_KEY": "k"}))
    assert P.copyleaks_configured(FakeCreds({"COPYLEAKS_API_KEY": "k", "COPYLEAKS_EMAIL": "e"}))


def test_missing_config_flags_the_missing_identifiers():
    m = P.missing_config(FakeCreds({"COPYSCAPE_API_KEY": "k", "COPYLEAKS_API_KEY": "k2"}))
    assert "COPYSCAPE_USERNAME" in m
    assert "COPYLEAKS_EMAIL" in m
    assert "SWITCHBOARD_PUBLIC_URL" not in m  # copyleaks not fully configured, so not flagged yet


def test_parse_copyscape_matches_sorted_by_score():
    xml = """<response><count>2</count>
      <result><url>http://a.com/x</url><title>A</title><percentmatched>4</percentmatched><wordsmatched>20</wordsmatched></result>
      <result><url>http://b.com/y</url><title>B</title><percentmatched>12</percentmatched><wordsmatched>60</wordsmatched></result>
    </response>"""
    r = P.parse_copyscape_xml(xml)
    assert r["status"] == "done" and r["count"] == 2
    assert r["score"] == 12  # highest percentmatched
    assert r["matches"][0]["url"] == "http://b.com/y"


def test_parse_copyscape_no_matches_is_clear():
    r = P.parse_copyscape_xml("<response><count>0</count></response>")
    assert r["status"] == "done" and r["score"] == 0 and r["matches"] == []


def test_parse_copyscape_error_and_bad_xml():
    assert P.parse_copyscape_xml("<response><error>Invalid API key</error></response>")["status"] == "error"
    assert P.parse_copyscape_xml("not xml <<<")["status"] == "error"


def test_parse_copyleaks_score_percent_and_fraction():
    r1 = P.parse_copyleaks_result(
        {"results": {"score": {"aggregatedScore": 37},
                     "internet": [{"url": "u", "title": "t", "matchedWords": 50}]}})
    assert r1["score"] == 37 and r1["matches"][0]["url"] == "u"
    r2 = P.parse_copyleaks_result({"results": {"score": {"aggregatedScore": 0.42}}})
    assert r2["score"] == 42  # fraction normalised to percent
    r3 = P.parse_copyleaks_result({})
    assert r3["status"] == "done" and r3["score"] is None


def test_signal_text_states():
    assert P.signal_text(None) is None
    assert P.signal_text({"copyscape": {"status": "done", "score": 0}}) == "Copyscape: clear"
    assert P.signal_text({"copyscape": {"status": "done", "score": 8}}) == "Copyscape: 8% match"
    s = P.signal_text({"copyscape": {"status": "done", "score": 3}, "copyleaks": {"status": "pending"}})
    assert "Copyscape: 3% match" in s and "Copyleaks: scanning" in s
    assert P.signal_text({"copyleaks": {"status": "error"}}) == "Copyleaks: error"


def test_scan_id_embeds_job_id_roundtrip():
    sid = P.new_scan_id(4821)
    assert sid.startswith("sw4821x")
    assert P.job_id_from_scan(sid) == 4821
    assert P.job_id_from_scan("garbage") is None


async def test_run_copyscape_not_configured_never_calls_network():
    r = await P.run_copyscape(FakeCreds({"COPYSCAPE_API_KEY": "k"}), "text " * 20)
    assert r["status"] == "not_configured"


async def test_submit_copyleaks_login_fail_is_soft():
    r = await P.submit_copyleaks(
        FakeCreds({"COPYLEAKS_API_KEY": "k"}),  # no email → login returns None
        scan_id="sw1xabc", text="text " * 20, webhook_url="https://x/y")
    assert r["status"] == "error"
