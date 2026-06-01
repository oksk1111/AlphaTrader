from modules.portfolio_manager import PortfolioManager
import pprint

def test_manager():
    pm = PortfolioManager()
    portfolio = pm.generate_and_save_portfolio(max_kr_etf=10)
    print("\n--- Created Portfolio ---")
    pprint.pprint(portfolio)

if __name__ == '__main__':
    test_manager()