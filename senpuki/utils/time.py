from datetime import datetime, timedelta
import re

def parse_duration(duration: str | dict | timedelta) -> timedelta:
    if isinstance(duration, timedelta):
        return duration
        
    if isinstance(duration, dict):
        return timedelta(**duration)
        
    if not isinstance(duration, str):
        raise TypeError(f"Duration must be str, dict, or timedelta, got {type(duration)}")

    # Parse composite duration strings like "2d8h", "1h30m", "10s"
    # We look for all matches of (value)(unit)
    matches = re.findall(r"(\d+(?:\.\d*)?)([smhdw])", duration)
    
    if not matches:
         raise ValueError(f"Invalid duration string: {duration}")

    total_seconds = 0.0
    for value_str, unit in matches:
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
            
    return timedelta(seconds=total_seconds)

def now_utc() -> datetime:
    return datetime.now() # naive for simplicity, or use timezone.utc