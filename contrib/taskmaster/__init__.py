import asyncio
import json
import os

from datetime import datetime

from i3hub import extension, listen, status_array_merge


SOCKET_ADDRESS = b'\0i3hub-taskmaster'
POMODORO_TIME = 25 * 60
REST_TIME = 5 * 60
LONG_REST_TIME = 20 * 60
ALARM_POMODORO = os.path.join(os.path.dirname(__file__), 'alarm-pomodoro.oga')
ALARM_REST = os.path.join(os.path.dirname(__file__), 'alarm-rest.oga')
POMODORO_COLOR = '#0e93cb'
REST_COLOR = '#00b722'


class Task(object):
    def __init__(self, task_dict):
        self.id = task_dict['id']
        self.description = task_dict['description']
        self.project = task_dict.get('project', None)

    def __str__(self):
        return self.description


@extension()
class Taskmaster(object):
    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._vitrc_location = '{}/vitrc'.format(self._i3.runtime_dir)
        self._command = 'vit'
        self._font = None
        self._window_title = None
        self._socket_address = SOCKET_ADDRESS
        self._pomodoro_time = None
        self._rest_time = None
        self._long_rest_time = None
        self._alarm_pomodoro = None
        self._alarm_rest = None
        self._pomodoro_color = None
        self._rest_color = None
        self._server = None
        self._status = 'stopped'
        self._start_time = None
        self._stop_future = None
        self._task = None
        self._pomodoro_session_stop = True
        self._pomodoro_count = 0
        self._pomodoro_loop_task = None

    def _argv(self):
        argv = ['VITRC={} urxvt -title {}'.format(self._vitrc_location,
            self._window_title)]
        if self._font:
            argv.append('-fn "{}"'.format(self._font))
        argv.append('-e {}'.format(self._command))
        return ' '.join(argv)

    def _total_time(self):
        if self._status == 'started':
            return self._pomodoro_time
        elif self._status == 'resting':
            if self._pomodoro_count % 4 == 0:
                return self._long_rest_time
            else:
                return self._rest_time
        raise Exception('Invalid state')

    def _remaining(self):
        elapsed = (datetime.now() - self._start_time).seconds
        return self._total_time() - elapsed

    def _remaining_repr(self):
        remaining = self._remaining()
        if remaining <= 0:
            return '00:00'
        return '{:02d}:{:02d}'.format(remaining // 60, remaining % 60)

    def _progress(self):
        return '✓' * self._pomodoro_count

    def _i3bar_status(self):
        if self._status == 'started':
            return {
                'name': 'i3hub_taskmaster',
                'markup': 'none',
                'color': self._pomodoro_color,
                'full_text': '(Task: {}) Remaining: {} {}'.format(self._task,
                    self._remaining_repr(), self._progress())
                }
        elif self._status == 'resting':
            return {
                'name': 'i3hub_taskmaster',
                'markup': 'none',
                'color': self._rest_color,
                'full_text': '(Resting) Remaining: {} {}'.format(
                    self._remaining_repr(), self._progress())
                }

    def _message(self, writer, msg):
        print(msg)
        writer.write(msg.encode('utf-8') + b'\n')

    async def _start(self, writer, request):
        _, task_id = request
        if self._status != 'stopped':
            # already started, stop first
            self._stop()
            await self._pomodoro_loop_task
            if task_id == str(self._task.id):
                # same task id simply means stop
                return
        tw_export = await asyncio.create_subprocess_exec('task', 'export',
                task_id, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await tw_export.communicate()
        if tw_export.returncode:
            self._message(writer,
                    'taskwarrior task export failed: "{}"'.format(
                        stderr.decode('utf8').strip()))
            return
        self._task = Task(json.loads(stdout.decode('utf-8'))[0])
        self._pomodoro_count = 0
        self._start_time = datetime.now()
        self._status = 'started'
        self._message(writer, 'started task "{}"'.format(self._task))
        self._pomodoro_loop_task = self._loop.create_task(
                self._pomodoro_loop())

    def _stop(self):
        self._status = 'stopped'
        self._stop_future.set_result(None)

    def _play_sound(self, path):
        async def paplay(path):
            proc = await asyncio.create_subprocess_exec('paplay', path)
            await proc.wait()
        self._loop.create_task(paplay(path))

    async def _on_request(self, reader, writer):
        request = (await reader.read(1024)).decode('utf-8').split('\n')
        if request[0] == 'start':
            await self._start(writer, request)
        else:
            self._message(writer, 'invalid request')
        await writer.drain()
        writer.close()

    async def _pomodoro_loop(self):
        self._stop_future = asyncio.Future()
        task_id = str(self._task.id)
        self._pomodoro_count = 0
        self._start_time = datetime.now()
        tw = await asyncio.create_subprocess_exec('task', task_id, 'start')
        await tw.wait()
        while self._status != 'stopped':
            if self._remaining() <= 0:
                if self._status == 'started':
                    self._pomodoro_count += 1
                    self._status = 'resting'
                    self._start_time = datetime.now()
                    self._play_sound(self._alarm_rest)
                else:
                    assert self._status == 'resting'
                    self._status = 'started'
                    self._start_time = datetime.now()
                    self._play_sound(self._alarm_pomodoro)
            self._i3.refresh_i3bar()
            await asyncio.wait([self._stop_future], timeout=1)
        self._i3.refresh_i3bar()
        tw = await asyncio.create_subprocess_exec('task', task_id, 'stop')
        await tw.wait()
                
    @listen('i3hub::init')
    async def on_init(self, event, arg):
        config = arg['config']
        self._font = config.get('font', None)
        self._window_title = config.get('window-title', 'tasks')
        self._pomodoro_time = config.get('pomodoro-time', POMODORO_TIME)
        self._rest_time = config.get('rest-time', REST_TIME)
        self._long_rest_time = config.get('long-rest-time', LONG_REST_TIME)
        self._alarm_pomodoro = config.get('alarm-pomodoro', ALARM_POMODORO)
        self._alarm_rest = config.get('alarm-rest', ALARM_REST)
        self._pomodoro_color = config.get('pomodoro-color', POMODORO_COLOR)
        self._rest_color = config.get('rest-color', REST_COLOR)
        self._server = await asyncio.start_unix_server(self._on_request,
                self._socket_address, loop=self._loop)
        with open(self._vitrc_location, 'w') as f:
            f.write((
                'map S=:!r echo -n "start\\n%TASKID" | '
                'socat - ABSTRACT-CONNECT:{}<Return>\n'
                ).format(self._socket_address.decode('utf-8')[1:]))

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        status = self._i3bar_status()
        if status:
            status_array_merge(status_array, status)

    @listen('i3::workspace')
    async def on_workspace(self, event, arg):
        if arg['change'] == 'init' and arg['current']['name'] == 'tasks':
            await self._i3.command('exec {}'.format(self._argv()))
        if arg['change'] == 'focus' and arg['old']['name'] == 'tasks':
            await self._i3.command('[workspace=tasks] kill')

    @listen('i3::shutdown')
    async def on_shutdown(self, event, arg):
        if self._status != 'stopped':
            self._stop()
            await self._pomodoro_loop_task
