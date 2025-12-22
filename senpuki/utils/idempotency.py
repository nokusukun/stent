import hashlib
import logging
from typing import Any # Import Any
from senpuki.utils.serialization import Serializer, JsonSerializer

logger = logging.getLogger(__name__)

def default_idempotency_key(
    fn_name: str,
    version: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    serializer: Serializer | None = None,
) -> str:
    if serializer is None:
        serializer = JsonSerializer()
    
    # Simple structure to hash
    data = {
        "fn": fn_name,
        "version": version,
        "args": args,
        "kwargs": kwargs,
    }
    
    try:
        payload = serializer.dumps(data)
    except Exception:
        # Fallback if args aren't serializable by the default serializer
        payload = str(data).encode("utf-8")

    key = hashlib.sha256(payload).hexdigest()
    logger.debug(f"Generated idempotency key for {fn_name}: {key}")
    return key