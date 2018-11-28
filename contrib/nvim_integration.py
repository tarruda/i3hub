import asyncio
import os
import shlex

import mpack.asyncio
from i3hub import extension, listen

@extension()
class NvimIntegration(object):
    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._current_window = None
        self._current_workspace = None
        self._enabled_workspaces = None
        # map running nvim pids to their respective msgpack-rpc connections.
        # Each connection will be removed automatically by `nvim_poll` tasks
        # once nvim exits.
        self._nvim_connections = {}

    async def _spawn(self, *args):
        proc = await asyncio.create_subprocess_exec(*args,
                stdout=asyncio.subprocess.PIPE, loop=self._loop)
        stdout, _ = await proc.communicate()
        await proc.wait()
        return stdout.decode('utf-8', 'replace').split('\n')

    async def _set_current_window(self, container):
        props = container.get('window_properties', None)
        if not props:
            return
        window = {
            'id': container['window'],
            'title': props['title'],
            'class': props['class'],
            'instance': props['instance'],
            'pid': None
        }
        window_id = str(window['id'])
        out = await self._spawn('xprop', '-id', window_id, '_NET_WM_PID')
        try:
            window['pid'] = int(out[0].split()[-1])
        except:
            pass
        self._current_window = window

    async def _ps_children(self, pid):
        """Helper to list child processes of `pid`."""
        # source:
        # https://unix.stackexchange.com/questions/152140/find-out-which-process-is-in-foreground-on-bash
        out = await self._spawn('ps', '-O', 'stat', '--ppid', str(pid))
        return [l.split() for l in out[1:] if l]

    async def _find_foreground_pid(self, session_pid):
        for proc_data in await self._ps_children(session_pid):
            if '+' in proc_data[1]:
                return int(proc_data[0])

    async def _find_session_leader(self, window_pid):
        if not window_pid:
            return
        for proc_data in await self._ps_children(window_pid):
            if 's' in proc_data[1]:
                return '+' in proc_data[1], int(proc_data[0])

    async def _nvim_poll(self, pid, conn):
        """Helper to watch for nvim connection EOF."""
        while True:
            msg = await conn.next_message()
            if not msg:
                break
        print('connection with pid {} closed'.format(pid))
        del self._nvim_connections[pid]

    async def _nvim_navigate(self, direction):
        try:
            conn = await self._get_nvim_connection()
            if not conn:
                # not an nvim window
                return False
            # Invoke the defined helper function to navigate
            wincmd = {
                'up': 'k',
                'down': 'j',
                'left': 'h',
                'right': 'l',
            }[direction]
            return await conn.request('nvim_call_function', 'g:I3WindowNav',
                    [wincmd])
        except Exception as e:
            print('nvim navigation failed due to exception', e)
            return False

    async def _get_nvim_connection(self):
        """Check if the window is running nvim, and if so return an open
        msgpack-rpc connection to it."""

        if self._current_window['class'] != 'URxvt':
            # don't do anything for non-terminal windows
            return None

        # Find the session leader of the window.
        leader = await self._find_session_leader(self._current_window['pid'])
        if not leader:
            return None
        is_foreground, session_pid = leader
        if is_foreground:
            # The session leader is also the foreground process
            foreground_pid = session_pid
        else:
            # Find the foreground process
            foreground_pid = await self._find_foreground_pid(session_pid)
        # Optimization: if the foreground_pid is cached in nvim_connections,
        # return the cached msgpack-rpc session
        if foreground_pid in self._nvim_connections:
            return self._nvim_connections[foreground_pid]
        # find if the foreground pid is nvim
        out = await self._spawn('readlink',
                '/proc/{}/exe'.format(foreground_pid))
        prog = out[0].strip()
        if os.path.basename(prog) != 'nvim':
            return None
        # if yes, parse lsof output to discover the socket path
        lsof_out = await self._spawn('lsof', '-U', '-a', '-p',
                str(foreground_pid))
        socket_path = None
        for line in lsof_out[1:]:
            data = line.split()
            if data[8][0] == '/':
                socket_path = data[8]
                break
        # create the msgpack-rpc connection
        reader, writer = await asyncio.open_unix_connection(socket_path,
                loop=self._loop)
        conn = mpack.asyncio.Session(reader, writer)
        self._nvim_connections[foreground_pid] = conn
        # define a helper function to switch windows and check if succeeded
        define_helper_fn = (
                'exe ":function! g:I3WindowNav(cmd)\\n'
                ' let g:i3_oldw = winnr()\\n'
                ' silent exe \'wincmd \' . a:cmd\\n'
                ' return g:i3_oldw != winnr()\\n'
                'endfunction"')
        await conn.request('nvim_command', define_helper_fn)
        # create a task to listen for nvim connection close (eg shutdown) so we
        # can remove the cached connection
        self._loop.create_task(self._nvim_poll(foreground_pid, conn))
        return conn

    @listen('i3hub::init')
    async def on_init(self, event, arg):
        self._i3.require('nop_binding')

        def find_focused_window(node):
            if node['focused']:
                return node
            for n in node['nodes']:
                rv = find_focused_window(n)
                if rv:
                    return rv

        def find_focused_workspace(workspaces):
            for workspace in workspaces:
                if workspace['focused']:
                    return workspace['name']

        config = arg['config']
        self._enabled_workspaces = set(config.get('workspaces', []))
        self._current_workspace = find_focused_workspace(
                await self._i3.get_workspaces())
        await self._set_current_window(find_focused_window(
            await self._i3.get_tree()))

    @listen('extension::nop-binding::focus')
    async def on_focus(self, event, argv):
        direction = argv[0]
        ws_enabled = self._current_workspace in self._enabled_workspaces
        if not ws_enabled or not await self._nvim_navigate(direction):
            await self._i3.command('focus {}'.format(direction))

    @listen('i3::workspace')
    async def on_workspace(self, event, arg):
        if arg['change'] == 'focus':
            self._current_workspace = arg['current']['name']

    @listen('i3::window')
    async def on_window(self, event, arg):
        "Called when the focused container changes."

        container = arg['container']
        if arg['change'] == 'focus' and container['type'] == 'con':
            await self._set_current_window(container)

    @listen('i3::shutdown')
    async def on_shutdown(self, event, arg):
        for conn in self._nvim_connections.values():
            conn.close()




