from i3hub import extension, listen


@extension
class KeyboardLayoutSwitcher(object):
    def __init__(self, i3):
        self._i3 = i3
        self._layouts = None
        self._current_layout = -1
        self._extra_xkb_opts = None

    def _merge(self, status_data):
        layout = self._layouts[self._current_layout]
        updated = False
        for item in status_data:
            if item['name'] == 'keyboard_layout':
                item.update({'full_text': layout})
                updated = True
                break
        if not updated:
            status_data.insert(0, {
                'name': 'keyboard_layout',
                'markup': 'none',
                'full_text': layout
                })

    async def _switch_layout(self):
        self._current_layout = (self._current_layout + 1) % len(self._layouts)
        new_layout = self._layouts[self._current_layout]
        await self._i3.command('exec setxkbmap {} {}'.format(new_layout,
            self._extra_xkb_opts))
        self._i3.update_status()

    @listen('i3hub::init')
    async def on_init(self, event, config):
        self._layouts = config.get('layouts', ['us'])
        self._extra_xkb_opts = config.get('extra-xkb-opts', '')
        await self._switch_layout()

    @listen('i3hub::status_click')
    async def on_status_click(self, event, payload):
        if payload['name'] == 'keyboard_layout':
            await self._switch_layout()

    @listen('i3hub::status_update')
    async def on_status_update(self, event, status_data):
        self._merge(status_data)

    @listen('extension::binding::switch-layout')
    async def on_binding(self, event, args):
        await self._switch_layout()
