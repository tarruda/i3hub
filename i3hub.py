#!/usr/bin/env python3

import argparse
import asyncio
import collections
import json
import importlib
import inspect
import os
import pkgutil
import re
import signal
import shlex
import struct
import subprocess
import sys


from xdg.BaseDirectory import (
        xdg_config_dirs as XDG_CONFIG_DIRS,
        get_runtime_dir as xdg_runtime_dir)


JSON_SEPS = (',', ':')
STOP_SIGNAL = signal.SIGRTMAX
CONT_SIGNAL = signal.SIGRTMAX - 1


MESSAGES = (
    ('command', lambda cmd_str: cmd_str),
    ('get_workspaces',),
    ('subscribe', lambda events: json.dumps(events, separators=JSON_SEPS)),
    ('get_outputs',),
    ('get_tree',),
    ('get_marks',),
    ('get_bar_config', lambda bar_config_name: bar_config_name or ''),
    ('get_version',),
    ('get_binding_modes',),
    ('get_config',),
    ('send_tick', lambda payload: payload or ''),
)


I3_EVENTS = (
    'workspace',
    'output',
    'mode',
    'window',
    'barconfig_update',
    'binding',
    'shutdown',
    'tick',
)

HUB_EVENTS = (
    'init',
    'status_click',
    'status_update',
    'status_stop',
    'status_cont',
)


class ParseSignal(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if namespace.status_command is None:
            return parser.error(
                    '{} should only be specified with --status-command'.format(
                        option_string))

        match = re.match('^(SIG[A-Z]+)(?:\s*([\+\-])\s*(\d+)\s*)?$', value)
        errmsg = '"{}" is not a valid signal'.format(value)
        if not match:
            return parser.error(errmsg)
        groups = match.groups()
        if not hasattr(signal, groups[0]):
            return parser.error(errmsg)
        sigval = getattr(signal, groups[0])
        if groups[1]:
            if groups[1] == '+':
                sigval += int(groups[2])
            else:
                sigval -= int(groups[2])
        setattr(namespace, option_string[2:].replace('-', '_'), sigval)


class I3ConnectionMeta(type):
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


class I3Connection(object, metaclass=I3ConnectionMeta):
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
        header_data = await self._reader.readexactly(self.HEADER_SIZE)
        if len(header_data) == 0:
            self._eof = True
            return 'eof', None, None
        magic, msg_length, msg_type = struct.unpack(self.HEADER_FORMAT,
                header_data)
        is_event = (msg_type >> 31) == 1
        msg_type = msg_type & 0x7f
        payload = json.loads(
                (await self._reader.readexactly(msg_length)).decode(
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
        else:
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
                attrs[name] = gen_method(getattr(I3Connection, name))

        return type.__new__(cls, clsname, superclasses, attrs)


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
    def __init__(self, loop, conn, i3bar_reader, i3bar_writer,
            plugins, status_command='i3status',
            status_command_stop_signal=None, status_command_cont_signal=None):
        self._loop = loop
        self._conn = conn
        self._i3bar_reader = i3bar_reader
        self._i3bar_writer = i3bar_writer
        self._plugins = plugins
        self._status_command = status_command
        self._status_command_stop_sig = (status_command_stop_signal or
                signal.SIGSTOP)
        self._status_command_cont_sig = (status_command_cont_signal or
                signal.SIGCONT)
        self._status_proc = None
        self._current_status_data = None
        self._i3api = None
        self._event_handlers = {}
        self._closed = False

    @property
    def _run_as_status(self):
        return self._i3bar_writer is not None

    def _add_event_handler(self, event, handler):
        print('subscribing {handler} ({module}) to event "{event}"'.format(
            handler=handler,
            module=sys.modules[handler.__module__].__file__,
            event=event))
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
        def is_event_handler(obj):
            return hasattr(obj, '_i3hub_listen_to')

        subscribed_i3_events = set()
        for plugin in self._plugins:
            for _, handler in inspect.getmembers(plugin, is_event_handler):
                for event in handler._i3hub_listen_to:
                    event.split('::', maxsplit=1)
                    ns, ev = event.split('::', maxsplit=1)
                    if ns == 'i3':
                        subscribed_i3_events.add(ev)
                    self._add_event_handler(event, handler)
        # subscribe the connection to all i3 events listened by plugins
        await self._conn.subscribe(list(subscribed_i3_events))

    async def _dispatch_event(self, event, payload, serially=False):
        if serially:
            for handler in self._event_handlers.get(event, []):
                await handler(self._i3api, payload)
        else:
            for handler in self._event_handlers.get(event, []):
                 self._loop.create_task(handler(self._i3api, payload))

    async def _dispatch_stop(self):
        if self._status_proc:
            self._status_proc.send_signal(self._status_command_stop_sig)
        return await self._dispatch_event('i3hub::status_stop', None) 

    async def _dispatch_cont(self):
        if self._status_proc:
            self._status_proc.send_signal(self._status_command_cont_sig)
        return await self._dispatch_event('i3hub::status_cont', None) 

    async def _dispatch_update(self, line, first):
        # unlike most events, status updates are processed serially to allow
        # deterministic ordered processing by plugins
        self._current_status_data = json.loads(line)
        await self._dispatch_event('i3hub::status_update',
                self._current_status_data, serially=True)
        self._output_updated_status(first)

    def _output_updated_status(self, first):
        if not first:
            self._i3bar_writer.write(','.encode('utf-8'))
        self._i3bar_writer.write(
                json.dumps(self._current_status_data,
                    separators=JSON_SEPS).encode('utf-8'))
        self._i3bar_writer.write('\n'.encode('utf-8'))

    async def _dispatch_shutdown(self, payload):
        # this should be called either when i3 shuts down or when i3hub is
        # killed (connection closed by stop(), which results in "eof event")
        assert not self._i3api._shutting_down
        self._i3api._shutting_down = True
        handlers = self._event_handlers.get('i3::shutdown', [])
        for handler in handlers:
            await handler(self._i3api, payload)

    async def _read_status(self, first=False):
        line = await self._status_proc.stdout.readline()
        if not line:
            return False
        if not first:
            line = line[1:]
        await self._dispatch_update(line.decode('utf-8', 'replace'), first)
        return True

    async def _read_click_events(self):
        # read opening bracket
        await self._i3bar_reader.readline()
        while not self._i3bar_reader.at_eof():
            line = (await self._i3bar_reader.readline())
            if not line:
                print('reached EOF while reading stdin')
                break
            if line[0] == b',':
                # remove leading comma
                line = line[1:]
            try:
                click_event_payload = json.loads(line.decode('utf-8',
                    'replace'))
            except json.JSONDecodeError:
                print('failed to parse click event: {}'.format(line))
                continue
            await self._dispatch_event('i3hub::status_click',
                    click_event_payload)

    async def _run_status(self):
        print('started running as status command')
        click_events = self._i3bar_reader is not None
        if click_events:
            self._loop.create_task(self._read_click_events())
        self._i3bar_writer.write(json.dumps({
            'version': 1,
            'stop_signal': STOP_SIGNAL,
            'cont_signal': CONT_SIGNAL,
            'click_events': click_events
        }, separators=JSON_SEPS).encode('utf-8'))
        self._i3bar_writer.write('\n[\n'.encode('utf-8'))
        if not self._status_command:
            await self._dispatch_update('[]\n', True)
            return
        # i3 will send stop/cont signals to the process group. use preexec_fn
        # to ensure the child status process ignores our own STOP/CONT signal
        # numbers. This must also be done by plugins spawn children.
        def ignore_sigs():
            signal.signal(STOP_SIGNAL, signal.SIG_IGN)
            signal.signal(CONT_SIGNAL, signal.SIG_IGN)
        self._status_proc = await asyncio.create_subprocess_exec(
                *self._status_command, stdout=asyncio.subprocess.PIPE,
                preexec_fn=ignore_sigs)
        # ignore first line with version information
        await self._status_proc.stdout.readline()
        # ignore second line line with opening bracket
        await self._status_proc.stdout.readline()
        # dispatch initial status state
        status_read = await self._read_status(first=True)
        while status_read:
            status_read = await self._read_status()
        await self._status_proc.wait()

    async def _dispatch_events(self):
        print('started dispatching events')
        while True:
            event, payload = await self._conn.wait_event()
            if event in ('shutdown', 'eof',):
                await self._dispatch_shutdown(payload)
                break
            if event is not None:
                await self._dispatch_event('i3::' + event, payload)

    async def run(self):
        print('starting')
        self._setup_signals()
        self._i3api = I3ApiWrapper(self._conn,
                get_status_cb=lambda: self._current_status_data,
                update_status_cb=lambda: self._output_updated_status(False))
        await self._setup_events()
        tasks = [self._dispatch_events()]
        # FIXME: only invoke 'init' event after the status has sent the opening
        # bracket. This will prevent races where a plugin updates the status
        # before the header has been sent
        if self._run_as_status:
            tasks.append(self._run_status())
        await self._dispatch_event('i3hub::init', None)
        await asyncio.gather(*tasks)
        print('stopped')

    def stop(self):
        if self._closed:
            return
        print('stopping')
        if self._status_proc:
            self._status_proc.terminate()
        if self._i3bar_reader:
            self._i3bar_reader.feed_eof()
        self._conn.close()
        self._closed = True


def listen(event):
    split = event.split('::', maxsplit=1)
    if len(split) != 2:
        raise Exception('"{}" is not a valid event name'.format(event))
    ns, ev = split
    if ns not in ['i3', 'i3hub', 'plugin']:
        raise Exception('Invalid event namespace "{}"'.format(ns))
    if ns == 'i3' and ev not in I3_EVENTS:
        raise Exception('Invalid i3 event "{}"'.format(ev))
    if ns == 'i3hub' and ev not in HUB_EVENTS:
        raise Exception('Invalid i3hub event "{}"'.format(ev))
    def dec(fn):
        if not inspect.iscoroutinefunction(fn):
            raise Exception('Only coroutine functions can be event listeners')
        if not hasattr(fn, '_i3hub_listen_to'):
            fn._i3hub_listen_to = []
        fn._i3hub_listen_to.append(event)
        return fn
    return dec


async def connect(socket_path=None, loop=None):
    if not socket_path:
        socket_path = get_socket_path()
    if not loop:
        loop = asyncio.get_event_loop()
    reader, writer = await asyncio.open_unix_connection(socket_path, loop=loop)
    return I3Connection(loop, reader, writer)


def get_socket_path():
    return subprocess.check_output(['i3', '--get-socketpath']).decode().strip()


def discover_plugins(config_dirs):
    loaded = set()
    for config_dir in config_dirs:
        plugins_dir = '{}/plugins'.format(config_dir)
        if not os.path.exists('{}/__init__.py'.format(plugins_dir)):
            continue
        sys.path.insert(0, config_dir)
        importlib.import_module('plugins')
        for _, name, _ in pkgutil.iter_modules(path=[plugins_dir]):
            print('loading {} from {}'.format(name, plugins_dir)) 
            name = '.{}'.format(name)
            if name not in loaded:
                loaded.add(name)
                yield importlib.import_module(name, package='plugins')


async def setup_i3bar_streams(loop):
    # dup stdout fd and use to write i3bar updates, since it is possible that
    # stdout original fd will be closed for logging
    new_stdout = os.fdopen(os.dup(sys.stdout.fileno()), 'w', 1)
    # create StreamReader for stdin
    stdin = asyncio.StreamReader(loop=loop)
    stdin_proto = asyncio.StreamReaderProtocol(stdin)
    await loop.connect_read_pipe(lambda: stdin_proto, sys.stdin)
    # create StreamWriter for the dupped stdout fd
    stdout_trans, stdout_proto = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, new_stdout)
    stdout = asyncio.streams.StreamWriter(stdout_trans, stdout_proto, None,
            loop)
    return stdin, stdout


def setup_logging(loop, log_file):
    log = open(log_file, 'w', 1)
    # We dup the log file fd to fds 1 and 2. This will redirect
    # stdout/stderr output from both i3hub plugins and child processes
    # to the log file
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    sys.stdout = log
    sys.stderr = log


async def i3hub_main(loop, args):
    if args.run_as_status:
        # this must be done before redirecting stdout for logging
        i3bar_reader, i3bar_writer = await setup_i3bar_streams(loop)
    else:
        i3bar_reader, i3bar_writer = None, None
    if args.log_file or args.run_as_status:
        if not args.log_file:
            # even if a log file is not specified, always use one when running
            # as i3bar status since stdout is already used for writing status
            # updates.
            args.log_file = '{}/i3hub.log'.format(xdg_runtime_dir())
        setup_logging(loop, args.log_file)
    # load plugins
    plugins = list(discover_plugins(args.config_dirs.split(':')))
    # connect to i3
    conn = await connect(loop=loop)
    status_command = (
                shlex.split(args.status_command) if args.status_command
                else None)
    hub = I3Hub(loop, conn, i3bar_reader, i3bar_writer, plugins,
            status_command, args.status_command_stop_signal,
            args.status_command_cont_signal)
    await hub.run()


def parse_args():
    parser = argparse.ArgumentParser('i3hub')
    parser.add_argument('--config-dirs', default=':'.join(
        '{}/i3hub'.format(d) for d in XDG_CONFIG_DIRS))
    parser.add_argument('--run-as-status', default=False, action='store_true')
    parser.add_argument('--status-command', default=None)
    parser.add_argument('--status-command-stop-signal', default=None,
            action=ParseSignal)
    parser.add_argument('--status-command-cont-signal', default=None,
            action=ParseSignal)
    parser.add_argument('--log-file', default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.status_command:
        # imply --run-as-status
        args.run_as_status = True
    loop = asyncio.get_event_loop()
    loop.run_until_complete(i3hub_main(loop, args))
    loop.close()


if __name__ == '__main__':
    main()
