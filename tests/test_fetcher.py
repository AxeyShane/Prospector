from prospector.fetcher import discover_links, html_to_text, same_site

HTML = """<html><head><title>Acme Ltd</title></head><body><nav>
<a href="/about-us">About Us</a><a href="/global-presence">Global Presence</a>
<a href="/products/crushers">Products</a><a href="/blog/post">News</a>
<a href="/dealer-network">Dealer Network</a><a href="mailto:x@y.com">Mail</a>
<a href="https://facebook.com/acme">FB</a><a href="/brochure.pdf">PDF</a>
</nav><main><p>We   export to  Australia.</p></main></body></html>"""


def test_text_extraction_collapses_whitespace():
    title, text = html_to_text(HTML)
    assert title == "Acme Ltd"
    assert "We export to Australia." in text


def test_presence_pages_are_prioritised_over_marketing_pages():
    links = discover_links("https://acme.com", HTML, limit=8)
    assert links[0].endswith("/global-presence")
    assert links[1].endswith("/dealer-network")
    assert any(link.endswith("/about-us") for link in links)


def test_external_and_binary_links_are_skipped():
    links = discover_links("https://acme.com", HTML, limit=8)
    assert not any("facebook" in link for link in links)
    assert not any(link.endswith(".pdf") for link in links)
    assert not any("mailto" in link for link in links)


def test_same_site_tolerates_www_and_subdomains():
    assert same_site("https://acme.com", "https://www.acme.com/about")
    assert same_site("https://acme.com", "https://in.acme.com/x")
    assert not same_site("https://acme.com", "https://acme-fake.com")


# ---------------------------------------------------------------------------
# Three ways the decisive sentence used to be thrown away.
# ---------------------------------------------------------------------------

def test_the_footer_survives_a_page_that_uses_main():
    """On a manufacturer's site the overseas offices are in the footer.

    Taking <main> alone dropped it on every site modern enough to use the tag --
    which is most of the ones large enough to have overseas offices.
    """
    html = ("<html><body><main><p>" + "x" * 300 + "</p></main>"
            "<footer>Overseas offices: Sydney, Dubai, Nairobi</footer>"
            "</body></html>")
    _, text = html_to_text(html)
    assert "Sydney" in text


def test_a_long_page_keeps_its_tail_as_well_as_its_head():
    """Dealer tables and export-market lists live at the bottom of the page."""
    html = ("<html><body><p>TOP MARKER</p><p>" + "filler " * 4000 +
            "</p><p>Distributor: Acme Pty Ltd, Perth WA</p></body></html>")
    _, text = html_to_text(html, max_chars=2000)
    assert "TOP MARKER" in text
    assert "Acme Pty Ltd" in text
    assert len(text) < 2200


def test_the_contact_and_dealer_pages_get_a_reserved_slot():
    """They rank below Products, so a link-rich site pushed them off the list.

    Those are the two pages the whole qualification stage exists to read.
    """
    links = "".join(f'<a href="/products/{i}">Product {i}</a>' for i in range(20))
    html = (f'<html><body>{links}'
            '<a href="/global-presence">Global Presence</a>'
            '<a href="/contact-us">Contact Us</a>'
            '<a href="/dealers">Dealer Network</a>'
            '<a href="/about">About</a></body></html>')

    picked = discover_links("https://acme.example", html, limit=6)
    assert any("contact" in u for u in picked)
    assert any("dealer" in u for u in picked)
    assert len(picked) <= 6
