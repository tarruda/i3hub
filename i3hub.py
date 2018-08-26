#!/usr/bin/env python3

import argparse
import asyncio
import collections
import json
import importlib
import os
import pkgutil
import signal
import shlex
import struct
import subprocess
import sys


from xdg.BaseDirectory import xdg_config_dirs as XDG_CONFIG_DIRS


STOP_SIGNAL = signal.SIGRTMAX
CONT_SIGNAL = signal.SIGRTMAX - 1


MESSAGES = (
    ('command', lambda cmd_str: cmd_str),
    ('get_workspaces',),
    ('subscribe', lambda events: json.dumps(events)),
    ('get_outputs',),
    ('get_tree',),
    ('get_marks',),
    ('get_bar_config', lambda bar_config_name='': bar_config_name),
    ('get_version',),
    ('get_binding_modes',),
    ('get_config',),
    ('send_tick', lambda payload: payload),
)


I3_EVENTS = (
    'workspace',
    'output',
    'mode',
    'window',
    'barconfig_update',
    'binding',
    'shutdown',
)

HUB_EVENTS = (
    'init',
    'status_update',
    'status_stop',
    'status_cont',
)


class I3AsyncConnectionMeta(type):
    def __new__(cls, clsname, superclasses, attrs):
        def gen_method(msg_type, handler):
            if len(handler) == 1:
                async def async_method(self):
                    return await self._send(msg_type)
            else:
                async def async_method(self, arg=None):
                    return await self._send(msg_type, handler[1](arg))
            return async_method

        for msg_type, handler in enumerate(MESSAGES):
            attrs[handler[0]] = gen_method(msg_type, handler)

        return type.__new__(cls, clsname, superclasses, attrs)


class I3ConnectionMeta(type):
    def __new__(cls, clsname, superclasses, attrs):
        def gen_method(async_method):
            def method(self, *args, **kwargs):
                return self._conn._loop.run_until_complete(
                        async_method(self._conn, *args, **kwargs))
            return method

        for message in MESSAGES:
            name = message[0]
            attrs[name] = gen_method(getattr(I3AsyncioConnection, name))

        return type.__new__(cls, clsname, superclasses, attrs)


class I3ApiWrapperMeta(type):
    def __new__(cls, clsname, superclasses, attrs):
        def gen_method(async_method):
            def wrapper(self, *args, **kwargs):
                if self._shutting_down:
                    raise Exception('Cannot send messages when shutting down')
                return async_method(self._conn, *args, **kwargs)
            return wrapper

        for message in MESSAGES:
            name = message[0]
            if name != 'subscribe':
                attrs[name] = gen_method(getattr(I3AsyncioConnection, name))

        return type.__new__(cls, clsname, superclasses, attrs)


class I3AsyncioConnection(object, metaclass=I3AsyncConnectionMeta):
    MAGIC = b'i3-ipc'
    HEADER_FORMAT = '={}sII'.format(len(MAGIC))
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

    def __init__(self, loop, reader, writer):
        self._loop = loop
        self._reader = reader
        self._writer = writer
        self._reply = None
        self._send_queue = collections.deque()
        self._event_queue = collections.deque()
        self._eof = False
        self._polling = False

    def _send_now(self, message_type, payload):
        body = payload.encode('utf-8')
        header = self.MAGIC + struct.pack('=II', len(body), message_type)
        self._writer.write(header + body)

    async def _send(self, message_type, payload=''):
        while self._reply:
            # wait our turn to send the message
            future = asyncio.Future(loop=self._loop)
            self._send_queue.append(future)
            await future
        self._send_now(message_type, payload)
        self._reply = asyncio.Future(loop=self._loop)
        if not self._polling:
            self._loop.create_task(self._wait_reply())
        payload = await self._reply
        self._reply = None
        if self._send_queue:
            # allow the next message to be sent
            self._send_queue.popleft().set_result(None)
        return payload

    async def _recv(self):
        assert not self._eof
        header_data = await self._reader.read(self.HEADER_SIZE)
        if len(header_data) == 0:
            self._eof = True
            return 'eof', None, None
        magic, msg_length, msg_type = struct.unpack(self.HEADER_FORMAT,
                header_data)
        is_event = (msg_type >> 31) == 1
        msg_type = msg_type & 0x7f
        payload = json.loads((await self._reader.read(msg_length)).decode(
            'utf-8', 'replace'))
        return msg_type, is_event, payload

    async def _poll(self):
        self._polling = True
        msg_type, is_event, payload = await self._recv()
        self._polling = False
        if msg_type == 'eof':
            self._event_queue.append(('eof', None))
        elif is_event:
            self._event_queue.append((I3_EVENTS[msg_type], payload))
        elif payload:
            assert self._reply
            self._reply.set_result(payload)

    async def _wait_reply(self):
        while not self._reply.done():
            await self._poll()

    async def wait_event(self):
        while not self._event_queue:
            await self._poll()
        return self._event_queue.popleft()

    def close(self):
        self._writer.close()


class I3Connection(object, metaclass=I3ConnectionMeta):
    def __init__(self, conn):
        self._conn = conn


class I3ApiWrapper(object, metaclass=I3ApiWrapperMeta):
    def __init__(self, conn, get_status_cb, update_status_cb):
        self._conn = conn
        self._shutting_down = False
        self._get_status_cb = get_status_cb
        self._update_status_cb = update_status_cb

    def get_status(self):
        return self._get_status_cb()

    def update_status(self):
        return self._update_status_cb()


class I3Hub(object):
    def __init__(self, config_dirs, socket_path=None, run_as_status=False,
            status_command='i3status'):
        self._socket_path = socket_path
        self._run_as_status = run_as_status
        self._status_command = status_command
        self._status_stop_sig = signal.SIGSTOP
        self._status_cont_sig = signal.SIGCONT
        self._config_dirs = config_dirs.split(':')
        self._statusproc = None
        self._current_status_data = None
        self._plugins = None
        self._conn = None
        self._loop = None
        self._i3api = None
        self._event_handlers = {}
        self._closed = False

    def _add_event_handler(self, event, handler):
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    def _setup_signals(self):
        loop = self._loop
        sig_handler = lambda: self.stop()
        loop.add_signal_handler(signal.SIGINT, sig_handler)
        loop.add_signal_handler(signal.SIGTERM, sig_handler)
        if self._run_as_status:
            loop.add_signal_handler(STOP_SIGNAL,
                    lambda: loop.create_task(self._dispatch_stop()))
            loop.add_signal_handler(CONT_SIGNAL,
                    lambda: loop.create_task(self._dispatch_cont()))

    async def _setup_events(self):
        for plugin in self._plugins:
            for event in I3_EVENTS + HUB_EVENTS:
                handler = getattr(plugin, 'on_{}'.format(event), None)
                if handler:
                    self._add_event_handler(event, handler)
        i3_events = (k for k in self._event_handlers.keys() if k in I3_EVENTS)
        await self._conn.subscribe(*list(i3_events))

    async def _dispatch_event(self, event, payload, serially=False):
        if serially:
            for handler in self._event_handlers.get(event, []):
                await handler(self._i3api, payload)
        else:
            for handler in self._event_handlers.get(event, []):
                 self._loop.create_task(handler(self._i3api, payload))

    async def _dispatch_stop(self):
        if self._statusproc:
            self._statusproc.send_signal(self._status_stop_sig)
        return await self._dispatch_event('status_stop', None) 

    async def _dispatch_cont(self):
        if self._statusproc:
            self._statusproc.send_signal(self._status_cont_sig)
        return await self._dispatch_event('status_cont', None) 

    async def _dispatch_update(self, line, first):
        # unlike most events, status updates are processed serially to allow
        # deterministic ordered processing by plugins
        self._current_status_data = json.loads(line)
        await self._dispatch_event('status_update', self._current_status_data,
                serially=True)
        self._output_updated_status(first)

    def _output_updated_status(self, first):
        if not first:
            sys.stdout.write(',')
        sys.stdout.write(json.dumps(self._current_status_data))
        sys.stdout.write('\n')

    async def _dispatch_shutdown(self, payload):
        # this should be called either when i3 shuts down or when i3hub is
        # killed (connection closed by stop(), which results in "eof event")
        assert not self._i3api._shutting_down
        self._i3api._shutting_down = True
        handlers = self._event_handlers.get('shutdown', [])
        for handler in handlers:
            await handler(self._i3api, payload)

    async def _read_status(self, first=False):
        line = await self._statusproc.stdout.readline()
        if not line:
            return False
        if not first:
            line = line[1:]
        await self._dispatch_update(line.decode('utf-8', 'replace'), first)
        return True

    async def _run_status(self):
        # output initial similar to what i3status does in json mode, also
        # allow plugins to modify the initial status data
        sys.stdout.write(json.dumps({
            'version': 1,
            'stop_signal': STOP_SIGNAL,
            'cont_signal': CONT_SIGNAL,
            'click_events': True
        }))
        sys.stdout.write('\n[\n')
        if not self._status_command:
            await self._dispatch_update('[]\n', True)
            return
        self._statusproc = await asyncio.create_subprocess_exec(
                *self._status_command, stdout=asyncio.subprocess.PIPE)
        # ignore first line with version information
        await self._statusproc.stdout.readline()
        # ignore second line line with opening bracket
        await self._statusproc.stdout.readline()
        # dispatch initial status state
        status_read = await self._read_status(first=True)
        while status_read:
            status_read = await self._read_status()
        await self._statusproc.wait()

    async def _dispatch_events(self):
        while True:
            event, payload = await self._conn.wait_event()
            if event in ('shutdown', 'eof',):
                await self._dispatch_shutdown(payload)
                break
            if event is not None:
                await self._dispatch_event(event, payload)

    async def run(self):
        self._plugins = list(discover_plugins(self._config_dirs))
        self._conn = await connect(self._socket_path)
        self._loop = self._conn._loop
        self._setup_signals()
        self._i3api = I3ApiWrapper(self._conn,
                get_status_cb=lambda: self._current_status_data,
                update_status_cb=lambda: self._output_updated_status(False))
        await self._setup_events()
        await self._dispatch_event('init', None)
        tasks = [self._dispatch_events()]
        if self._run_as_status:
            tasks.append(self._run_status())
        await asyncio.gather(*tasks)

    def stop(self):
        if self._closed:
            return
        if self._statusproc:
            self._statusproc.terminate()
        self._conn.close()
        self._closed = True


async def connect(socket_path=None, loop=None):
    if not socket_path:
        socket_path = get_socket_path()
    if not loop:
        loop = asyncio.get_event_loop()
    reader, writer = await asyncio.open_unix_connection(socket_path, loop=loop)
    return I3AsyncioConnection(loop, reader, writer)


def get_socket_path():
    return subprocess.check_output(['i3', '--get-socketpath']).decode().strip()


def connect_sync(socket_path=None, loop=None):
    if not loop:
        loop = asyncio.get_event_loop()
    async_conn = loop.run_until_complete(connect(socket_path, loop))
    return I3Connection(async_conn)


def parse_args():
    parser = argparse.ArgumentParser('i3 plugin manager')
    parser.add_argument('--config-dirs', default=':'.join(
        '{}/i3hub'.format(d) for d in XDG_CONFIG_DIRS))
    parser.add_argument('--run-as-status', default=False, action='store_true')
    parser.add_argument('--status-command', default=None)
    return parser.parse_args()


def discover_plugins(config_dirs):
    loaded = set()
    for config_dir in config_dirs:
        plugins_dir = '{}/plugins'.format(config_dir)
        if not os.path.exists('{}/__init__.py'.format(plugins_dir)):
            continue
        sys.path.insert(0, config_dir)
        importlib.import_module('plugins')
        for _, name, _ in pkgutil.iter_modules(path=[plugins_dir]):
            name = '.{}'.format(name)
            if name not in loaded:
                loaded.add(name)
                yield importlib.import_module(name, package='plugins')


def main():
    args = parse_args()
    loop = asyncio.get_event_loop()
    hub = I3Hub(config_dirs=args.config_dirs,
            run_as_status=args.run_as_status,
            status_command=(
                shlex.split(args.status_command) if args.status_command
                else None))
    loop.run_until_complete(hub.run())
    loop.close()


if __name__ == '__main__':
    main()
