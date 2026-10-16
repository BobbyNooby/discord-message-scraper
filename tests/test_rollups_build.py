

def test_media_is_keyed_on_the_url_and_survives_mixed_providers(conn, cfg):
    """The same GIF arrives with a provider on one post and none on another.

    Keying on (url, provider, …) broke the table's own primary key on the
    reference database, so the URL is the identity and the first sighting that
    carried a title or thumbnail fills the gaps.
    """
    from tests.conftest import author_raw, seed_messages
    from discord_scraper.rollups.build import rebuild_media

    def embed(url, provider, thumbnail):
        return {"type": "gifv", "url": url,
                "provider": ({"name": provider} if provider else None),
                "thumbnail": {"url": thumbnail}}

    seed_messages(conn, [
        ("zz1", "u1", "u1handle", "look", "2026-01-01T10:00:00+00:00", None, None, 0, 0,
         author_raw("u1", "u1handle",
                    embeds=[embed("https://tenor.com/view/cat", None, "https://media.tenor.com/cat.gif")])),
        ("zz2", "u1", "u1handle", "again", "2026-01-02T10:00:00+00:00", None, None, 0, 0,
         author_raw("u1", "u1handle",
                    embeds=[embed("https://tenor.com/view/cat", "Tenor", "")])),
    ])
    from discord_scraper.rollups.identity import resolve_identities

    rebuild_media(conn, cfg, resolve_identities(conn, cfg))
    rows = conn.execute(
        "SELECT url, provider, title, thumbnail, uses, first_seen, last_seen FROM rollup_media"
    ).fetchall()
    assert len(rows) == 1, "one URL, one row"
    url, provider, _title, thumbnail, uses, first_seen, last_seen = rows[0]
    assert uses == 2
    assert provider == "Tenor", "the named sighting wins over 'unprovidenced'"
    assert thumbnail.endswith("cat.gif"), "the missing thumbnail was filled in, not overwritten"
    assert first_seen < last_seen
