"""Ejemplo reproducible usando el puerto de market data."""
from datetime import date
from options_scanner.filters import safety_margin
from options_scanner.market_data import FakeMarketDataProvider
from options_scanner.scanner import scan_puts

def main() -> None:
    as_of = date(2026, 8, 20)
    provider = FakeMarketDataProvider()
    underlying = provider.get_underlying("NVDA")
    print(f"Candidatas para NVDA a {as_of.isoformat()}:")
    for quote in scan_puts(provider, "NVDA", as_of):
        contract = quote.contract
        margin = safety_margin(underlying.current_price, contract.strike)
        print(f"- PUT strike={contract.strike:.2f}, DTE={contract.days_to_expiration(as_of)}, delta={quote.delta:.2f}, distancia_al_strike={margin:.1%}")

if __name__ == "__main__":
    main()
