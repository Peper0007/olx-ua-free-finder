"""Offline tests for parsers, pre-filter and message formatting (no network)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finder.prefilter import is_junk
from finder.sources import alo, bazar, olx
from finder.telegram import format_ad


def olx_html():
    ads = [
        {"id": 149884270, "title": "Nike Blazer 77 Vintage Low", "description": "Подарявам маратонки",
         "url": "https://www.olx.bg/d/ad/nike-blazer-77-vintage-low-CID655-IDa8TKe.html",
         "createdTime": "2026-11-22T13:47:14+02:00",
         "price": {"budget": False, "free": True, "exchange": False, "displayValue": "Безплатно", "regularPrice": None},
         "location": {"cityName": "гр. Банско", "regionName": "Област Благоевград", "districtName": None,
                      "pathName": "Област Благоевград, гр. Банско"},
         "params": [{"key": "delivery_paid_by", "name": "Доставката се поема от", "value": "купувача"},
                    {"key": "state", "value": "използвано"}],
         "photos": ["https://frankfurt.apollo.olxcdn.com:443/v1/files/abc/image;s=1118x1552"]},
        {"id": 2, "title": "Подарявам котенце", "price": {"free": True}, "location": {}, "photos": [],
         "url": "https://www.olx.bg/d/ad/x.html"},
        {"id": 3, "title": "Платена обява", "price": {"free": False}, "location": {}, "photos": [], "url": "u"},
    ]
    state = json.dumps({"listing": {"listing": {"pageNumber": 0, "ads": ads}}}, ensure_ascii=False)
    return ("<html><script>window.__INIT_CONFIG__ = {};\n        window.__PRERENDERED_STATE__= "
            + json.dumps(state, ensure_ascii=False) + ";\n</script></html>")


BAZAR = '''
<div class="listItemContainer listItemContainerV2 "> <a title="Стари порцеланови чаши" href="https://bazar.bg/obiava-56928821/stari-chashi" data-id="56928821" class="listItemLink "> <img class="cover " alt="x" src="//cdn9.focus.bg/bazar/f0/pics/aaa.webp" data-src="//cdn9.focus.bg/bazar/f0/pics/aaa.webp"> <div class="title"> <span class="title"> Стари порцеланови чаши </span> <span class="location">гр. София, Бояна</span> <span class="date">днес</span> <span class="price">Подарявам <span class="currency"></span> </span> </div> </a></div>
<div class="listItemContainer listItemContainerV2 "> <a title="Продавам" href="https://bazar.bg/obiava-1/p" data-id="1" class="listItemLink "> <div class="title"><span class="title">Продавам кола</span><span class="price">13 000 €</span></div></a></div>
'''

BAZAR_DETAIL = '''<html><div itemprop="description">Сервиз от 70-те<br>Чешки порцелан</div>
<img src="//cdn9.focus.bg/bazar/f0/pics/aaa.webp"><img data-src="//cdn9.focus.bg/bazar/e6/pics/bbb.webp">
<img src="//cdn9.focus.bg/bazar/f0/thumbs/aaa.webp"></html>'''

ALO = '''
<div class="listvip-item higher noselect mb20 hgha" id="adrows_8870249" title="Подарявам книги!!!">
  <a href="/podaryavam-knigi-8870249#highlight=x"><img src="user_files/p/prodajbi/8870249_124191998_medium.jpg"></a>
  <h3>Подарявам книги!!!</h3><span class="mobile_block">София</span>
  <div class="listvip-desc">Стари съветски книги</div></div>
<div class="listtop-item" id="adrows_111"><h3>Цистерни</h3>СУПЕР ТОП<span class="price_nowrap">957 €</span></div>
<div class="listvip-item" id="adrows_222"><a href="/x-222"></a><h3>Маса</h3><span class="price_nowrap">150 лв.</span></div>
'''


def test_olx():
    ads = olx.parse_listing(olx_html())
    assert [a.ad_id for a in ads] == ["149884270", "2"], ads
    a = ads[0]
    assert a.location == "гр. Банско, Област Благоевград"
    assert a.delivery == "купувача"
    assert a.photos[0].endswith(";s=1118x1552")
    assert a.posted == "2026-11-22 13:47"
    assert is_junk(ads[1]) and not is_junk(a)


def test_bazar():
    ads = bazar.parse_listing(BAZAR)
    assert len(ads) == 1 and ads[0].ad_id == "56928821"
    assert ads[0].photos == ["https://cdn9.focus.bg/bazar/f0/pics/aaa.webp"]
    assert ads[0].location == "гр. София, Бояна"
    bazar.parse_details(BAZAR_DETAIL, ads[0])
    assert "Чешки порцелан" in ads[0].description
    assert len(ads[0].photos) == 2


def test_alo():
    ads = alo.parse_listing(ALO)
    assert [a.ad_id for a in ads] == ["8870249"], ads
    a = ads[0]
    assert a.url == "https://www.alo.bg/podaryavam-knigi-8870249"
    assert a.photos == ["https://www.alo.bg/user_files/p/prodajbi/8870249_124191998_big.jpg"]
    assert a.location == "София" and "съветски" in a.description


def test_prefilter():
    from finder.common import Ad
    junk = ["Котята в добрые руки", "Цуценя шукає дім", "Віддам диван", "Дрова безкоштовно", "Курточка на хлопчика. Ріст 128см",
            "одяг для дівчинки 4-5 років", "Пральна машина на запчастини", "Прийму в дар ноутбук", "Матрац 160х200"]
    good = ["Старий телевізор", "Порцелянова статуетка", "Фарфорові чашки СРСР", "Фотоапарат Зеніт",
            "Вінілові платівки", "Набір значків", "Старовинний годинник", "Кришталева ваза", "Ретро шафа"]
    for t in junk:
        assert is_junk(Ad("x", "1", t, "u")), t
    for t in good:
        assert not is_junk(Ad("x", "1", t, "u")), t
    assert is_junk(Ad("olx", "1", "Книги", "u", category_id="4006"))
    assert not is_junk(Ad("olx", "1", "Книги", "u", category_id="49"))
    assert not is_junk(Ad("olx", "1", "Книги", "u", category_id="40050"))


def test_format():
    from finder.common import Ad
    ad = Ad("olx", "1", "Чаша <b>", "https://olx.bg/x", location="Видин", delivery="купувача")
    txt = format_ad(ad, {"score": 7, "price": "$20–25", "items": "чаша Włocławek", "why": "ПНР", "pickup": "курьер"})
    assert "&lt;b&gt;" in txt and "7/10" in txt and "Открыть на OLX" in txt


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
