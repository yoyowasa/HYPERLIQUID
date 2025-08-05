"""
簡易リトライ & 指数バックオフデコレータ
    @retry_async(max_attempts=5, base_delay=0.5)
    async def api_call(...):
        ...
"""

import asyncio
import functools
from typing import Callable, TypeVar

_T = TypeVar("_T")


def retry_async(max_attempts: int = 5, base_delay: float = 0.5) -> Callable[[_T], _T]:
    def decorator(func: _T) -> _T:  # type: ignore[valid-type]
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for n in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception:  # noqa: BLE001
                    if n == max_attempts:
                        raise
                    await asyncio.sleep(base_delay * 2 ** (n - 1))

        return wrapper  # type: ignore[return-value]

    return decorator
