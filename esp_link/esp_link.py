# esp_link.py Run on ESP8266. Provides a link between Pyboard/STM device and
# IOT server.

# Copyright (c) Peter Hinch 2018
# Released under the MIT licence. Full text in root of this repository.

import gc
import uasyncio as asyncio
import network
gc.collect()
import ujson
from micropython_iot import client
#import client
from machine import Pin, I2C
import ujson
from . import asi2c


class App:
    def __init__(self, loop, verbose):
        self.verbose = verbose
        self.cl = None  # Client instance for server comms.
        self.timeout = 0  # Set by config
        self.qos = 0
        # Instantiate a Pyboard Channel
        i2c = I2C(scl=Pin(0),sda=Pin(2))  # software I2C
        syn = Pin(5)
        ack = Pin(4)
        self.chan = asi2c.Responder(i2c, syn, ack)  # Channel to Pyboard
        self.sreader = asyncio.StreamReader(self.chan)
        self.swriter = asyncio.StreamWriter(self.chan, {})
        loop.create_task(self.start(loop))

    async def start(self, loop):
        await self.chan.ready()  # Wait for sync
        # Override settings in local.py with those sent from Pyboard
        self.verbose and print('awaiting config')
        while True:
            line = await self.sreader.readline()
            # After a crash can contain last message
            try:
                config = ujson.loads(line)
            except ValueError:
                self.verbose and print('JSON error. Got:', line)
            else:
                if isinstance(config, list) and len(config) == 9 and config[-1] == 'cfg':
                    break  # Got good config
                else:
                    self.verbose and print('Got bad config', line)

        self.timeout = config[3]
        self.qos = config[6]
        # Handle case where ESP8266 has not been initialised to the WLAN
        sta_if = network.WLAN(network.STA_IF)
        ap = network.WLAN(network.AP_IF) # access-point interface.
        ap.active(False)         # deactivate AP interface.
        if sta_if.isconnected():
            self.verbose and print('Connected to WiFi.')
        else:
            # Either ESP does not 'know' this WLAN or it needs time to connect.
            if config[5] == '':  # No SSID supplied: can only keep trying
                verbose and print('Connecting to ESP8266 stored network...')
                net = 'stored network'
            else:
                # Try to connect to specified WLAN. ESP will save details for
                # subsequent connections.
                net = config[5]
                self.verbose and print('Connecting to specified network...')
                sta_if.active(True)
                sta_if.connect(config[5], config[6])
            self.verbose and print('Awaiting WiFi.')
            count = 0
            while not sta_if.isconnected():
                await asyncio.sleep(1)
                count += 1
                if count > 20:
                    err = "Can't connect to {}".format(net)
                    data = ['error', err]
                    line = ''.join((ujson.dumps(data), '\n'))
                    await self.swriter.awrite(line)
                    # Message to Pyboard and REPL. Crash the board. Pyboard
                    # detects, can reboot and retry, change config, or whatever
                    raise ValueError(err)  # croak...

        self.verbose and print('Setting client config', config)
        self.cl = client.Client(loop, config[0], config[2], config[1], config[3],
                                verbose=self.verbose)
        self.verbose and print('App awaiting connection.')
        await self.cl
        loop.create_task(self.to_server())
        loop.create_task(self.from_server())
        loop.create_task(self.server_status())
        if config[4]:
            loop.create_task(self.report(config[4]))

    async def to_server(self):
        self.verbose and print('Started to_server task.')
        while True:
            line = await self.sreader.readline()
            # If the following pauses fo an outage, the Pyboard may write
            # one more line. Subsequent calls to channel.write pause pending
            # resumption of communication with the server.
            await self.cl.write(line)
            # https://github.com/peterhinch/micropython-iot/blob/master/qos/README.md
            if self.qos:  # qos 0 or 1 supported
                await asyncio.sleep_ms(self.timeout)
                if not self.cl.status():
                    await self.cl.write(line)
            self.verbose and print('Sent', line, 'to server app')

    async def from_server(self):
        self.verbose and print('Started from_server task.')
        while True:
            line = await self.cl.readline()
            await self.swriter.awrite(line.decode('utf8'))
            self.verbose and print('Sent', line, 'to Pyboard app\n')

    async def server_status(self):  # TODO Kevin: use callback?
        oldstatus = True
        while True:
            await asyncio.sleep_ms(500)
            status = self.cl.status()
            if status != oldstatus:
                oldstatus = status
                data = ['status', status]
                line = ''.join((ujson.dumps(data), '\n'))
                await self.swriter.awrite(line)

    async def report(self, time):
        data = ['report', 0, 0, 0]
        count = 0
        while True:
            await asyncio.sleep(time)
            data[1] = self.cl.connects  # For diagnostics
            data[2] = count
            count += 1
            gc.collect()
            data[3] = gc.mem_free()
            line = ''.join((ujson.dumps(data), '\n'))
            await self.swriter.awrite(line)

    def close(self):
        self.verbose and print('Closing interfaces')
        if self.cl is not None:
            self.cl.close()
        self.chan.close()

loop = asyncio.get_event_loop()
app = App(loop, True)
try:
    loop.run_forever()
finally:
    app.close()  # e.g. ctrl-c at REPL
