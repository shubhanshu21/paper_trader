"""
tests/test_single_instance_lock.py — utils/single_instance_lock.py,
added so custom_strategy_scheduler.py (which places REAL broker orders)
can never accidentally run twice on the same machine — see that module's
docstring for the double-order scenario this prevents. Uses a real
temporary lock directory (flock is a real OS primitive, not something
meaningfully mockable) — no network/real app state.
"""
import fcntl
import importlib

import pytest


@pytest.fixture()
def lock_module(tmp_path, monkeypatch):
    """A fresh import of the module with _LOCK_DIR redirected to a scratch tmp_path — isolates each test's locks from each other and from logs/ in the real repo."""
    import automate.utils.single_instance_lock as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_LOCK_DIR", tmp_path)
    monkeypatch.setattr(mod, "_held_locks", {})
    return mod


class TestAcquireSingletonLock:
    def test_first_acquirer_succeeds(self, lock_module):
        assert lock_module.acquire_singleton_lock("scheduler") is True

    def test_second_acquirer_in_the_same_process_is_refused(self, lock_module):
        """flock is per-process, not per-call — a second attempt while the first handle is still open must fail, simulating two instances on one machine."""
        assert lock_module.acquire_singleton_lock("scheduler") is True
        assert lock_module.acquire_singleton_lock("scheduler") is False

    def test_different_lock_names_are_independent(self, lock_module):
        assert lock_module.acquire_singleton_lock("scheduler") is True
        assert lock_module.acquire_singleton_lock("something_else") is True

    def test_lock_is_released_by_a_second_process_after_the_first_closes_its_handle(self, lock_module, tmp_path):
        """Simulates the crash-recovery property: closing/dropping the fd (what happens automatically on process exit, including a crash) releases the OS-level lock immediately, no manual cleanup needed."""
        assert lock_module.acquire_singleton_lock("scheduler") is True
        held_fh = lock_module._held_locks["scheduler"]
        held_fh.close()  # simulates process exit — the OS releases the flock the instant the fd closes

        # A brand-new handle on the same path can now acquire it (no stale lock left behind).
        path = tmp_path / "scheduler.lock"
        with open(path, "w") as fh2:
            try:
                fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        assert acquired is True
