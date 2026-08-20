from __future__ import annotations

import argparse
import json
import sys

from .provider import IbkrError, IbkrMarketDataProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico de conectividad IBKR (solo lectura)")
    parser.add_argument("--base-url", default="https://localhost:5000/v1/api")
    parser.add_argument("--insecure", action="store_true", help="No verificar el certificado TLS local")
    parser.add_argument("--contracts", type=int, default=3, help="Número máximo de contratos")
    args = parser.parse_args()
    provider = IbkrMarketDataProvider(args.base_url, verify_tls=not args.insecure)
    try:
        provider.require_authenticated_session()
        stock = provider.find_stock("NVDA")
        price = provider.stock_price(int(stock["conid"]))
        expirations = provider.option_expirations(stock)
        month = expirations[0]
        strikes = provider.put_strikes(int(stock["conid"]), month)
        nearest = sorted(strikes, key=lambda strike: abs(strike - price))[:max(1, args.contracts)]
        contracts = provider.put_contracts(int(stock["conid"]), month, nearest)
        market_data = provider.option_market_data(contracts[:args.contracts])
        print(json.dumps({"symbol": "NVDA", "conid": stock["conid"], "price": price,
                          "expirations": expirations, "selected_expiration": month,
                          "put_strikes": strikes, "contracts": market_data}, indent=2))
        missing = sorted({key for row in market_data for key in ("bid", "ask", "delta", "theta", "iv", "open_interest") if row[key] is None})
        if missing:
            print("Aviso: market data parcial; campos no disponibles: " + ", ".join(missing), file=sys.stderr)
        return 0
    except IbkrError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
