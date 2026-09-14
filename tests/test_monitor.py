import unittest
from unittest.mock import Mock, patch

from src.monitor import SEAGM_URL, fetch_html, normalize_products, parse_products


SETTINGS_HTML = """
<div id="lang_currency_settings">
  <form action="/zh-cn/setting?csrfToken=test-token" method="post">
    <input type="hidden" name="region" value="de">
    <input type="hidden" name="request_uri" value="/zh-cn/itunes-gift-card-united-states">
  </form>
</div>
"""


def product_html(currency: str = "CNY", symbol: str = "¥") -> str:
    rows = []
    for face_value in (2, 3, 4, 5, 10):
        rows.append(
            f"""
            <div class="SKU_type">
              <div class="sku"><span>iTunes 礼品卡 {face_value} 美金 (美国)</span></div>
              <b class="price_origional">{symbol} {face_value:.2f}</b>
              <b class="price_discount">{symbol} {face_value * 0.97:.2f}</b>
            </div>
            """
        )
    return (
        f'<div class="language_currency"><b class="currency">{currency}</b></div>'
        '<div id="cardType">'
        + "".join(rows)
        + "</div><!-- iTunes Gift Card -->"
    )


class FetchHtmlTests(unittest.TestCase):
    @patch("src.monitor.requests.Session")
    def test_fetch_html_switches_session_to_cny(self, session_factory: Mock) -> None:
        session = session_factory.return_value
        settings_response = Mock(
            text=SETTINGS_HTML,
            url="https://www.seagm.com/zh-cn/language_currency",
        )
        settings_response.raise_for_status.return_value = None
        product_response = Mock(text=product_html(), url=SEAGM_URL)
        product_response.raise_for_status.return_value = None
        session.get.side_effect = [settings_response, product_response]
        session.post.return_value.raise_for_status.return_value = None

        self.assertEqual(fetch_html(), product_response.text)
        post_data = session.post.call_args.kwargs["data"]
        self.assertEqual(post_data["currency"], "CNY")
        self.assertEqual(post_data["language"], "zh")

    @patch("src.monitor.requests.Session")
    def test_fetch_html_rejects_non_cny_response(self, session_factory: Mock) -> None:
        session = session_factory.return_value
        settings_response = Mock(
            text=SETTINGS_HTML,
            url="https://www.seagm.com/zh-cn/language_currency",
        )
        settings_response.raise_for_status.return_value = None
        product_response = Mock(text=product_html("EUR", "€"), url=SEAGM_URL)
        product_response.raise_for_status.return_value = None
        session.get.side_effect = [settings_response, product_response]
        session.post.return_value.raise_for_status.return_value = None

        with self.assertRaisesRegex(RuntimeError, "预期 CNY，实际为 EUR"):
            fetch_html()


class ParseProductsTests(unittest.TestCase):
    def test_parse_cny_products(self) -> None:
        products = parse_products(product_html())

        self.assertEqual(len(products), 5)
        self.assertEqual(products[0]["face_value_usd"], 2)
        self.assertEqual(products[0]["list_price_cny"], 2.0)
        self.assertEqual(products[0]["price_cny"], 1.94)
        self.assertEqual(normalize_products(products)[0]["cny_per_usd_value"], 0.97)


if __name__ == "__main__":
    unittest.main()
