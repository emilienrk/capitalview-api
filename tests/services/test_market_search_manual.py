import sys
import os

# Add backend directory to sys.path so we can import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from services.market_data.providers.yahoo import YahooProvider

def test_manual_search_evolution():
    """
    Manual test to visualize search results evolution for 'l', 'lv', 'lvm', 'lmvh'.
    Run with: python tests/services/test_market_search_manual.py
    """
    try:
        provider = YahooProvider()
        steps = ["l", "lv", "lvm", "lmvh"]
        
        print("\n\n=== STARTING MANUAL SEARCH TEST ===\n")
        
        for query in steps:
            print(f"\n--- Searching for: '{query}' ---")
            try:
                results = provider.search(query)
                print(f"Found {len(results)} results.")
                
                # Try to fetch bulk info to get ISINs
                symbols = [r['symbol'] for r in results[:5] if r.get('symbol')]
                print(f"  Fetching bulk info for: {symbols}")
                bulk_info = provider.get_bulk_info(symbols)
                
                for i, res in enumerate(results[:5]): # Show top 5
                    symbol = res.get('symbol', 'N/A')
                    name = res.get('name', 'N/A')
                    exchange = res.get('exchange', 'N/A')
                    atype = res.get('type', 'N/A')
                    
                    # Check if we got ISIN from bulk info
                    isin = res.get('isin')
                    if not isin and symbol in bulk_info:
                        isin = bulk_info[symbol].get('isin')
                        if isin:
                            res['isin'] = isin # Update result for display
                            print(f"    -> Found ISIN for {symbol}: {isin}")
                    
                    isin_display = isin if isin else "N/A"
                    print(f"  {i+1}. Symbol: {symbol:<10} | Name: {name:<30} | ISIN: {isin_display:<15} | Exch: {exchange:<10} | Type: {atype}")
            except Exception as e:
                print(f"Error searching for '{query}': {e}")

        print("\n=== END OF TEST ===\n")
    except Exception as e:
        print(f"Failed to initialize provider or run test: {e}")

if __name__ == "__main__":
    test_manual_search_evolution()
