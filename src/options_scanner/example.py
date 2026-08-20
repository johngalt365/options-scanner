"""Ejemplo de selección sobre contratos ficticios."""

from options_scanner.filters import filter_put_candidates, safety_margin
from options_scanner.sample_data import EXAMPLE_DATE, NVDA, NVDA_OPTIONS


def main() -> None:
    candidates = filter_put_candidates(NVDA, NVDA_OPTIONS, EXAMPLE_DATE)
    print(f"Candidatas para {NVDA.symbol} a {EXAMPLE_DATE.isoformat()}:")
    for contract in candidates:
        margin = safety_margin(NVDA.current_price, contract.strike)
        print(
            f"- PUT strike={contract.strike:.2f}, "
            f"DTE={contract.days_to_expiration(EXAMPLE_DATE)}, "
            f"delta={contract.delta:.2f}, margen={margin:.1%}"
        )


if __name__ == "__main__":
    main()
