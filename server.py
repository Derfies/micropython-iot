# server_cp.py Server for IOT communications.

# Released under the MIT licence.
# Copyright (C) Peter Hinch 2019

# Maintains bidirectional full-duplex links between server applications and
# multiple WiFi connected clients. Each application instance connects to its
# designated client. Connections are resilient and recover from outages of WiFi
# and of the connected endpoint.
# This server and the server applications are assumed to reside on a device
# with a wired interface on the local network.

# Run under CPython 3.5+ or MicroPython Unix build
import sys
from . import gmid, isnew  # __init__.py

upython = sys.implementation.name == 'micropython'
if upython:
    import usocket as socket
    import uasyncio as asyncio
    import utime as time
    import uselect as select
    import uerrno as errno
    from . import Lock
else:
    import socket
    import asyncio
    import time
    import select
    import errno
    Lock = asyncio.Lock

# Read the node ID. There isn't yet a Connection instance.
# CPython does not have socket.readline. Return 1st string received
# which starts with client_id.

# Note re OSError: did detect errno.EWOULDBLOCK. Not supported in MicroPython.
# In cpython EWOULDBLOCK == EAGAIN == 11.
async def _readid(s):
    data = ''
    start = time.time()
    while True:
        try:
            d = s.recv(4096).decode()
        except OSError as e:
            err = e.args[0]
            if err == errno.EAGAIN:
                if (time.time() - start) > TO_SECS:
                    raise OSError  # Timeout waiting for data
                else:
                    # Waiting for data from client. Limit CPU overhead. 
                    await asyncio.sleep(TIM_TINY)
            else:
                raise OSError  # Reset by peer 104
        else:
            if d == '':
                raise OSError  # Reset by peer or t/o
            data = ''.join((data, d))
            if data.find('\n') != -1:  # >= one line
                return data


# API: application calls server.run()
# Allow 2 extra connections. This is to cater for error conditions like
# duplicate or unexpected clients. Accept the connection and have the
# Connection class produce a meaningful error message.
async def run(loop, expected, verbose=False, port=8123, timeout=1500):
    addr = socket.getaddrinfo('0.0.0.0', port, 0, socket.SOCK_STREAM)[0][-1]
    s_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # server socket
    s_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s_sock.bind(addr)
    s_sock.listen(len(expected) + 2)
    global TO_SECS
    global TIMEOUT
    global TIM_SHORT
    global TIM_TINY
    TIMEOUT = timeout
    TO_SECS = timeout / 1000  # ms to seconds
    TIM_SHORT = TO_SECS / 10  # Delay << timeout
    TIM_TINY = 0.05  # Short delay avoids 100% CPU utilisation in busy-wait loops
    verbose and print('Awaiting connection.', port)
    poller = select.poll()
    poller.register(s_sock, select.POLLIN)
    while True:
        res = poller.poll(1)  # 1ms block
        if len(res):  # Only s_sock is polled
            c_sock, _ = s_sock.accept()  # get client socket
            c_sock.setblocking(False)
            try:
                data = await _readid(c_sock)
            except OSError:
                c_sock.close()
            else:
                client_id, init_str = data.split('\n', 1)
                verbose and print('Got connection from client', client_id)
                Connection.go(loop, client_id, init_str, verbose, c_sock,
                              s_sock, expected)
        await asyncio.sleep(0.2)


# A Connection persists even if client dies (minimise object creation).
# If client dies Connection is closed: ._close() flags this state by closing its
# socket and setting .sock to None (.status() == False).
class Connection:
    _conns = {}  # index: client_id. value: Connection instance
    _expected = set()  # Expected client_id's
    _server_sock = None

    @classmethod
    def go(cls, loop, client_id, init_str, verbose, c_sock, s_sock, expected):
        if cls._server_sock is None:  # 1st invocation
            cls._server_sock = s_sock
            cls._expected.update(expected)
        if client_id in cls._conns:  # Old client, new socket
            if cls._conns[client_id].status():
                print('Duplicate client {} ignored.'.format(client_id))
                c_sock.close()
            else:  # Reconnect after failure
                cls._conns[client_id].reconnect(c_sock)
        else: # New client: instantiate Connection
            Connection(loop, c_sock, client_id, init_str, verbose)

    # Server-side app waits for a working connection
    @classmethod
    async def client_conn(cls, client_id):
        while True:
            if client_id in cls._conns:
                c = cls._conns[client_id]
                # await c 
                # works but under CPython produces runtime warnings. So do:
                await c._status_coro()
                return c
            await asyncio.sleep(0.5)

    # App waits for all expected clients to connect.
    @classmethod
    async def wait_all(cls, client_id=None, peers=None):
        conn = None
        if client_id is not None:
            conn = await client_conn(client_id)
        if peers is None:  # Wait for all expected clients
            while len(cls._expected):
                await asyncio.sleep(0.5)
        else:
            while not set(cls._conns.keys()).issuperset(peers):
                await asyncio.sleep(0.5)
        return conn

    @classmethod
    def close_all(cls):
        for conn in cls._conns.values():
            conn._close()
        if cls._server_sock is not None:
            cls._server_sock.close()

    def __init__(self, loop, c_sock, client_id, init_str, verbose):
        self.loop = loop
        self.sock = c_sock  # Socket
        self.client_id = client_id
        self.verbose = verbose
        Connection._conns[client_id] = self
        try:
            Connection._expected.remove(client_id)
        except KeyError:
            print('Unknown client {} has connected. Expected {}.'.format(
                client_id, Connection._expected))

        self._init = True  # Server power-up
        self._wr_pause = True  # Initial or subsequent client connection
        self.getmid = gmid()  # Generator for message ID's
        self.lock = Lock()
        loop.create_task(self._keepalive())
        self.lines = []
        loop.create_task(self._read(init_str))

    def reconnect(self, c_sock):
        self.sock = c_sock
        self._wr_pause = True

    async def _read(self, init_str):
        while True:
            # Start (or restart after outage). Do this promptly.
            # Fast version of await self._status_coro()
            while self.sock is None:
                await asyncio.sleep(TIM_TINY)
            buf = bytearray(init_str.encode('utf8'))
            start = time.time()
            while self.status():
                try:
                    d = self.sock.recv(4096)
                except OSError as e:
                    err = e.args[0]
                    if err == errno.EAGAIN:  # Would block: try later
                        if time.time() - start > TO_SECS:
                            self._close()  # Unless it timed out.
                        else:
                            # Waiting for data from client. Limit CPU overhead.
                            await asyncio.sleep(TIM_TINY)
                    else:
                        self._close()  # Reset by peer 104
                else:
                    start = time.time()  # Something was received
                    if d == b'':  # Reset by peer
                        self._close()
                    buf.extend(d)
                    l = bytes(buf).decode().split('\n')
                    if len(l) > 1:  # Have at least 1 newline
                        self.lines.extend(l[:-1])
                        buf = bytearray(l[-1].encode('utf8'))

    def status(self):
        return self.sock is not None

    def __await__(self):
        if upython:
            while not self.status():
                yield TIM_SHORT
        else:  # CPython: Meet requirement for generator in __await__
            return self._status_coro().__await__()

    __iter__ = __await__

    async def _status_coro(self):
        while not self.status():
            await asyncio.sleep(TIM_SHORT)

    async def readline(self):
        while True:
            if self.verbose and not self.status():
                print('Reader Client:', self.client_id, 'awaiting OK status')
            await self._status_coro()
            self.verbose and print('Reader Client:', self.client_id, 'OK')
            while self.status():
                if len(self.lines):
                    line = self.lines.pop(0)
                    if len(line):  # Ignore keepalives
                        # Discard dupes: get message ID
                        mid = int(line[0:2], 16)
                        # mid == 0 : client has power cycled
                        if not mid:
                            isnew(-1)
                        # _init : server has powered up
                        if self._init or not mid or isnew(mid):
                            self._init = False
                            return ''.join((line[2:], '\n'))

                await asyncio.sleep(TIM_TINY)  # Limit CPU utilisation
            self.verbose and print('Read client disconnected: closing connection.')
            self._close()

    async def _keepalive(self):
        to = TO_SECS * 2 / 3
        while True:
            await self._vwrite('\n')
            await asyncio.sleep(to)

    # qos>0 Repeat tx if outage occurred after initial tx (1st may have been lost)
    async def _do_qos(self, buf):
        await asyncio.sleep(TO_SECS)
        if self.status():
            return
        await self._vwrite(buf)
        self.verbose and print('Repeat', buf, 'to server app')

    async def write(self, line, pause=True):
        fstr =  '{:02x}{}' if line.endswith('\n') else '{:02x}{}\n'
        buf = fstr.format(next(self.getmid), line)  # Local copy
        end = time.time() + TO_SECS
        await self._vwrite(buf)
        # Ensure qos by conditionally repeating the message
        self.loop.create_task(self._do_qos(buf))
        if pause:  # Throttle rate of non-keepalive messages
            dt = end - time.time()
            if dt > 0:
                await asyncio.sleep(dt)  # Control tx rate: <= 1 msg per timeout period

    async def _vwrite(self, buf):  # Verbatim write: add no message ID
        ok = False
        while not ok:
            if self.verbose and not self.status():
                print('Writer Client:', self.client_id, 'awaiting OK status')
            await self._status_coro()
            if self._wr_pause:  # Initial or subsequent connection
                self._wr_pause = False
                await asyncio.sleep(0.2)  # TEST give client time

#            self.verbose and print('Writer Client:', self.client_id, 'OK')
            async with self.lock:  # >1 writing task?
                ok = await self._send(buf)  # Fail clears status

    # Send a string as bytes. Return True on apparent success, False on failure.
    async def _send(self, d):
        if not self.status():
            return False
        d = d.encode('utf8')
        start = time.time()
        while len(d):
            try:
                ns = self.sock.send(d)  # Raise OSError if client fails
            except OSError:
                break
            else:
                d = d[ns:]
                if len(d):
                    await asyncio.sleep(TIM_SHORT)
                    if (time.time() - start) > TO_SECS:
                        break
        else:
            return True  # Success
        self.verbose and print('Write fail: closing connection.')
        self._close()
        return False

    def __getitem__(self, client_id):  # Return a Connection of another client
        return Connection._conns[client_id]

    def _close(self):
        if self.sock is not None:
            self.verbose and print('fail detected')
            self.sock.close()
            self.sock = None

# API aliases
client_conn = Connection.client_conn
wait_all = Connection.wait_all
