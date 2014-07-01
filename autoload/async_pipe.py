import sys
import subprocess
import threading

try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty  # python 3.x

ON_POSIX = 'posix' in sys.builtin_module_names

class AsyncPipe (object):
    def __init__(self, subprocess_kwargs, parser):
        """Wraps a subprocess and thread reading from it

        subprocess_kwargs needs to include `args`,
        which names the program to run.
        parser is passed the processes stdout, and the queue.
        """
        self.proc = subprocess.Popen(stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     bufsize=1, close_fds=ON_POSIX,
                                     **subprocess_kwargs)
        self.queue = Queue()
        self.io_thread = threading.Thread(
            target=parser,
            args=(self.proc.stdout, self.queue))

        self.io_thread.daemon = True # thread dies with the program
        self.io_thread.start()

    def get(self, block=True, timeout=None):
        return self.queue.get(block, timeout)

    def get_nowait(self):
        return self.queue.get_nowait()

    def write(self, string):
        self.proc.stdin.write(string)

