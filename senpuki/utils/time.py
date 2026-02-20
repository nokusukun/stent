from datetime import datetime, timedelta, timezone
import re

def parse_duration(duration: str | dict | timedelta) -> timedelta:
    if isinstance(duration, timedelta):
        return duration
        
    if isinstance(duration, dict):
        return timedelta(**duration)
        
    if not isinstance(duration, str):
        raise TypeError(f"Duration must be str, dict, or timedelta, got {type(duration)}")

    text = duration.strip()
    if not text:
        raise ValueError(f"Invalid duration string: {duration}")

    # Parse composite duration strings like "2d8h", "1h30m", "10s"
    token_re = re.compile(r"(\d+(?:\.\d*)?)([smhdw])")
    total_seconds = 0.0
    pos = 0
    for match in token_re.finditer(text):
        if match.start() != pos:
            raise ValueError(f"Invalid duration string: {duration}")

        value_str, unit = match.groups()
        value = float(value_str)
        if unit == 's':
            total_seconds += value
        elif unit == 'm':
            total_seconds += value * 60
        elif unit == 'h':
            total_seconds += value * 3600
        elif unit == 'd':
            total_seconds += value * 86400
        elif unit == 'w':
            total_seconds += value * 604800

        pos = match.end()

    if pos == 0 or pos != len(text):
        raise ValueError(f"Invalid duration string: {duration}")

    return timedelta(seconds=total_seconds)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)
