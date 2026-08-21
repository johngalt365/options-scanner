from datetime import date, timedelta
from unittest import TestCase
from options_scanner.historical import (DemoHistoricalDataProvider, HistoricalBar, HistoricalPeriod,
                                        IbkrHistoricalDataProvider, map_ibkr_historical_bars)
from options_scanner.technical_analysis import Pivot, PivotType, ZoneType, atr, cluster_zones, detect_pivots
from options_scanner.technical_context import StrikePosition, build_technical_context, strike_context
from options_scanner.scan_service import ScanMetrics, ScanResult
from options_scanner.web import render_page


def bars(values, spread=2):
    start=date(2026,1,1)
    return tuple(HistoricalBar(start+timedelta(days=i),v,v+spread/2,v-spread/2,v,1000+i) for i,v in enumerate(values))

class Transport:
    def __init__(self): self.calls=[]
    def get(self,path,params):
        self.calls.append((path,params))
        if "search" in path:return [{"symbol":"XYZ","conid":42}]
        return {"data":[{"t":1767225600000,"o":10,"h":12,"l":9,"c":11,"v":123}]}

class TechnicalTest(TestCase):
    def test_historical_mapping_and_ibkr_read_only_endpoint(self):
        transport=Transport(); result=IbkrHistoricalDataProvider(transport).get_historical_bars("xyz")
        self.assertEqual((result[0].open,result[0].volume),(10,123))
        self.assertEqual(transport.calls[-1],("/iserver/marketdata/history",{"conid":"42","period":"6m","bar":"1d","outsideRth":"true"}))

    def test_mapping_skips_malformed_rows(self):
        self.assertEqual(len(map_ibkr_historical_bars({"data":[{"bad":1},{"t":"2026-01-02","o":1,"h":2,"l":.5,"c":1}]})),1)

    def test_mapping_accepts_real_gateway_nested_data_shape(self):
        payload={"data":{"data":[{"t":1767225600000,"o":10.5,"h":12.0,"l":9.0,"c":11.25,"v":1234}]}}
        mapped=map_ibkr_historical_bars(payload)
        self.assertEqual((mapped[0].session.isoformat(),mapped[0].close,mapped[0].volume),("2026-01-01",11.25,1234))

    def test_pivot_high_and_low_and_insufficient(self):
        found=detect_pivots(bars((10,9,8,9,12,9,8)),window=1)
        self.assertIn(PivotType.LOW,{p.kind for p in found}); self.assertIn(PivotType.HIGH,{p.kind for p in found})
        self.assertEqual(detect_pivots(bars((1,2)),window=2),())

    def test_atr_tolerance_and_zone_strength_are_explainable(self):
        series=bars(tuple([100]*20),spread=10)
        ps=tuple(Pivot(i,series[i].session,p,PivotType.LOW,2) for i,p in enumerate((98,101,99,100),1))
        zones=cluster_zones(ps,series,tolerance_atr=.6)
        self.assertEqual(len(zones),1); self.assertEqual(zones[0].contacts,4)
        self.assertGreaterEqual(zones[0].score,70); self.assertEqual(zones[0].strength,"fuerte")
        self.assertAlmostEqual(atr(series)[-1],10)

    def test_zone_broken_conservatively_by_close_beyond_atr(self):
        series=bars((100,100,100,100,100,100,100,90),spread=2)
        pivot=Pivot(2,series[2].session,99,PivotType.LOW,1)
        zone=cluster_zones((pivot,),series,tolerance_atr=.5)[0]
        self.assertTrue(zone.broken); self.assertFalse(zone.inverted)

    def test_nearest_zones_and_strike_positions(self):
        series=bars((100,98,100,105,110,107,110,104,102),spread=1)
        context=build_technical_context("X",HistoricalPeriod.SIX_MONTHS,series,105,(96,98,101),window=1)
        support=context.nearest_support
        self.assertIsNotNone(support)
        self.assertEqual({s.position for s in context.strikes},{StrikePosition.ABOVE,StrikePosition.INSIDE,StrikePosition.BELOW})
        self.assertIsNotNone(context.nearest_resistance)

    def test_empty_series(self):
        context=build_technical_context("X",HistoricalPeriod.SIX_MONTHS,(),100,(90,))
        self.assertEqual(context.zones,()); self.assertIsNone(context.nearest_support)
        self.assertIsNone(strike_context(90,None).position)

    def test_demo_history_is_reproducible_and_period_aware(self):
        provider=DemoHistoricalDataProvider(date(2026,8,21))
        first=provider.get_historical_bars("X",HistoricalPeriod.THREE_MONTHS)
        self.assertEqual(first,provider.get_historical_bars("X",HistoricalPeriod.THREE_MONTHS))
        self.assertEqual(len(first),66); self.assertEqual(len(provider.get_historical_bars("X",HistoricalPeriod.ONE_YEAR)),264)

    def test_web_chart_selector_frozen_and_context_messages(self):
        series=DemoHistoricalDataProvider().get_historical_bars("X")
        context=build_technical_context("X",HistoricalPeriod.SIX_MONTHS,series,105,(100,))
        result=ScanResult((),ScanMetrics(),.1,underlying_price=105,market_data_status="Frozen",technical_context=context)
        page=render_page(result=result).decode()
        for text in ('value="3m"','value="6m" selected','value="1y"','<svg role="img"','Contexto técnico','Última sesión disponible','no garantizan reacciones futuras','Frozen'):
            self.assertIn(text,page)
        self.assertIn('<details id="technical-x">',page)
        self.assertNotIn('<details id="technical-x" open',page)
        self.assertIn('data-period="3m"',page); self.assertIn('data-period="6m"',page); self.assertIn('data-period="1y"',page)
        self.assertIn("event.target.closest('.period-button')",page)

    def test_frozen_without_history_shows_safe_visible_fallback(self):
        context=build_technical_context("X",HistoricalPeriod.SIX_MONTHS,(),105)
        result=ScanResult((),ScanMetrics(historical_status="empty"),.1,underlying_price=105,
                          market_data_status="Frozen",technical_context=context)
        page=render_page(result=result).decode()
        self.assertIn("Contexto técnico",page); self.assertIn("Histórico no disponible",page)
        self.assertIn("El scan de opciones no se ha visto afectado",page)
        self.assertIn("Frozen",page)
