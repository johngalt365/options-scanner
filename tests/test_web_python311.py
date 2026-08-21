import importlib

from options_scanner.scan_service import ScanMetrics, ScanResult


def test_web_import_and_sortable_header_rendering():
    web = importlib.import_module("options_scanner.web")
    result = ScanResult((), ScanMetrics(), 0.1, underlying_price=100)

    page = web.render_page(multi_results=(("NVDA", result, None),)).decode()

    assert ('<button type="button" class="sort-button" data-column="0" '
            'data-kind="text">Ticker <span aria-hidden="true">↕</span></button>') in page
    assert ('<button type="button" class="sort-button" data-column="1" '
            'data-kind="number">Score <span aria-hidden="true">↕</span></button>') in page
    assert ('<button type="button" class="sort-button" data-column="3" '
            'data-kind="number">Precio <span aria-hidden="true">↕</span></button>') in page
    assert '<th>Estado</th>' in page
    assert '<button type="button" class="sort-button" data-column="2"' not in page
