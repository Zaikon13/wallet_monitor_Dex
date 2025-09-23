# core/watch.py
class _Watcher:
    def poll_once(self) -> int:
        return 0  # no-op for now

def make_from_env():
    return _Watcher()
