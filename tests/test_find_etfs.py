import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.kis_domestic import KisDomestic
import pprint

def test_kis():
    kis = KisDomestic()
    print("Testing Trading Value Rank...")
    data = kis.get_trading_value_rank()
    
    etfs = []
    if data:
        for item in data[:200]:
            name = item.get('hts_kor_isnm', '')
            code = item.get('mksc_shrn_iscd', '')
            if any(k in name for k in ['KODEX', 'TIGER', 'ACE', 'KBSTAR', 'KOACT', 'RISE', 'TIME']):
                if '인버스' not in name and '선물' not in name and '레버리지' not in name:
                    etfs.append(f"{name} ({code})")
    print("Found ETFs:", etfs)

if __name__ == '__main__':
    test_kis()