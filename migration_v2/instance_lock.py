"""One worker per local store. OS releases this lock after a crash."""
import os


class InstanceLock:
    def __init__(self, root):
        self.file = (root / 'worker.lock').open('a+b')
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('This project store is already open in another application instance') from None

    def close(self):
        if not self.file.closed:
            self.file.close()
