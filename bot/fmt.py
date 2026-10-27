from datetime import datetime, timezone


def fmt_duration(sec: int) -> str:
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_money(usd: float) -> str:
    return f"${usd:.4f}"


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_time(ts: int | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
