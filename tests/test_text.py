import pytest

from discord_scraper.text import clean, links, load_stopwords, tokens
from tests.conftest import cfg_fixture


@pytest.mark.parametrize("raw,expected", [
    ("check https://youtu.be/x?si=1 out", ["check", "out"]),
    ("<@123> hello <:kek:456>", ["hello", "kek"]),
    ("```py\nprint(1)\n``` real", ["real"]),
    # collapse 3+ repeats to 2 (spec §6.1), so ``sooo`` survives as ``soo``:
    # collapsing to 1 would turn ``goooood`` into ``god``.
    ("sooo goooood", ["soo", "good"]),
    # markdown, inline code and an animated emoji ("hi" is 2 letters, dropped)
    ("**bold** `code` <a:party:1> hello", ["bold", "party", "hello"]),
])
def test_clean_strips_links_entities_code_and_elongation(raw, expected):
    assert tokens(raw) == expected


def test_tokens_are_three_letters_minimum_and_digit_free():
    assert tokens("ab abc a1b x9 123") == ["abc"]


def test_links_extracts_http_www_and_bare_domains():
    assert links("a https://example.com/x b www.foo.io c bar.gg/d") == [
        "https://example.com/x",
        "www.foo.io",
        "bar.gg/d",
    ]


def test_chat_filler_is_filtered_but_topics_are_not(tmp_path):
    stop = load_stopwords(cfg_fixture(tmp_path, packs=["en"]))
    assert "lol" in stop and "gonna" in stop
    assert "play" not in stop and "game" not in stop


def test_regional_pack_is_opt_in(tmp_path):
    assert "lah" not in load_stopwords(cfg_fixture(tmp_path, packs=["en"]))
    assert "lah" in load_stopwords(cfg_fixture(tmp_path, packs=["en", "ms"]))


def test_user_lists_merge_last_and_the_ignore_list_wins(tmp_path):
    cfg = cfg_fixture(tmp_path, packs=["en"], ignore=["Zonked"])
    cfg.stopword_dir.mkdir()
    (cfg.stopword_dir / "mine.txt").write_text("ternor\n\n# a comment\n", encoding="utf-8")
    stop = load_stopwords(cfg)
    assert "ternor" in stop
    assert "zonked" in stop
