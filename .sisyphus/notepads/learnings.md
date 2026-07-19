
# Learnings - ResumeManager Task

- Removed __slots__ from class to allow mocking internal methods in tests
- Used asyncio.Queue for pending messages with asyncio.wait_for() for timeout
- Implemented exponential backoff: base_backoff * (2 ** (retry_count - 1))
- ResumeManager accesses cache._lock and cache._db directly for raw database access
- Used patch.object() with new_callable=AsyncMock for patching async methods in tests
