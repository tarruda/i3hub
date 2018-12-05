import asyncio
import datetime
import json
import signal
import psutil

from i3hub import extension, listen, status_array_merge


KB = 1024
MB = KB * 1024
GB = MB * 1024

@extension()
class HubStatus(object):
    _I3HUB_STATUS_EXTENSION = True

    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._stop_sig = None
        self._cont_sig = None
        self._updating = False
        self._now = datetime.datetime.now()

    def _get_color(self, percent_usage):
        color = None
        if percent_usage >= 0.75:
            color = '#cc0000'
        elif percent_usage >= 0.5:
            color = '#ffcc00'
        return color

    def _disk(self):
        usage = psutil.disk_usage('/')
        used = usage.used / GB
        total = usage.total / GB
        return {
            'name': 'disk',
            'color': self._get_color(used / total),
            'markup': 'none',
            'full_text': '\uf0a0 {:.1f}G/{:.1f}G'.format(used, total)
        }

    def _memory(self):
        vm = psutil.virtual_memory()
        used = vm.used / GB
        total = vm.total / GB
        return {
            'name': 'memory',
            'color': self._get_color(used / total),
            'markup': 'none',
            'full_text': '\uf2db {:.1f}G/{:.1f}G'.format(used, total)
        }

    def _cpu(self):
        percent = psutil.cpu_percent()
        return {
            'name': 'cpu',
            'color': self._get_color(percent / 100),
            'markup': 'none',
            'full_text': '\uf233 {:.0f} %'.format(percent)
        }

    def _network(self):
        wifi_icon = '\uf1eb'
        net_icon = '\uf0e8'  # (this is actually the sitemap icon)
        vpn_icon = '\uf023'  # lock icon, try to find a better one later
        stats = psutil.net_if_stats()
        for k, interface in stats.items():
            if k == 'lo':
                continue
            if interface.isup:
                addrs = psutil.net_if_addrs()[k]
                return {
                    'name': 'network',
                    'markup': 'none',
                    'full_text': '{} {}'.format(net_icon, addrs[0].address)
                }

    def _battery(self):
        b = psutil.sensors_battery()
        if not b:
            return None
        return {
            'name': 'battery',
            'markup': 'none',
            'full_text': 'battery'
        }

    def _date(self):
        return {
            'name': 'date',
            'markup': 'none',
            'full_text': '\uf073 {}'.format(
                self._now.strftime('%Y-%m-%d %H:%M:%S'))
        }

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        stats = [
            self._disk(),
            self._memory(),
            self._cpu(),
            self._network(),
            self._battery(),
            self._date()
        ]
        for stat in stats:
            if stat:
                status_array.append(stat)


    @listen('i3hub::i3bar_suspend')
    async def on_i3bar_suspend(self, event, arg):
        pass

    @listen('i3hub::i3bar_resume')
    async def on_i3bar_resume(self, event, arg):
        pass

    @listen('i3hub::i3bar_click')
    async def on_i3bar_click(self, event, arg):
        pass

    @listen('i3hub::init')
    async def init(self, event, arg):
        self._loop.create_task(self.run())

    async def run(self):
        while True:
            await asyncio.sleep(1)
            self._now = datetime.datetime.now()
            if self._now.second % 5 == 0:
                self._i3.refresh_i3bar()
