# Example implementation of an i3 status line. The goal here is to avoid
# spawning external commands, almost everything is obtained through the psutil
# python module (sudo apt install python3-psutil). The only exception is the
# default network interface, which is read from /proc/net/route filesystem.

# This is intentionally not configurable, it serves more as an example or
# starting point to someone wanting to implement their own custom status line.
import asyncio
import datetime
import json
import os
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
        self._proc_net_route = None
        self._current_status = []
        self._counters = psutil.net_io_counters(pernic=True, nowrap=True)
        self._counters_timestamp = datetime.datetime.now().timestamp()

    def _get_color(self, percent_usage):
        color = 'green'
        if percent_usage >= 0.75:
            color = '#cc0000'
        elif percent_usage >= 0.5:
            color = '#ffcc00'
        return color

    def _get_default_gateway_interface(self):
        if not self._proc_net_route:
            if not os.path.exists('/proc/net/route'):
                return
            self._proc_net_route = open('/proc/net/route')
        self._proc_net_route.seek(0)
        self._proc_net_route.readline() # skip first line
        for line in self._proc_net_route:
            line = line.split()
            if len(line) > 1 and int(line[1], base=16) == 0:
                return line[0]

    def _compute_nic_throughput(self, nic, now):
        last_counters = self._counters
        last_timestamp = self._counters_timestamp
        self._counters = psutil.net_io_counters(pernic=True)
        self._counters_timestamp = now.timestamp()
        last_nc = last_counters.get(nic, None)
        nc = self._counters.get(nic, None)
        if not (nc and last_nc):
            return
        seconds_passed = self._counters_timestamp - last_timestamp
        download_rate = (nc.bytes_recv - last_nc.bytes_recv) / seconds_passed
        upload_rate = (nc.bytes_sent - last_nc.bytes_sent) / seconds_passed
        if download_rate < KB:
            download = '{:.0f} B/s'.format(download_rate)
        elif download_rate < MB:
            download = '{:.0f} K/s'.format(download_rate / KB)
        else:
            download = '{:.0f} M/s'.format(download_rate / MB)
        if upload_rate < KB:
            upload = '{:.0f} B/s'.format(upload_rate)
        elif upload_rate < MB:
            upload = '{:.0f} K/s'.format(upload_rate / KB)
        else:
            upload = '{:.0f} M/s'.format(upload_rate / MB)
        return download, upload

    def _disk(self, now):
        usage = psutil.disk_usage('/')
        used = usage.used / GB
        total = usage.total / GB
        return {
            'name': 'disk',
            'markup': 'pango',
            'full_text': ('<span foreground="{}">\uf0a0</span> '
                          '{:.1f}G/{:.1f}G').format(
                              self._get_color(used / total), used, total)
        }

    def _memory(self, now):
        vm = psutil.virtual_memory()
        used = vm.used / GB
        total = vm.total / GB
        return {
            'name': 'memory',
            'markup': 'pango',
            'full_text': ('<span foreground="{}">\uf2db</span> '
                          '{:.1f}G/{:.1f}G').format(
                            self._get_color(used / total), used, total)
        }

    def _cpu(self, now):
        percent = psutil.cpu_percent()
        return {
            'name': 'cpu',
            'markup': 'pango',
            'full_text': ('<span foreground="{}">\uf233</span> '
                          '{:.0f} %').format(self._get_color(percent / 100),
                              percent)
        }

    def _network(self, now):
        wifi_icon = '\uf1eb'
        net_icon = '\uf0e8'  # (this is actually the sitemap icon)
        vpn_icon = '\uf023'  # lock icon, try to find a better one later
        stats = psutil.net_if_stats()
        default_gateway_interface = self._get_default_gateway_interface()
        for k, interface in stats.items():
            if k == default_gateway_interface or (
                    not default_gateway_interface and interface.isup):
                addrs = psutil.net_if_addrs()[k]
                download, upload = self._compute_nic_throughput(k, now)
                return {
                    'name': 'network',
                    'markup': 'pango',

                    'full_text': ('{} {} '
                    '<span foreground="#0e93cb">\uf019</span> {} '
                    '<span foreground="#0e93cb">\uf093</span> {}').format(
                        net_icon, addrs[0].address, download, upload)
                }

    def _battery(self, now):
        b = psutil.sensors_battery()
        if not b:
            return None
            color = '#ffcc00'
        if b.power_plugged:
            icon = '\uf376' 
            color = 'green'
        elif b.percent > 75:
            icon = '\uf240' 
            color = 'green'
        elif b.percent > 50:
            icon = '\uf241' 
            color = '#ffcc00'
        elif b.percent > 25:
            icon = '\uf242' 
            color = '#ffcc00'
        elif b.percent > 10:
            color = '#cc0000'
            icon = '\uf243' 
        else:
            color = '#cc0000'
            icon = '\uf244' 
        return {
            'name': 'battery',
            'markup': 'pango',
            'full_text': '<span foreground="{}">{}</span> {:.0f}%'.format(
                color, icon, b.percent)
        }

    def _date(self, now):
        return {
            'name': 'date',
            'markup': 'none',
            'full_text': '\uf073 {}'.format(now.strftime('%Y-%m-%d %H:%M:%S'))
        }

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        status_array += self._current_status

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
        modules = [
            (self._date, 5),
            (self._battery, 30),
            (self._network, 10),
            (self._cpu, 5),
            (self._memory, 5),
            (self._disk, 30),
        ]

        def check(first_run):
            now = datetime.datetime.now()
            rv = False
            for module, update_frequency in modules:
                if first_run or now.second % update_frequency == 0:
                    result = module(now)
                    if result:
                        status_array_merge(self._current_status, result)
                        rv = True
            return rv

        check(True)
        self._i3.refresh_i3bar()
        while True:
            await asyncio.sleep(1)
            if check(False):
                self._i3.refresh_i3bar()
