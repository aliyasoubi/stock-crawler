import pytest

from stock_crawler.sovereign.treasury import TreasuryError, find_file_url

PAGE = """
<p><a href="#">Merkezi Yönetim Borç Stoku İstatistikleri</a></p>
<a href="https://ms.hmb.gov.tr/uploads/2026/09/Merkezi_Yonetim_Borc_Stoku_Enstruman_Dagilimi-13704bc8.xls">
  <strong>Merkezi Yönetim Borç Stoku Enstrüman Dağılımı</strong></a>
<a href="https://ms.hmb.gov.tr/uploads/2024/08/Merkezi-Yonetim-Borc-Stokunun-Enstruman-Dagilimi.pdf">Merkezi Yönetim Borç Stoku Enstrüman Dağılımı</a>
"""


def test_find_file_url_picks_excel_by_link_text():
    url = find_file_url(PAGE, "Merkezi Yönetim Borç Stoku Enstrüman Dağılımı")
    assert url.endswith("Enstruman_Dagilimi-13704bc8.xls")


def test_find_file_url_fails_loudly_when_layout_changes():
    with pytest.raises(TreasuryError):
        find_file_url(PAGE, "Some Renamed Link")
