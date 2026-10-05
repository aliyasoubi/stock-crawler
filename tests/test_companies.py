"""companies: which KAP pages are accepted. Offline."""
from stock_crawler.companies.crawler import is_kap_page, is_profile_page

PADDING = b"x" * 25_000


def page(content: str) -> bytes:
    return f'<html><a href="/tr/sirket-bilgileri/ozet/1">{content}'.encode() + PADDING + b"</html>"


def test_profile_page_needs_the_profile_itself():
    """KAP's throttling shell is a valid-looking page without the profile; it must be fetched again."""
    shell = page("")
    assert is_kap_page(shell) and not is_profile_page(shell)
    assert is_profile_page(page("<h3>Merkez Adresi</h3><h3>Şirketin Sektörü</h3>"))
