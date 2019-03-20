# This extension implements automatic split toggling on enabled workspaces.
# Workspaces where automatic toggling is enabled is configured on
# ~/.config/i3hub/i3hub.cfg like this:
#
#       [split_alternator]
#       workspaces = ["1", "2", "3"]
# 
# On i3 configuration It is possible to configure a key to toggle this
# functionality at runtime:
#
#       bindsym Mod1+a nop toggle-split-alternator
from i3hub import extension, listen


@extension()
class SplitAlternator(object):
    def __init__(self, i3):
        self._i3 = i3
        self._enabled_workspaces = None
        self._current_workspace = None

    @listen('i3hub::init')
    async def on_init(self, event, arg):
        self._i3.require('nop_binding')

        config = arg['config']
        self._enabled_workspaces = set(config.get('workspaces', []))
        for workspace in await self._i3.get_workspaces():
            if workspace['focused']:
                self._current_workspace = workspace['name']
                break

    @listen('extension::nop-binding::toggle-split-alternator')
    async def on_toggle(self, event, arg):
        if self._current_workspace in self._enabled_workspaces:
            self._enabled_workspaces.remove(self._current_workspace)
        else:
            self._enabled_workspaces.add(self._current_workspace)

    @listen('i3::workspace')
    async def on_workspace(self, event, arg):
        if arg['change'] == 'focus':
            self._current_workspace = arg['current']['name']
        elif arg['change'] == 'init':
            await self._i3.command('split toggle')

    @listen('i3::window')
    async def on_window(self, event, arg):
        container = arg['container']
        if (arg['change'] == 'focus' and container['type'] == 'con' and
                self._current_workspace in self._enabled_workspaces):
            await self._i3.command('split toggle')
