# client.py Client class for resilient asynchronous IOT communication link.

# Released under the MIT licence.
# Copyright (C) Peter Hinch, Kevin Köck 2018

# After sending ID now pauses before sending further data to allow server to
# initiate read task.

import gc

gc.collect()
import usocket as socket
import uasyncio as asyncio
gc.collect()
from sys import platform
import network
import utime
import machine
import errno
from . import gmid, isnew, launch, Event, Lock, SetByte  # __init__.py
gc.collect()
from micropython import const
WDT_SUSPEND = const(-1)
WDT_CANCEL = const(-2)
WDT_CB = const(-3)

ESP32 = platform == 'esp32' or platform == 'esp32_LoBo'

# Message ID generator
getmid = gmid()
gc.collect()


class Client:
    def __init__(self, loop, my_id, server, ssid='', pw='',
                 port=8123, timeout=1500,
                 conn_cb=None, conn_cb_args=None,
                 verbose=False, led=None, wdog=False):
        self._loop = loop
        self._my_id = my_id if my_id.endswith('\n') else '{}{}'.format(my_id, '\n')
        self._server = server
        self._ssid = ssid
        self._pw = pw
        self._port = port
        self._to = timeout  # Client and server timeout
        self._tim_short = timeout // 10
        self._tim_ka = timeout // 2  # Keepalive interval
        self._concb = conn_cb
        self._concbargs = () if conn_cb_args is None else conn_cb_args
        self._verbose = verbose
        self._led = led
        self._wdog = wdog

        if wdog:
            if platform == 'pyboard':
                self._wdt = machine.WDT(0, 20000)
                def wdt():
                    def inner(feed=0):  # Ignore control values
                        if not feed:
                            self._wdt.feed()
                    return inner
                self._feed = wdt()
            else:
                def wdt(secs=0):
                    timer = machine.Timer(-1)
                    timer.init(period=1000, mode=machine.Timer.PERIODIC,
                               callback=lambda t:self._feed())
                    cnt = secs
                    run = False  # Disable until 1st feed
                    def inner(feed=WDT_CB):
                        nonlocal cnt, run, timer
                        if feed == 0:  # Fixed timeout
                            cnt = secs
                            run = True
                        elif feed < 0:  # WDT control/callback
                            if feed == WDT_SUSPEND:
                                run = False  # Temporary suspension
                            elif feed == WDT_CANCEL:
                                timer.deinit()  # Permanent cancellation
                            elif feed == WDT_CB and run:  # Timer callback and is running.
                                cnt -= 1
                                if cnt <= 0:
                                    reset()
                    return inner
                self._feed = wdt(20)
        else:
            self._feed = lambda x: None

        if platform == 'esp8266':
            self._sta_if = network.WLAN(network.STA_IF)
            ap = network.WLAN(network.AP_IF) # create access-point interface
            ap.active(False)         # deactivate the interface
        elif platform == 'pyboard':
            self._sta_if = network.WLAN()
        elif ESP32:
            self._sta_if = network.WLAN(network.STA_IF)
        else:
            raise OSError(platform, 'is unsupported.')

        self._sta_if.active(True)
        gc.collect()

        self._evfail = Event(100)  # 100ms pause
        self._evread = Event()  # Respond fast to incoming
        self._s_lock = Lock(100)  # For internal send conflict.
        self._last_wr = utime.ticks_ms()
        self.connects = 0  # Connect count for test purposes/app access
        self._sock = None
        self._ok = False  # Set after 1st successful read
        self._acks_pend = SetByte()  # ACKs which are expected to be received
        gc.collect()
        loop.create_task(self._run(loop))

    # **** API ****
    def __iter__(self):  # App can await a connection
        while not self._ok:
            yield from asyncio.sleep_ms(500)

    def status(self):
        return self._ok

    async def readline(self):
        await self._evread
        d = self._evread.value()
        self._evread.clear()
        return d

    async def write(self, buf, qos=True, wait=True):
        if qos and wait:  # Disallow concurrent writes
            while self._acks_pend:
                await asyncio.sleep_ms(50)
        # Prepend message ID to a copy of buf
        fstr =  '{:02x}{}' if buf.endswith('\n') else '{:02x}{}\n'
        mid = next(getmid)
        self._acks_pend.add(mid)
        buf = fstr.format(mid, buf)
        await self._write(buf)
        if qos:  # Return when an ACK received
            await self._do_qos(mid, buf)

    def close(self):
        self._close()  # Close socket and WDT
        self._feed(WDT_CANCEL)

    # **** For subclassing ****

    # May be overridden e.g. to provide timeout (asyncio.wait_for)
    async def bad_wifi(self):
        if not self._ssid:
            raise OSError('No initial WiFi connection.')
        s = self._sta_if
        if s.isconnected():
            return
        if ESP32:  # Maybe none of this is needed now?
            s.disconnect()
            # utime.sleep_ms(20)  # Hopefully no longer required
            await asyncio.sleep(1)

        while True:  # For the duration of an outage
            s.connect(self._ssid, self._pw)
            if await self._got_wifi(s):
                break

    async def bad_server(self):
        await asyncio.sleep(0)
        raise OSError('No initial server connection.')

    # **** API end ****

    def _close(self):
        self._verbose and print('Closing sockets.')
        if isinstance(self._sock, socket.socket):
            self._sock.close()

    # Await a WiFi connection for 10 secs.
    async def _got_wifi(self, s):
        for _ in range(20):  # Wait t s for connect. If it fails assume an outage
            #if ESP32:  # Hopefully no longer needed
                #utime.sleep_ms(20)
            await asyncio.sleep_ms(500)
            self._feed(0)
            if s.isconnected():
                return True
        return False

    async def _write(self, line):
        tshort = self._tim_short
        while True:
            # After an outage wait until something is received from server
            # before we send.
            while not self._ok:
                await asyncio.sleep_ms(tshort)
            async with self._s_lock:
                if await self._send(line):
                    return

            self._evfail.set('writer fail')
            # Wait for a response to _evfail
            while self._ok:
                await asyncio.sleep_ms(tshort)

    # Handle qos. Retransmit until matching ACK received.
    # ACKs typically take 200-400ms to arrive.
    async def _do_qos(self, mid, line):
        nms100 = 10  # Hundreds of ms to wait for ACK 
        while True:
            while not self._ok:  # Wait for any outage to clear
                await asyncio.sleep_ms(self._tim_short)
            for _ in range(nms100):
                await asyncio.sleep_ms(100)  # Wait for an ACK (how long?)
                if mid not in self._acks_pend:
                    return  # ACK was received
            if self._ok:
                await self._write(line)
                self._verbose and print('Repeat', line, 'to server app')

    # Make an attempt to connect to WiFi. May not succeed.
    async def _connect(self, s):
        self._verbose and print('Connecting to WiFi')
        if platform == 'esp8266':
            s.connect()
        elif self._ssid:
            s.connect(self._ssid, self._pw)
        else:
            raise ValueError('No WiFi credentials available.')

        # Break out on success (or fail after 10s).
        await self._got_wifi(s)
        
        t = utime.ticks_ms()
        self._verbose and print('Checking WiFi stability for {}ms'.format(2 * self._to))
        # Timeout ensures stable WiFi and forces minimum outage duration
        while s.isconnected() and utime.ticks_diff(utime.ticks_ms(), t) < 2 * self._to:
            await asyncio.sleep(1)
            self._feed(0)

    async def _run(self, loop):
        # ESP8266 stores last good connection. Initially give it time to re-establish
        # that link. On fail, .bad_wifi() allows for user recovery.
        await asyncio.sleep(1)  # Didn't always start after power up
        s = self._sta_if
        if platform == 'esp8266':
            s.connect()
            for _ in range(4):
                await asyncio.sleep(1)
                if s.isconnected():
                    break
            else:
                await self.bad_wifi()
        else:
            await self.bad_wifi()
        init = True
        while True:
            while not s.isconnected():  # Try until stable for 2*.timeout
                await self._connect(s)
            self._verbose and print('WiFi OK')
            self._sock = socket.socket()
            self._evfail.clear()
            _reader = self._reader()
            try:
                serv = socket.getaddrinfo(self._server, self._port)[0][-1]  # server read
                # If server is down OSError e.args[0] = 111 ECONNREFUSED
                self._sock.connect(serv)
                self._sock.setblocking(False)
                # Start reading before server can send: can't send until it
                # gets ID.
                loop.create_task(_reader)
                # Server reads ID immediately, but a brief pause is probably wise.
                await asyncio.sleep_ms(50)
                # No need for lock yet.
                if not await self._send(self._my_id):
                    raise OSError
            except OSError:
                if init:
                    await self.bad_server()
            else:
                _keepalive = self._keepalive()
                loop.create_task(_keepalive)
                if self._concb is not None:
                    # apps might need to know connection to the server acquired
                    launch(self._concb, True, *self._concbargs)
                await self._evfail  # Pause until something goes wrong
                self._verbose and print(self._evfail.value())
                self._ok = False
                asyncio.cancel(_reader)
                asyncio.cancel(_keepalive)
                await asyncio.sleep_ms(0)  # wait for cancellation
                self._feed(0)  # _concb might block (I hope not)
                if self._concb is not None:
                    # apps might need to know if they lost connection to the server
                    launch(self._concb, False, *self._concbargs)
            finally:
                init = False
                self._close()  # Close socket but not wdt
                s.disconnect()
                self._feed(0)
                await asyncio.sleep_ms(self._to * 2)  # Ensure server detects outage
                while s.isconnected():
                    await asyncio.sleep(1)
                    self._feed(0)

    async def _reader(self):  # Entry point is after a (re) connect.
        c = self.connects  # Count successful connects
        self._evread.clear()  # No data read yet
        to = 2 * self._to  # Extend timeout on 1st pass for slow server
        try:
            while True:
                line = await self._readline(to)  # OSError on fail
                to = self._to
                mid = int(line[0:2], 16)
                if len(line) == 3:  # Got ACK: remove from expected list
                    self._acks_pend.discard(mid)  # qos0 acks are ignored
                    continue  # All done
                # Old message still pending. Discard new one peer will re-send.
                if self._evread.is_set():
                    continue
                # Message received & can be passed to user: send ack.
                self._loop.create_task(self._sendack(mid))
                # Discard dupes. mid == 0 : Server has power cycled
                if not mid:
                    isnew(-1)  # Clear down rx message record
                if isnew(mid):
                    self._evread.set(line[2:].decode())
                if c == self.connects:
                    self.connects += 1  # update connect count
        except OSError:
            self._evfail.set('reader fail')  # ._run cancels other coros

    async def _sendack(self, mid):
        async with self._s_lock:
            if await self._send('{:02x}\n'.format(mid)):
                return
        self._evfail.set('sendack fail')  # ._run cancels other coros

    async def _keepalive(self):
        while True:
            due = self._tim_ka - utime.ticks_diff(utime.ticks_ms(), self._last_wr)
            if due <= 0:
                async with self._s_lock:
                    if not await self._send(b'\n'):
                        self._evfail.set('keepalive fail')
                        return
            else:
                await asyncio.sleep_ms(due)

    # Read a line from nonblocking socket: reads can return partial data which
    # are joined into a line. Blank lines are keepalive packets which reset
    # the timeout: _readline() pauses until a complete line has been received.
    async def _readline(self, to):
        led = self._led
        line = b''
        start = utime.ticks_ms()
        while True:
            if line.endswith(b'\n'):
                self._ok = True  # Got at least 1 packet
                if len(line) > 1:
                    return line
                # Got a keepalive: discard, reset timers, toggle LED.
                self._feed(0)
                line = b''
                start = utime.ticks_ms()
                if led is not None:
                    if isinstance(led, machine.Pin):
                        led(not led())
                    else:  # On Pyboard D
                        led.toggle()
            d = self._sock.readline()
            if d == b'':
                raise OSError
            if d is None:  # Nothing received: wait on server
                await asyncio.sleep_ms(0)
            elif line == b'':
                line = d
            else:
                line = b''.join((line, d))
            if utime.ticks_diff(utime.ticks_ms(), start) > to:
                raise OSError

    async def _send(self, d):  # Write a line to socket.
        start = utime.ticks_ms()
        while d:
            try:
                ns = self._sock.send(d)  # OSError if client closes socket
            except OSError as e:
                err = e.args[0]
                if err == errno.EAGAIN:  # Would block: await server read
                    await asyncio.sleep_ms(100)
                else:
                    return False  # peer disconnect
            else:
                d = d[ns:]
                if d:  # Partial write: pause
                    await asyncio.sleep_ms(20)
                if utime.ticks_diff(utime.ticks_ms(), start) > self._to:
                    return False

        #if platform == 'pyboard':
        await asyncio.sleep_ms(200)  # Reduce message loss (why ???)
        self._last_wr = utime.ticks_ms()
        return True
