"""Phase 9b (§16.3) writer-emulation style layer — pure helpers."""
from switchboard.style import (
    FEATURE_KEYS,
    build_distill_prompt,
    parse_style_features,
    select_exemplars,
    style_guide_text,
)


def _rows():
    return [
        {"author": "Alice", "title": "A1", "url": "u/a1", "sessions": 100},
        {"author": "Alice", "title": "A2", "url": "u/a2", "sessions": 300},   # Alice's best
        {"author": "Alice", "title": "A3", "url": "u/a3", "sessions": 50},
        {"author": "Bob", "title": "B1", "url": "u/b1", "sessions": 200},
        {"author": "Bob", "title": "B2", "url": "u/b2", "sessions": 90},
        {"author": "Cara", "title": "C1", "url": "u/c1", "sessions": 10},
    ]


def test_select_exemplars_interleaves_by_rank_and_caps():
    picks = select_exemplars(["Alice", "Bob", "Cara"], _rows(), per_author=2, cap=4)
    # rank-0 of each author first (best-performing), then rank-1, capped at 4.
    assert [p["url"] for p in picks] == ["u/a2", "u/b1", "u/c1", "u/a1"]


def test_select_exemplars_dedups_and_skips_missing_fields():
    rows = _rows() + [{"author": "Bob", "title": "dupe", "url": "u/b1", "sessions": 999},
                      {"author": "", "url": "u/x", "sessions": 5},          # no author
                      {"author": "Dan", "url": "", "sessions": 5}]          # no url
    picks = select_exemplars(["Alice", "Bob", "Cara", "Dan"], rows, per_author=2, cap=10)
    urls = [p["url"] for p in picks]
    assert len(urls) == len(set(urls))            # no duplicate URLs
    assert "u/x" not in urls and all(p["author"] for p in picks)


def test_select_exemplars_respects_top_author_order_only():
    # An author absent from the top list contributes nothing even if in rows.
    picks = select_exemplars(["Bob"], _rows(), per_author=3, cap=10)
    assert {p["author"] for p in picks} == {"Bob"}


def test_parse_style_features_plain_json():
    raw = '{"voice":"wry","tone":"confident","dos":["lead with the news"],"donts":"no clickbait"}'
    f = parse_style_features(raw)
    assert set(f) == set(FEATURE_KEYS)            # every key present
    assert f["voice"] == "wry" and f["tone"] == "confident"
    assert f["dos"] == ["lead with the news"]
    assert f["donts"] == ["no clickbait"]         # scalar coerced to a 1-item list
    assert f["vocabulary"] == ""                  # missing string key defaults to ''


def test_parse_style_features_tolerates_fences_and_prose():
    raw = 'Sure!\n```json\n{"voice": "punchy"}\n```\nHope that helps.'
    assert parse_style_features(raw)["voice"] == "punchy"


def test_parse_style_features_caps_lists_and_survives_garbage():
    f = parse_style_features('{"dos": ["a","b","c","d","e","f","g"]}')
    assert f["dos"] == ["a", "b", "c", "d", "e"]  # capped at 5
    empty = parse_style_features("not json at all")
    assert empty == {k: ([] if k in ("dos", "donts") else "") for k in FEATURE_KEYS}


def test_style_guide_text_empty_and_populated():
    assert style_guide_text(None) == ""
    assert style_guide_text({}) == ""
    assert style_guide_text({k: "" for k in FEATURE_KEYS}) == ""   # all-blank → nothing usable
    guide = style_guide_text({"voice": "wry", "dos": ["cite sources"], "donts": ["no fluff"]})
    assert "HOUSE STYLE GUIDE" in guide
    assert "- Voice: wry" in guide
    assert "- Do: cite sources" in guide and "- Don't: no fluff" in guide


def test_build_distill_prompt_includes_brand_and_truncates():
    ex = [{"author": "Alice", "title": "T", "url": "u", "text": "x" * 5000}]
    prompt = build_distill_prompt("hotcars", ex, max_chars=100)
    assert "BRAND: hotcars" in prompt and "EXEMPLAR 1" in prompt
    assert "x" * 100 in prompt and "x" * 101 not in prompt   # body truncated to max_chars
