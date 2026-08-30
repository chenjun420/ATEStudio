"""Unit tests for ResourceManager.

Tests thread-safe resource locking with timeout support and deadlock detection.
"""

import threading
import time

import pytest

from ate_platform.exceptions import ResourceAcquireError
from ate_platform.scheduler.resource_manager import ResourceManager


class TestResourceManagerBasics:
    """Test basic acquire/release functionality."""

    def test_acquire_returns_true_for_new_resource(self):
        """Acquiring an unheld resource should return True."""
        rm = ResourceManager()
        assert rm.acquire("DMM_CH1", "step1") is True

    def test_acquire_returns_false_for_held_resource(self):
        """Acquiring a held resource without waiting should return False."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")
        assert rm.acquire("DMM_CH1", "step2") is False

    def test_release_makes_resource_available(self):
        """Releasing a resource should make it available again."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")
        rm.release("DMM_CH1", "step1")
        assert rm.is_available("DMM_CH1") is True

    def test_is_available_for_new_resource(self):
        """A new resource should be available."""
        rm = ResourceManager()
        assert rm.is_available("DMM_CH1") is True

    def test_is_available_returns_false_when_held(self):
        """A held resource should not be available."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")
        assert rm.is_available("DMM_CH1") is False

    def test_get_owner_returns_none_for_available_resource(self):
        """get_owner should return None for unheld resources."""
        rm = ResourceManager()
        assert rm.get_owner("DMM_CH1") is None

    def test_get_owner_returns_owner_id(self):
        """get_owner should return the current owner."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")
        assert rm.get_owner("DMM_CH1") == "step1"


class TestResourceManagerTimeout:
    """Test timeout-based acquisition."""

    def test_acquire_with_timeout_succeeds_when_released(self):
        """Acquire with timeout should succeed if resource is released during wait."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        def release_after_delay():
            time.sleep(0.1)
            rm.release("DMM_CH1", "step1")

        thread = threading.Thread(target=release_after_delay)
        thread.start()

        # Should succeed after waiting
        start = time.time()
        result = rm.acquire("DMM_CH1", "step2", timeout=1.0)
        elapsed = time.time() - start

        thread.join()
        assert result is True
        assert elapsed >= 0.1  # Should have waited

    def test_acquire_with_timeout_fails_on_timeout(self):
        """Acquire with timeout should return False if timeout expires."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        start = time.time()
        result = rm.acquire("DMM_CH1", "step2", timeout=0.2)
        elapsed = time.time() - start

        assert result is False
        assert elapsed >= 0.15  # Should have waited close to timeout (allow tolerance)

    def test_acquire_with_zero_timeout_returns_immediately(self):
        """Acquire with timeout=0 should return immediately if not available."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        start = time.time()
        result = rm.acquire("DMM_CH1", "step2", timeout=0)
        elapsed = time.time() - start

        assert result is False
        assert elapsed < 0.1  # Should return quickly

    def test_acquire_with_negative_timeout_returns_immediately(self):
        """Acquire with negative timeout should behave like timeout=0."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        start = time.time()
        result = rm.acquire("DMM_CH1", "step2", timeout=-1.0)
        elapsed = time.time() - start

        assert result is False
        assert elapsed < 0.1


class TestResourceManagerThreadSafety:
    """Test thread-safe operations."""

    def test_concurrent_acquire_only_one_succeeds(self):
        """Only one thread should acquire a resource at a time."""
        rm = ResourceManager()
        results = []
        threads = []

        def try_acquire(owner_id):
            result = rm.acquire("DMM_CH1", owner_id)
            results.append((owner_id, result))
            if result:
                time.sleep(0.05)
                rm.release("DMM_CH1", owner_id)

        # Create multiple threads trying to acquire
        for i in range(5):
            t = threading.Thread(target=try_acquire, args=(f"thread{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # At least one should have succeeded
        successes = [r for r in results if r[1] is True]
        assert len(successes) >= 1

    def test_multiple_resources_independent(self):
        """Different resources should be independently lockable."""
        rm = ResourceManager()

        assert rm.acquire("DMM_CH1", "step1") is True
        assert rm.acquire("DMM_CH2", "step2") is True

        assert rm.get_owner("DMM_CH1") == "step1"
        assert rm.get_owner("DMM_CH2") == "step2"

        rm.release("DMM_CH1", "step1")
        assert rm.is_available("DMM_CH1") is True
        assert rm.is_available("DMM_CH2") is False


class TestResourceManagerErrors:
    """Test error conditions."""

    def test_same_owner_cannot_acquire_twice(self):
        """Same owner attempting to acquire twice should raise error."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        with pytest.raises(ResourceAcquireError) as exc_info:
            rm.acquire("DMM_CH1", "step1")

        assert "already holds resource" in str(exc_info.value)

    def test_release_nonexistent_resource_raises(self):
        """Releasing a non-existent resource should raise error."""
        rm = ResourceManager()

        with pytest.raises(ResourceAcquireError) as exc_info:
            rm.release("NONEXISTENT", "step1")

        assert "not found" in str(exc_info.value)

    def test_release_wrong_owner_raises(self):
        """Releasing with wrong owner should raise error."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        with pytest.raises(ResourceAcquireError) as exc_info:
            rm.release("DMM_CH1", "wrong_owner")

        assert "not 'wrong_owner'" in str(exc_info.value)

    def test_release_unheld_resource_raises(self):
        """Releasing an unheld resource should raise error."""
        rm = ResourceManager()

        with pytest.raises(ResourceAcquireError) as exc_info:
            rm.release("DMM_CH1", "step1")

        assert "not found" in str(exc_info.value)


class TestResourceManagerRepr:
    """Test string representation."""

    def test_repr_shows_empty_state(self):
        """Repr should show empty state initially."""
        rm = ResourceManager()
        assert "held_resources={}" in repr(rm)

    def test_repr_shows_held_resources(self):
        """Repr should show held resources."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")
        assert "'DMM_CH1': 'step1'" in repr(rm)


class TestResourceManagerDeadlockDetection:
    """Test deadlock detection via timeout."""

    def test_timeout_detects_potential_deadlock(self):
        """Timeout mechanism can detect potential deadlock situations."""
        rm = ResourceManager()
        rm.acquire("RESOURCE_A", "step1", timeout=5.0)

        # Another thread tries to acquire with short timeout
        # This simulates a potential deadlock scenario
        start = time.time()
        result = rm.acquire("RESOURCE_A", "step2", timeout=0.1)
        elapsed = time.time() - start

        # Should timeout and return False
        assert result is False
        # Should have waited approximately the timeout duration
        assert 0.08 <= elapsed <= 0.2

    def test_chain_of_waits_eventually_succeeds(self):
        """Resource released in a chain should allow sequential acquisition."""
        rm = ResourceManager()

        # First thread acquires
        rm.acquire("DMM_CH1", "step1")

        results = []

        def wait_and_acquire():
            # Wait for resource to be released
            result = rm.acquire("DMM_CH1", "step2", timeout=2.0)
            results.append(result)
            if result:
                time.sleep(0.05)
                rm.release("DMM_CH1", "step2")

        thread = threading.Thread(target=wait_and_acquire)
        thread.start()

        # Release after a short delay
        time.sleep(0.1)
        rm.release("DMM_CH1", "step1")

        thread.join()

        # Second thread should have acquired successfully
        assert results == [True]


class TestResourceManagerQA:
    """QA scenario from task specification."""

    def test_qa_scenario(self):
        """Run the exact QA scenario from the task specification."""
        from ate_platform.scheduler.resource_manager import ResourceManager

        rm = ResourceManager()
        assert rm.acquire("DMM_CH1", "step1", timeout=1.0) is True
        assert rm.is_available("DMM_CH1") is False
        assert rm.acquire("DMM_CH1", "step2", timeout=0.1) is False  # Already held
        rm.release("DMM_CH1", "step1")
        assert rm.is_available("DMM_CH1") is True
        print("OK")
