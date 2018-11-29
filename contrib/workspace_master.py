import asyncio
import re
import sys

from i3hub import extension, listen 


EXEC_PATTERN = re.compile('^exec\s+')

@extension()
class WorkspaceMaster(object):
    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._workspaces = None
        self._wait_future = None

    @listen('i3hub::init')
    async def on_init(self, event, arg):
        self._i3.require('nop_binding')

        config = arg['config']
        self._workspaces = {}
        for k, v in config.get('workspaces', {}).items():
            directory = v.get('directory', None)
            self._workspaces[k] = {
                'materialized': v.get('materialized', False)
            }
            if not directory:
                self._workspaces[k]['commands'] = v.get('commands', [])
                continue
            self._workspaces[k]['commands'] = []
            for cmd in v['commands']:
                m = EXEC_PATTERN.match(cmd)
                if m:
                    prefix = m.group(0)
                    cmd = '{} cd {} && {}'.format(prefix, directory,
                            cmd[len(prefix):])
                self._workspaces[k]['commands'].append(cmd)

    async def _run_workspace_commands(self, workspace):
        for cmd in workspace['commands']:
            if cmd == '[wait-for-window]':
                f = asyncio.Future()
                self._wait_future = f
                await asyncio.wait([self._wait_future], timeout=1)
                if f.done() and f.result():
                    break
                continue
            reply = await self._i3.command(cmd)
            if not reply[0]['success']:
                print('failed to execute', cmd, file=sys.stderr)

    @listen('extension::nop-binding::select-workspace')
    async def on_select_workspace(self, event, arg):
        rofi = await asyncio.create_subprocess_exec('rofi', '-p',
                'Select workspace ', '-dmenu', stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE)
        active_workspaces = map(lambda w: w['name'],
                await self._i3.get_workspaces())
        shown_workspaces = []
        for k, v in self._workspaces.items():
            if v['materialized'] and k not in active_workspaces:
                continue
            shown_workspaces.append(k)
        workspaces = '\n'.join(sorted(shown_workspaces,
            key=lambda w: w not in active_workspaces))
        selected, _ = await rofi.communicate(workspaces.encode('utf-8'))
        selected = selected.decode('utf-8').strip()
        if not selected:
            return
        await self._i3.command('workspace {}'.format(selected))

    @listen('i3::window')
    async def on_window(self, event, arg):
        if arg['change'] == 'focus' and self._wait_future:
            self._wait_future.set_result(False)
            self._wait_future = None

    @listen('i3::workspace')
    async def on_workspace(self, event, arg):
        if self._wait_future:
            # stop running setup commands for workspace
            self._wait_future.set_result(True)
            self._wait_future = None
        if arg['change'] != 'init':
            # only run when workspace is initializing
            return
        workspace = self._workspaces.get(arg['current']['name'], None)
        if workspace:
            # run commands in a separate task, since there's the possibility of
            # long blocking due to waiting for windows to spawn
            self._loop.create_task(self._run_workspace_commands(workspace))
