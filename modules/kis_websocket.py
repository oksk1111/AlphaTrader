import asyncio
import websockets
import json
import logging
import time
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_MOCK

# US 종목 거래소 매핑
US_EXCHANGE_MAP = {
    'TQQQ': 'NAS', 'SOXL': 'AMS', 'NVDL': 'NAS', 'TECL': 'AMS', 'FNGU': 'AMS',
    'UPRO': 'AMS', 'QQQ': 'NAS', 'SMH': 'NAS', 'SPY': 'AMS', 'SOXX': 'NAS', 'XLK': 'AMS',
}

class KisWebSocket:
    def __init__(self, tickers, callback):
        self.tickers = tickers
        self.callback = callback
        self.approval_key = None
        self.connected = False
        self.task = None
        self._loop = None
        
        # Real/Mock URL differentiation
        if KIS_MOCK:
            self.base_url = "https://openapivts.koreainvestment.com:29443"
            self.ws_url = "ws://ops.koreainvestment.com:31000" # Advanced Mock
        else:
            self.base_url = "https://openapi.koreainvestment.com:9443"
            self.ws_url = "ws://ops.koreainvestment.com:21000" # Real
            
    def get_approval_key(self):
        import requests
        url = f"{self.base_url}/oauth2/Approval"
        headers = {"content-type": "application/json; utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "secretkey": KIS_APP_SECRET
        }
        try:
            res = requests.post(url, headers=headers, data=json.dumps(body))
            res.raise_for_status()
            self.approval_key = res.json()["approval_key"]
            logging.info(f"WebSocket Approval Key Obtained: {self.approval_key[:10]}...")
            return True
        except Exception as e:
            logging.error(f"Failed to get WebSocket Approval Key: {e}")
            return False

    async def connect_loop(self):
        if not self.approval_key:
            if not self.get_approval_key():
                return

        tr_id = "H0STCNT0" if KIS_MOCK else "HDFSZC413000"

        while True:
            try:
                logging.info(f"Connecting to KIS WebSocket: {self.ws_url}/tryitout/{tr_id}")
                async with websockets.connect(f"{self.ws_url}/tryitout/{tr_id}") as websocket:
                    logging.info("WebSocket Connected successfully.")
                    self.connected = True
                    
                    # Subscribe to each ticker
                    for ticker in self.tickers:
                        # US Market code generation
                        # NAS -> DNAS, AMS -> DAMS, NYSE -> DNYS, AMEX -> DAMS. Default DNAS
                        exch = US_EXCHANGE_MAP.get(ticker, 'NAS')
                        exch_code = 'DNAS'
                        if exch == 'AMS':
                            exch_code = 'DAMS'
                        elif exch == 'NYS':
                            exch_code = 'DNYS'
                        
                        stock_code = f"{exch_code}{ticker}"
                        
                        req = {
                            "header": {
                                "approval_key": self.approval_key,
                                "custtype": "P",
                                "tr_type": "1", # Regist
                                "content-type": "utf-8"
                            },
                            "body": {
                                "input": {
                                    "tr_id": tr_id,
                                    "tr_key": stock_code 
                                }
                            }
                        }
                        await websocket.send(json.dumps(req))
                        logging.info(f"WebSocket Subscribed: {ticker} (Key: {stock_code})")
                        
                    while True:
                        data = await websocket.recv()
                        
                        # Data format is separated by |
                        # 0: encrypted(0/1) | 1: tr_id | 2: len | 3: data
                        parts = data.split('|')
                        if len(parts) > 3:
                            payload = parts[3]
                            try:
                                fields = payload.split('^')
                                if len(fields) > 2:
                                    # fields[0]: tr_key (e.g. DNASSOXL, DNASTQQQ, DAMSSOXL)
                                    # fields[2]: Current price
                                    tr_key = fields[0]
                                    current_price_str = fields[2]
                                    price = float(current_price_str)
                                    
                                    # Parse ticker out (remove DNAS/DAMS/DNYS prefix)
                                    ticker_parsed = tr_key[4:] if len(tr_key) > 4 else tr_key
                                    
                                    # Call the async callback safely
                                    if asyncio.iscoroutinefunction(self.callback):
                                        await self.callback(ticker_parsed, price)
                                    else:
                                        self.callback(ticker_parsed, price)
                                    
                            except Exception as e:
                                pass # Parse / Callback error
                                
                        elif data.startswith('{'): # JSON system message (PING or ACK or PONG)
                            msg = json.loads(data)
                            # Handle Ping-Pong if requested
                            header = msg.get('header', {})
                            if header.get('tr_id') == 'PING':
                                # Send PONG back if required, usually KIS closes connections without activity, 
                                # but KIS server handles it.
                                pass
                                
            except Exception as e:
                logging.error(f"WebSocket Connection Disrupted: {e}. Retrying in 5s...")
                self.connected = False
                await asyncio.sleep(5)

    def start_background(self):
        """Starts the WebSocket connection loop in a background thread or async task."""
        try:
            self._loop = asyncio.get_running_loop()
            self.task = self._loop.create_task(self.connect_loop())
            logging.info("WebSocket started as async task in existing event loop.")
        except RuntimeError:
            # No running event loop, run on a new background thread to avoid blocking synchronous callers
            import threading
            def run_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.connect_loop())
            t = threading.Thread(target=run_loop, daemon=True, name="KisWebSocketThread")
            t.start()
            logging.info("WebSocket started in a background daemon thread.")

    def stop(self):
        if self.task:
            self.task.cancel()
            logging.info("WebSocket connection loop stopped.")
