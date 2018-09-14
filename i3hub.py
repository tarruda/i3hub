#!/usr/bin/env python3

import argparse
import asyncio
import collections
import configparser
import json
import importlib.util
import inspect
import os
import pkgutil
import signal
import struct
import subprocess
import sys


from xdg.BaseDirectory import (
        xdg_config_home,
        load_data_paths,
        load_config_paths,
        get_runtime_dir)


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
        try:
            header_data = await self._reader.readexactly(self.HEADER_SIZE)
        except asyncio.IncompleteReadError:
            self._eof = True
            return 'eof', None, None
        magic, msg_length, msg_type = struct.unpack(self.HEADER_FORMAT,
                header_data)
        is_event = msg_type & 0x80000000
        msg_type &= 0x7fffffff
        try:
            body_data = await self._reader.readexactly(msg_length)
        except asyncio.IncompleteReadError:
            self._eof = True
            return 'eof', None, None
        payload = json.loads(body_data.decode('utf-8', 'replace'))
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
    def __init__(self, conn, get_status_cb, update_status_cb, emit_event_cb):
        self._conn = conn
        self._shutting_down = False
        self._get_status_cb = get_status_cb
        self._update_status_cb = update_status_cb
        self._emit_event_cb = emit_event_cb

    @property
    def event_loop(self):
        return self._conn._loop

    def get_status(self):
        return self._get_status_cb()

    def update_status(self):
        return self._update_status_cb()

    async def emit_event(self, event, arg):
        await self._emit_event_cb('plugin::' + event, arg)


class I3Hub(object):
    def __init__(self, loop, conn, i3bar_reader, i3bar_writer,
            plugins, config, status_output_sort_keys=False):
        self._loop = loop
        self._conn = conn
        self._i3bar_reader = i3bar_reader
        self._i3bar_writer = i3bar_writer
        self._plugins = plugins
        self._config = config
        self._status_output_sort_keys = status_output_sort_keys
        self._current_status_data = []
        self._first_status_update = True
        self._i3api = None
        self._event_handlers = {}
        self._closed = False

    @property
    def run_as_status(self):
        return self._i3bar_writer is not None

    def _add_event_handler(self, event, handler):
        # print(handler.__module__)
        print('subscribing {handler} ({module}) to event "{event}"'.format(
            handler=handler,
            module=sys.modules[handler.__module__].__file__,
            event=event))
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def _setup_events(self):
        def is_class_plugin(obj):
            return getattr(obj, '_i3hub_class_plugin', False)

        def is_event_handler(obj):
            return callable(obj) and hasattr(obj, '_i3hub_listen_to')

        def discover_event_handlers(plugin, subscribed_i3_events):
            module_plugin = plugin.__class__.__name__ == 'module'
            for _, handler in inspect.getmembers(plugin, is_event_handler):
                for event in handler._i3hub_listen_to:
                    event.split('::', maxsplit=1)
                    ns, ev = event.split('::', maxsplit=1)
                    if ns == 'i3':
                        subscribed_i3_events.add(ev)
                    self._add_event_handler(event, handler)
            if not module_plugin:
                # no searching for class plugin inside class plugins
                return
            for _, cls in inspect.getmembers(plugin, is_class_plugin):
                plugin_instance = cls(self._i3api)
                discover_event_handlers(plugin_instance, subscribed_i3_events)

        subscribed_i3_events = set()
        for plugin in self._plugins:
            discover_event_handlers(plugin, subscribed_i3_events)
        # subscribe the connection to all i3 events listened by plugins
        await self._conn.subscribe(list(subscribed_i3_events))

    async def _dispatch_event(self, event, arg):
        for handler in self._event_handlers.get(event, []):
            if inspect.ismethod(handler) and hasattr(handler.__self__,
                    '_i3hub_class_plugin'):
                await handler(event, arg)
            else:
                await handler(self._i3api, event, arg)

    async def dispatch_stop(self):
        return await self._dispatch_event('i3hub::status_stop', None) 

    async def dispatch_cont(self):
        return await self._dispatch_event('i3hub::status_cont', None) 

    def _output_updated_status(self):
        if self._first_status_update:
            self._first_status_update = False
        else:
            self._i3bar_writer.write(','.encode('utf-8'))
        self._i3bar_writer.write(
                json.dumps(self._current_status_data,
                    separators=JSON_SEPS,
                    sort_keys=self._status_output_sort_keys).encode('utf-8'))
        self._i3bar_writer.write('\n'.encode('utf-8'))

    async def _dispatch_shutdown(self, arg):
        # this should be called either when i3 shuts down or when i3hub is
        # killed (connection closed by close(), which results in "eof event")
        if not self._i3api._shutting_down:
            self._i3api._shutting_down = True
            await self._dispatch_event('i3::shutdown', arg)

    async def _read_click_events(self):
        # read opening bracket
        await self._i3bar_reader.readline()
        is_first = True
        while not self._i3bar_reader.at_eof():
            line = await self._i3bar_reader.readline()
            if not line:
                print('reached EOF while reading stdin')
                break
            if is_first:
                is_first = False
            else:
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

    async def _run_status(self, status_ready):
        print('started running as status command')
        click_events = self._i3bar_reader is not None
        if click_events:
            self._loop.create_task(self._read_click_events())
        self._i3bar_writer.write(json.dumps({
            'version': 1,
            'stop_signal': STOP_SIGNAL,
            'cont_signal': CONT_SIGNAL,
            'click_events': click_events
        }, separators=JSON_SEPS,
        sort_keys=self._status_output_sort_keys).encode('utf-8'))
        self._i3bar_writer.write('\n[\n'.encode('utf-8'))
        status_ready.set_result(None)

    async def _dispatch_i3_events(self):
        print('started dispatching i3 events')
        while True:
            event, payload = await self._conn.wait_event()
            if event in ('shutdown', 'eof',):
                await self._dispatch_shutdown(payload or 'eof')
                self.close()
                break
            if event is not None:
                self._loop.create_task(
                        self._dispatch_event('i3::' + event, payload))

    async def run(self):
        if self._closed:
            raise Exception('This I3Hub instance was already closed')
        print('starting')
        self._i3api = I3ApiWrapper(self._conn,
                get_status_cb=lambda: self._current_status_data,
                update_status_cb=lambda: self._output_updated_status(),
                emit_event_cb=self._dispatch_event)
        await self._setup_events()
        futures = []
        if self.run_as_status:
            # use a future to get notified when the _run_status task has
            # written the opening bracket. This ensures plugins can't send a
            # status update in the init event before the opening bracket is
            # sent, which could result in a parse failure by i3bar
            status_ready = asyncio.Future(loop=self._loop)
            futures.append(asyncio.ensure_future(self._run_status(
                status_ready)))
            await status_ready
        # dispatch the init event before reading events from i3
        await self._dispatch_event('i3hub::init', None)
        # start reading events from i3
        futures.append(asyncio.ensure_future(self._dispatch_i3_events()))
        await asyncio.gather(*futures)
        await self._dispatch_shutdown('close')
        print('stopped')

    def close(self):
        if self._closed:
            return
        print('stopping')
        if self._i3bar_reader:
            self._i3bar_reader.feed_eof()
        self._conn.close()
        self._closed = True


def plugin(cls):
    if not (inspect.isclass(cls) and cls.__name__ != 'module'):
        raise Exception('The @plugin decorator is for classes only')
    cls._i3hub_class_plugin = True
    return cls


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


def load_plugins(paths, plugins):
    candidates = []
    for plugin in plugins:
        l = len(candidates)
        if '/' in plugin:
            if os.path.exists(plugin):
                candidates.append(plugin)
        else:
            for path in paths:
                for ext in ['.py', '']:
                    p = os.path.join(path, plugin) + ext
                    if os.path.exists(p):
                        candidates.append(p)
        if len(candidates) == l:
            print('warning: plugin "{}" was not found'.format(plugin))
    for candidate in candidates:
        is_module = candidate.endswith('.py')
        spec_name = 'i3hub.plugins.{}'.format(os.path.basename(candidate))
        if is_module:
            spec_name = spec_name[:-3]
            module_path = candidate
        else:
            module_path = os.path.join(candidate, '__init__.py')
        if spec_name in sys.modules:
            print('"{}" already loaded, skipping "{}"'.format(spec_name,
                candidate))
            continue
        print('loading "{}" from "{}"'.format(spec_name, candidate))
        spec = importlib.util.spec_from_file_location(spec_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[spec_name] = module
        yield module


def load_config(config_path):
    config = configparser.ConfigParser(
            interpolation=configparser.ExtendedInterpolation())
    if os.path.exists(config_path):
        config.read(config_path)
    if 'i3hub' not in config:
        config['i3hub'] = {}
    if 'DEFAULT' not in config:
        config['DEFAULT'] = {}
    return config


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


def setup_signals(loop, hub):
    sig_handler = lambda: hub.close()
    loop.add_signal_handler(signal.SIGINT, sig_handler)
    loop.add_signal_handler(signal.SIGTERM, sig_handler)
    if hub.run_as_status:
        loop.add_signal_handler(STOP_SIGNAL,
                lambda: loop.create_task(hub.dispatch_stop()))
        loop.add_signal_handler(CONT_SIGNAL,
                lambda: loop.create_task(hub.dispatch_cont()))


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
            args.log_file = '{}/i3hub.log'.format(get_runtime_dir())
        setup_logging(loop, args.log_file)
    # load config
    config = load_config(args.config)
    try:
        config_load = json.loads(config['i3hub'].get('plugins', '[]'))
    except JSONDecodeError:
        config_load = []
    # load plugins
    plugins = list(load_plugins(args.plugin_path.split(':'), args.load +
        config_load))
    # connect to i3
    conn = await connect(loop=loop)
    hub = I3Hub(loop, conn, i3bar_reader, i3bar_writer, plugins, config)
    setup_signals(loop, hub)
    await hub.run()


def parse_args():
    parser = argparse.ArgumentParser('i3hub')
    parser.add_argument('--load', action='append', default=[])
    default_data_dirs = list(
            list(load_config_paths('i3hub')) + list(load_data_paths('i3hub')))
    default_plugin_path = ':'.join('{}/plugins'.format(p) for p in
            default_data_dirs)
    parser.add_argument('--plugin-path', default=default_plugin_path)
    config_candidates = list(c for c in
            (os.path.join(p, 'i3hub.cfg') for p in default_data_dirs)
            if os.path.exists(c))
    if not config_candidates:
        config_candidates.append('{}/i3hub/i3hub.cfg'.format(xdg_config_home))
    parser.add_argument('-c', '--config', default=config_candidates[0])
    parser.add_argument('--run-as-status', default=False, action='store_true')
    parser.add_argument('--log-file', default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(i3hub_main(loop, args))
    loop.close()


if __name__ == '__main__':
    main()
