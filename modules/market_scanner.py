from modules.kis_domestic import KisDomestic
from modules.logger import logger

class MarketScanner:
    def __init__(self):
        self.kis = KisDomestic()
        self.discovered_tickers = []  # List of discovered tickers: [{'code': '005930', 'reason': 'Volume Spike'}]

    def scan_volume_spikes(self, min_volume_increase_rate=300, min_price=1000):
        """
        Scan for tickers with sudden volume increase.
        - min_volume_increase_rate: Minimum volume increase rate (%) compared to previous day.
        - min_price: Minimum stock price to filter out penny stocks.
        """
        logger.info("📡 Scanning for Volume Spikes...")
        rank_data = self.kis.get_volume_rank()
        
        candidates = []
        if not rank_data:
            logger.warning("⚠️ No data from Volume Rank API")
            return []

        for item in rank_data:
            ticker = item.get('mksc_shrn_iscd')  # Short code
            name = item.get('hts_kor_isnm')
            price = float(item.get('stck_prpr', '0'))
            vol_rate = float(item.get('vol_inrt', '0')) # Volume Increase Rate
            
            # Filter
            if price < min_price:
                continue
            
            # Additional check: Skip if ETN or SPAC (Improve filter later based on name)
            if "ETN" in name or "스팩" in name or "우B" in name:
                continue

            if vol_rate >= min_volume_increase_rate:
                candidates.append({
                    'code': ticker,
                    'name': name,
                    'price': price,
                    'rate': vol_rate,
                    'reason': f"Volume +{vol_rate:.1f}%"
                })
        
        # Sort by rate desc
        candidates.sort(key=lambda x: x['rate'], reverse=True)
        top_candidates = candidates[:5] # Take top 5
        
        if top_candidates:
             logger.info(f"🔎 Discovered {len(top_candidates)} Volume Spike Tickers: {[c['name'] for c in top_candidates]}")
        
        return top_candidates

    def scan_top_gainers(self, min_gain=10, min_price=1000):
        """Scan for top gainers (>10% increase)"""
        logger.info("📡 Scanning for Top Gainers...")
        rank_data = self.kis.get_fluctuation_rank()
        
        candidates = []
        if not rank_data: return []

        for item in rank_data:
            ticker = item.get('mksc_shrn_iscd') # Might vary based on API response structure check
            if not ticker: ticker = item.get('stck_shrn_iscd')
            
            name = item.get('hts_kor_isnm')
            price = float(item.get('stck_prpr', '0'))
            change_rate = float(item.get('prdy_ctrt', '0')) # Change rate
            
            if price < min_price: continue
            if "ETN" in name or "스팩" in name or "우B" in name: continue
            
            if change_rate >= min_gain:
                candidates.append({
                    'code': ticker,
                    'name': name,
                    'price': price,
                    'rate': change_rate,
                    'reason': f"Price +{change_rate:.1f}%"
                })

        candidates.sort(key=lambda x: x['rate'], reverse=True)
        return candidates[:5]

scanner = MarketScanner()
