import pytest
from bs4 import BeautifulSoup

from bellhaven_sync.scraper import parse_detail


def page(address_html, h1="Bellhaven Meadows of Findlay"):
    return BeautifulSoup(f"""<div class="wrap"><h1>{h1}</h1><dl class="detail">
      <dt>Address</dt><dd>{address_html}</dd>
      <dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>
      <dt>Administrator</dt><dd>Sam Pruitt</dd><dt>Phone</dt><dd>(231) 533-2969</dd></dl></div>""", "html.parser")


@pytest.mark.parametrize("address_html", [
    "1800 N Blanchard St<br/>Findlay, OH 45840",   # live site format
    "1800 N Blanchard St, Findlay, OH 45840",      # single line
])
def test_address_formats(address_html):
    loc = parse_detail(page(address_html), "/communities/x")
    assert (loc["address"], loc["city"], loc["state"], loc["zip"]) == ("1800 N Blanchard St", "Findlay", "OH", "45840")
    assert loc["care_offerings"] == ["Assisted Living", "Memory Support"] and loc["administrator"] == "Sam Pruitt"


def test_badge_inside_heading_is_not_part_of_name():
    loc = parse_detail(page("1800 N Blanchard St<br/>Findlay, OH 45840",
                            h1='Bellhaven Meadows of Findlay <span class="tag">New</span>'), "/communities/x")
    assert loc["name"] == "Bellhaven Meadows of Findlay"


def test_unparseable_address_fails_loudly():
    with pytest.raises(ValueError):
        parse_detail(page("Findlay, OH 45840"), "/communities/x")
