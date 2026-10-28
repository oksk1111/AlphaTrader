from modules.brokers.models import Bar, Market, Quote
from modules.intraday_strategy import IntradayContext, evaluate_intraday, evaluate_overnight


def bars(up=True, count=30):
    out = []
    for i in range(count):
        close = 100 + i * (0.2 if up else -0.2)
        out.append(Bar(
            symbol="XLK", market=Market.US, currency="USD", timestamp=str(i),
            open=close - 0.05, high=close + 0.1, low=close - 0.1,
            close=close, volume=1000 + i * 20, interval="1m",
        ))
    return out


def daily(up=True):
    return [80 + i * (0.5 if up else -0.3) for i in range(80)]


def context(**over):
    minute_bars = over.pop("bars", bars())
    price = over.pop("price", minute_bars[-1].high + 0.2)
    quote = Quote("XLK", Market.US, "USD", price, bid=price - 0.01, ask=price + 0.01)
    values = dict(
        symbol="XLK", market=Market.US, bars=minute_bars, quote=quote,
        daily_closes=daily(), benchmark_return_pct=0.2, relative_volume=1.8,
        minutes_to_close=120, stream_age_sec=0.2,
    )
    values.update(over)
    return IntradayContext(**values)


def test_strong_signal_buys_with_positive_size():
    decision = evaluate_intraday(context())
    assert decision.action == "buy"
    assert decision.score >= 0.62
    assert 0.25 <= decision.size_multiplier <= 1.0


def test_weaker_features_reduce_score_instead_of_binary_block():
    strong = evaluate_intraday(context())
    weak_bars = bars(up=False)
    weak = evaluate_intraday(context(
        bars=weak_bars, price=weak_bars[-1].close,
        daily_closes=daily(up=False), relative_volume=0.7,
        benchmark_return_pct=1.0,
    ))
    assert weak.action == "defer"
    assert weak.score < strong.score
    assert weak.release_condition is None


def test_stale_stream_veto_has_release_condition():
    decision = evaluate_intraday(context(stream_age_sec=7.0))
    assert decision.action == "veto"
    assert "웹소켓" in decision.release_condition


def test_wide_spread_veto_auto_releases():
    ctx = context()
    wide = Quote("XLK", Market.US, "USD", ctx.quote.price,
                 bid=ctx.quote.price * 0.99, ask=ctx.quote.price * 1.01)
    decision = evaluate_intraday(IntradayContext(**{**ctx.__dict__, "quote": wide}))
    assert decision.action == "veto"
    assert "정상화" in decision.release_condition


def test_overnight_keeps_only_strong_position_at_close():
    keep = evaluate_overnight(context(minutes_to_close=10))
    not_close = evaluate_overnight(context(minutes_to_close=60))
    assert keep.keep and not not_close.keep


def test_insufficient_bars_defer_not_crash():
    decision = evaluate_intraday(context(bars=bars(count=10)))
    assert decision.action == "defer"
