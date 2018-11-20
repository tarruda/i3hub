import asyncio
import crontab
import glob
import json
import os
import re

from datetime import datetime

from i3hub import extension, listen, status_array_merge


SOCKET_ADDRESS = b'\0i3hub-taskmaster'
POMODORO_TIME = 25 * 60
REST_TIME = 5 * 60
ALARM_POMODORO = os.path.join(os.path.dirname(__file__), 'alarm-pomodoro.oga')
ALARM_REST = os.path.join(os.path.dirname(__file__), 'alarm-rest.oga')
BELL = os.path.join(os.path.dirname(__file__), 'bell.oga')
POMODORO_COLOR = '#0e93cb'
REST_COLOR = '#00b722'
TAG_PROJECT_SPEC = re.compile('^(tag|project):(.+)$', re.IGNORECASE)


def now():
    now = datetime.now()
    # zero time components after minute, or the test will most likely fail
    return datetime(now.year, now.month, now.day, now.hour, now.minute)


class Task(object):
    def __init__(self, task_dict, allowed_workspaces):
        self.id = task_dict['id']
        self.description = task_dict['description']
        project = task_dict.get('project', None)
        tags = task_dict.get('tags', [])
        allowed = []
        if project and ('project', project) in allowed_workspaces:
            allowed += allowed_workspaces[('project', project)]
        for tag in tags:
            if ('tag', tag) in allowed_workspaces:
                allowed += allowed_workspaces[('tag', tag)]
        self.first_allowed_workspace = allowed[0] if allowed else None
        self.allowed_workspaces = set(allowed)

    def __str__(self):
        return self.description

    def is_workspace_allowed(self, ws):
        return (not self.allowed_workspaces or ws == 'tasks' or
                ws in self.allowed_workspaces)


@extension(run_as_status_only=True)
class Taskmaster(object):
    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._vitrc_path = '{}/vitrc'.format(self._i3.runtime_dir)
        self._task_data_dir = os.getenv('TASKDATA',
                '{}/.task'.format(os.getenv('HOME')))
        self._command = 'vit'
        self._font = None
        self._window_title = None
        self._socket_address = SOCKET_ADDRESS
        self._pomodoro_time = None
        self._rest_time = None
        self._alarm_pomodoro = None
        self._alarm_rest = None
        self._pomodoro_color = None
        self._rest_color = None
        self._server = None
        self._status = 'stopped'
        self._start_time = None
        self._stop_future = None
        self._task = None
        self._pomodoro_count = 0
        self._pomodoro_loop_task = None
        self._allowed_workspaces = None
        self._warning_workspace_locked = False
        self._current_workspace = None
        self._calendar = None
        self._work_cron = None
        self._killing_tasks = False

    def _coefficients(self):
        d = now()
        rv = []
        for spec, crontab in self._calendar.items():
            if crontab.test(d):
                kind, value = spec
                rv.append('rc.urgency.user.{}.{}.coefficient=20.0'.format(kind,
                    value))
        return ' '.join(rv)

    def _argv(self):
        argv = ['VITRC={} urxvt -title {}'.format(self._vitrc_path,
            self._window_title)]
        if self._font:
            argv.append('-fn "{}"'.format(self._font))
        argv.append('-e {} {}'.format(self._command, self._coefficients()))
        return ' '.join(argv)

    def _total_time(self):
        if self._status == 'started':
            return self._pomodoro_time
        elif self._status == 'resting':
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
            color = self._pomodoro_color
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
        self._task = Task(json.loads(stdout.decode('utf-8'))[0],
                self._allowed_workspaces)
        self._pomodoro_count = 0
        self._start_time = datetime.now()
        self._status = 'started'
        self._switch_to_first_allowed_workspace()
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

    def _editing_task(self):
        return bool(glob.glob('{}/task.*.task'.format(self._task_data_dir)))

    async def _on_request(self, reader, writer):
        request = (await reader.read(1024)).decode('utf-8').split('\n')
        if request[0] == 'start':
            await self._start(writer, request)
        else:
            self._message(writer, 'invalid request')
        await writer.drain()
        writer.close()

    def _warn_workspace_locked(self):
        if self._warning_workspace_locked:
            return
        self._warning_workspace_locked = True
        self._play_sound(self._bell)
        async def blink():
            blink_colors = [self._pomodoro_color, '#FF8C00']
            count = 12
            while count:
                count -= 1
                self._pomodoro_color = blink_colors[count % 2]
                self._i3.refresh_i3bar()
                await asyncio.sleep(0.2)
            self._warning_workspace_locked = False
        self._loop.create_task(blink())

    def _switch_to_first_allowed_workspace(self):
        if (self._task.first_allowed_workspace and not
                self._task.is_workspace_allowed(self._current_workspace)):
            self._loop.create_task(self._i3.command('workspace {}'.format(
                self._task.first_allowed_workspace)))

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
                    if self._pomodoro_count % 4 == 0:
                        self._stop()
                else:
                    assert self._status == 'resting'
                    self._switch_to_first_allowed_workspace()
                    self._status = 'started'
                    self._start_time = datetime.now()
                    self._play_sound(self._alarm_pomodoro)
            self._i3.refresh_i3bar()
            await asyncio.wait([self._stop_future], timeout=1)
        self._i3.refresh_i3bar()
        tw = await asyncio.create_subprocess_exec('task', task_id, 'stop')
        await tw.wait()
        self._task = None
                
    @listen('i3hub::init')
    async def on_init(self, event, arg):
        config = arg['config']
        self._font = config.get('font', None)
        self._window_title = config.get('window-title', 'tasks')
        self._pomodoro_time = config.get('pomodoro-time', POMODORO_TIME)
        self._rest_time = config.get('rest-time', REST_TIME)
        self._alarm_pomodoro = config.get('alarm-pomodoro', ALARM_POMODORO)
        self._alarm_rest = config.get('alarm-rest', ALARM_REST)
        self._bell = config.get('bell', BELL)
        self._pomodoro_color = config.get('pomodoro-color', POMODORO_COLOR)
        self._rest_color = config.get('rest-color', REST_COLOR)
        self._server = await asyncio.start_unix_server(self._on_request,
                self._socket_address, loop=self._loop)
        user_vitrc = os.getenv('VITRC', '{}/.vitrc'.format(os.getenv('HOME')))
        with open(self._vitrc_path, 'w') as f:
            if os.path.exists(user_vitrc):
                with open(user_vitrc) as vrc:
                    f.write(vrc.read())
            f.write(('\n'
                'map S=:! echo -n "start\\n%TASKID" | '
                'socat - ABSTRACT-CONNECT:{}<Return>\n'
                ).format(self._socket_address.decode('utf-8')[1:]))
        workspace_policy = config.get('workspace-policy', {})
        self._allowed_workspaces = {}
        for k, v in workspace_policy.items():
            m = TAG_PROJECT_SPEC.match(k)
            if not m:
                print('invalid workspace policy key: {}'.format(k))
                continue
            if not isinstance(v, list):
                print('invalid workspace policy value must be a list')
                continue
            allowed = list((str(i) for i in v))
            self._allowed_workspaces[(m.group(1), m.group(2))] = allowed
        for workspace in await self._i3.get_workspaces():
            if workspace['focused']:
                self._current_workspace = workspace['name']
                break
        self._calendar = {}
        for spec, cron_expr in config.get('calendar', {}).items():
            if spec == 'work':
                self._work_cron = crontab.CronTab(str(cron_expr))
                continue
            m = TAG_PROJECT_SPEC.match(spec)
            if m:
                spec = m.group(1), m.group(2)
                ct = crontab.CronTab(str(cron_expr))
                self._calendar[spec] = ct

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        status = self._i3bar_status()
        if status:
            status_array_merge(status_array, status)

    def _kill_tasks(self):
        async def kill_soon():
            await asyncio.sleep(0.3)
            if self._current_workspace != 'tasks':
                await self._i3.command('[workspace=tasks] kill')
            self._killing_tasks = False
        if not self._killing_tasks:
            self._killing_tasks = True
            self._loop.create_task(kill_soon())

    def _next_workspace(self, new, old):
        if (self._status == 'started' and not
                self._task.is_workspace_allowed(new)):
            return old
        elif (self._status == 'stopped' and self._work_cron and
                self._work_cron.test(now())):
            return 'tasks'
        return new

    @listen('i3::workspace')
    async def on_workspace(self, event, arg):
        change = arg['change']
        new = arg['current']['name']
        if change == 'focus':
            self._current_workspace = new
            old = arg['old']['name']
            next_ws = self._next_workspace(new, old)
            if next_ws != new:
                await self._i3.command('workspace {}'.format(next_ws))
                if self._task:
                    self._warn_workspace_locked()
            elif old == 'tasks' and not self._editing_task():
                self._kill_tasks()
        elif change == 'init' and new == 'tasks':
            await self._i3.command('exec {}'.format(self._argv()))

    @listen('i3::shutdown')
    async def on_shutdown(self, event, arg):
        if self._status != 'stopped':
            self._stop()
            await self._pomodoro_loop_task
