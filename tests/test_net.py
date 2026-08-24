"""HTTP layer: robots, naming, URL hygiene."""

import pytest

from tpmap.net import Fetcher, normalise_url, slugify, url_to_stem


def test_normalise_url_drops_fragments_and_fills_empty_paths():
    assert normalise_url("https://x.com") == "https://x.com/"
    assert normalise_url("https://x.com/a?b=1#frag") == "https://x.com/a?b=1"


def test_slugify_is_filesystem_safe():
    assert slugify("Sanand 4/A (Ahmedabad)") == "sanand-4-a-ahmedabad"
    assert slugify("") == "unnamed"
    assert "/" not in slugify("a/b/c")


def test_url_to_stem_is_stable_and_distinguishes_similar_urls():
    a = "https://townplanmap.com/tp/scheme-ognaj221"
    b = "https://townplanmap.com/tp/scheme-ognaj222"
    assert url_to_stem(a) == url_to_stem(a)
    assert url_to_stem(a) != url_to_stem(b)
    assert url_to_stem(a).startswith("scheme-ognaj221-")


def test_robots_is_obeyed_and_can_be_overridden(site):
    # the fixture allows everything
    with Fetcher(rate=0) as f:
        assert f.allowed(f"{site}/tp/scheme-a.html")

    class Blocked(Fetcher):
        def _robots_for(self, url):
            from urllib.robotparser import RobotFileParser
            rp = RobotFileParser()
            rp.parse(["User-agent: *", "Disallow: /tp/"])
            return rp

    with Blocked(rate=0) as f:
        assert not f.allowed(f"{site}/tp/scheme-a.html")
        assert f.allowed(f"{site}/other")
        with pytest.raises(PermissionError):
            f.get(f"{site}/tp/scheme-a.html")

    with Blocked(rate=0, obey_robots=False) as f:
        assert f.allowed(f"{site}/tp/scheme-a.html")


def test_cache_avoids_a_second_request(site, tmp_path):
    with Fetcher(rate=0, cache_dir=tmp_path) as f:
        url = f"{site}/data/plots.geojson"
        first = f.get_bytes(url)
        assert list(tmp_path.iterdir())
        f.session.close()          # any further network use would now fail
        assert f.get_bytes(url) == first
