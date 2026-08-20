from datetime import date, timedelta
from unittest import TestCase

from options_scanner.filters import filter_put_candidates, safety_margin
from options_scanner.models import OptionContract, OptionType, Underlying


class FilterPutCandidatesTest(TestCase):
    def setUp(self) -> None:
        self.as_of = date(2026, 8, 20)
        self.nvda = Underlying("NVDA", 100.0)

    def contract(
        self,
        *,
        dte: int = 35,
        strike: float = 75.0,
        delta: float = -0.20,
        symbol: str = "NVDA",
        option_type: OptionType = OptionType.PUT,
    ) -> OptionContract:
        return OptionContract(
            symbol, option_type, strike, self.as_of + timedelta(days=dte), delta
        )

    def test_keeps_contract_that_matches_all_rules(self) -> None:
        matching = self.contract()

        self.assertEqual(
            filter_put_candidates(self.nvda, [matching], self.as_of), [matching]
        )

    def test_accepts_inclusive_boundaries(self) -> None:
        boundaries = [
            self.contract(dte=30, strike=80.0, delta=-0.15),
            self.contract(dte=45, strike=80.0, delta=-0.30),
        ]

        self.assertEqual(
            filter_put_candidates(self.nvda, boundaries, self.as_of), boundaries
        )

    def test_rejects_each_non_matching_rule(self) -> None:
        contracts = [
            self.contract(dte=29),
            self.contract(dte=46),
            self.contract(strike=81.0),
            self.contract(delta=-0.14),
            self.contract(delta=-0.31),
            self.contract(symbol="AMD"),
            self.contract(option_type=OptionType.CALL),
        ]

        self.assertEqual(filter_put_candidates(self.nvda, contracts, self.as_of), [])

    def test_delta_filter_uses_absolute_value(self) -> None:
        positive_delta = self.contract(delta=0.20)

        self.assertEqual(
            filter_put_candidates(self.nvda, [positive_delta], self.as_of),
            [positive_delta],
        )

    def test_safety_margin_formula(self) -> None:
        self.assertAlmostEqual(safety_margin(100.0, 80.0), 0.20)

    def test_rejects_inverted_ranges(self) -> None:
        with self.assertRaises(ValueError):
            filter_put_candidates(
                self.nvda, [], self.as_of, min_dte=46, max_dte=30
            )
