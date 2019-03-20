# Simple extension that allows switching keyboard layouts with setxkbmap . If
# i3hub is running as a i3 bar command, the extension will also display a
# widget in the bar.
from i3hub import extension, listen, status_array_merge


@extension()
class KeyboardLayoutSwitcher(object):
    _I3HUB_STATUS_EXTENSION = True

    def __init__(self, i3):
        self._i3 = i3
        self._layouts = None
        self._current_layout = -1
        self._extra_xkb_opts = None

    def _merge(self, status_array):
        layout = self._layouts[self._current_layout]
        status_array_merge(status_array, {
            'name': 'keyboard_layout',
            'markup': 'none',
            'full_text': '\uf11c {}'.format(layout)
            })

    async def _switch_layout(self):
        self._current_layout = (self._current_layout + 1) % len(self._layouts)
        new_layout = self._layouts[self._current_layout]
        await self._i3.command('exec setxkbmap {} {}'.format(new_layout,
            self._extra_xkb_opts))
        self._i3.refresh_i3bar()

    @listen('i3hub::init')
    async def on_init(self, event, arg):
        self._i3.require('nop_binding')

        config = arg['config']
        self._layouts = config.get('layouts', ['us'])
        self._extra_xkb_opts = config.get('extra-xkb-opts', '')
        await self._switch_layout()

    @listen('i3hub::i3bar_click')
    async def on_i3bar_click(self, event, payload):
        if payload['name'] == 'keyboard_layout':
            await self._switch_layout()

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        self._merge(status_array)

    @listen('extension::nop-binding::switch-layout')
    async def on_binding(self, event, args):
        await self._switch_layout()
