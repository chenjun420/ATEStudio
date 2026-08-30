import asyncio


class MemoryStorage[T]:
    def __init__(self) -> None:
        self._data: dict[str, T] = {}
        self._lock = asyncio.Lock()

    async def list(self) -> list[T]:
        async with self._lock:
            return list(self._data.values())

    async def get(self, id: str) -> T | None:
        async with self._lock:
            return self._data.get(id)

    async def create(self, id: str, item: T) -> T:
        async with self._lock:
            self._data[id] = item
            return item

    async def update(self, id: str, item: T) -> T | None:
        async with self._lock:
            if id not in self._data:
                return None
            self._data[id] = item
            return item

    async def delete(self, id: str) -> bool:
        async with self._lock:
            if id in self._data:
                del self._data[id]
                return True
            return False

    async def count(self) -> int:
        async with self._lock:
            return len(self._data)
