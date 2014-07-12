import sys
import time
import xml.etree.ElementTree as ET

from async_pipe import AsyncPipe

try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty  # python 3.x

ON_POSIX = 'posix' in sys.builtin_module_names
if ON_POSIX:
    import os
    import fcntl
    import select

def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()

def enqueue_xml_one_by_one(out, queue):
    """ Stupid, but portable method.  It works"""
    message = ''
    while True:
        acc = out.read(1)
        message += acc
        try:
            xml_message = ET.fromstring(message)
        except ET.ParseError:
            continue
        queue.put(xml_message)
        message = ''

def enqueue_xml(out, queue):
    """Fancy NONBLOCK efficient POSIX only way"""
    flags = fcntl.fcntl(out, fcntl.F_GETFL) # get current p.stdout flags
    fcntl.fcntl(out, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    message = ''
    while True:
        select.select([out], [], [])
        acc = out.read(1024)
        for c in acc:
            message += c
            if c != '>':
                continue
            try:
                xml_message = ET.fromstring(message)
                queue.put(xml_message)
                message = ''
            except ET.ParseError:
                continue

def enqueue_xml_stream(out, queue):
    """Fancyest way, steaming and POSIX only"""
    depth = 0
    s = InfiniteXML(out)
    try:
        for (event, node) in ET.iterparse(s, events=['start', 'end']):
            if event == 'start':
                depth += 1
            elif event == 'end':
                depth -= 1
            # We want the children of <root>
            if depth == 1:
                queue.put(node)
    except ET.ParseError:
        # We hit the end of the stream?
        pass
    finally:
        out.close()

class InfiniteXML (object):
    def __init__(self, out):
        flags = fcntl.fcntl(out, fcntl.F_GETFL) # get current p.stdout flags
        fcntl.fcntl(out, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._root = True
        self.out = out
    def read(self, len=None):
        len = len or 4096
        if self._root:
            self._root=False
            return "<root>"
        else:
            select.select([self.out], [], [])
            text = self.out.read(len)
            return text

    def close(self):
        self.out.close()


if __name__ == "__main__":
    oracle = AsyncPipe(
        dict(args=["python", "xml_oracle.py"], stderr=sys.stdout),
        enqueue_xml_stream)


    for i in range(10):
        oracle.write(str(i*i)+"\n")
        print("")
        print(i)
        try:
            node = oracle.get(True, 0.1)
        except Empty:
            print("No output yet")
            continue

        print("node: " + str(node.text))
