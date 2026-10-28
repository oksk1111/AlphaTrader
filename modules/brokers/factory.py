"""환경 설정에 따른 브로커 생성."""

import os

from modules.brokers.base import Broker


def get_broker(name: str = None) -> Broker:
    broker_name = (name or os.getenv("BROKER", "toss")).strip().lower()
    if broker_name == "toss":
        from modules.brokers.toss import TossBroker
        return TossBroker()
    raise ValueError(f"지원하지 않는 브로커: {broker_name}")
