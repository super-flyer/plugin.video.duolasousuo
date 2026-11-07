# -*- coding: utf-8 -*-
"""哆啦搜索 v2.0: 播放失败监听，自动切换线路并记录失效域名。

插件主脚本在播放前写 playback_state.json；本 service 在 Kodi 启动时常驻，
监听播放失败，按「不同域名、未尝试过」的顺序自动尝试备用线路（最多 3 次），
并把失败域名写入 playback_health.json（冷却 30 分钟），供下次自动跳过。
"""
import json
import os
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
DATA_DIR = os.path.join(xbmcvfs.translatePath('special://profile/addon_data'), ADDON.getAddonInfo('id'), '')
STATE_FILE = os.path.join(DATA_DIR, 'playback_state.json')
HEALTH_FILE = os.path.join(DATA_DIR, 'playback_health.json')
COOLDOWN = 1800
MAX_SWITCH = 3
STATE_TTL = 60
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def read_json(path, default=None):
    try:
        with xbmcvfs.File(path) as f:
            return json.loads(f.read() or '{}')
    except Exception:
        return {} if default is None else default


def write_json(path, data):
    try:
        xbmcvfs.mkdirs(DATA_DIR)
        with xbmcvfs.File(path, 'w') as f:
            f.write(json.dumps(data, ensure_ascii=False))
        return True
    except Exception as e:
        print('duola_debug:service write failed=' + str(e))
        return False


def host_of(url):
    try:
        return urllib.parse.urlparse((url or '').split('|', 1)[0]).netloc.lower()
    except Exception:
        return ''


def with_headers(url):
    """自动换线路时补上 UA / Referer，避免 CDN 防盗链直接拒绝。"""
    if not url or '|' in url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.netloc:
            return url
        referer = urllib.parse.quote(parsed.scheme + '://' + parsed.netloc + '/', safe='')
        ua = urllib.parse.quote(UA, safe='')
        return url + '|User-Agent=' + ua + '&Referer=' + referer
    except Exception:
        return url


def record_failure(url, now=None):
    now = now or int(time.time())
    host = host_of(url)
    if not host:
        return
    data = read_json(HEALTH_FILE)
    entry = data.get(host) if isinstance(data.get(host), dict) else {}
    entry['fail_until'] = now + COOLDOWN
    entry['fails'] = int(entry.get('fails', 0)) + 1
    entry['last_fail'] = now
    data[host] = entry
    write_json(HEALTH_FILE, data)


def pick_next(state, current, max_switch=MAX_SWITCH):
    """纯函数：从候选里挑一个不同域名、未尝试过的地址。"""
    if state.get('attempts', 0) >= max_switch:
        return None
    host = host_of(current)
    tried = set(state.get('tried') or [])
    for url in state.get('candidates') or []:
        if not url or url == current or url in tried:
            continue
        if host_of(url) == host:
            continue
        return url
    return None


class FailureWatcher(xbmc.Player):
    def onPlayBackError(self):
        self.handle_failure()

    def onPlayBackStopped(self):
        # 部分失败场景 Kodi 只发 Stopped；仅在流尚未加载时也按失败处理
        try:
            if self.getTime() <= 1 and self.getTotalTime() <= 0:
                self.handle_failure()
        except Exception:
            pass

    def handle_failure(self):
        state = read_json(STATE_FILE)
        if not state or not state.get('current'):
            return
        now = int(time.time())
        if now - int(state.get('written', 0)) > STATE_TTL:
            write_json(STATE_FILE, {})
            return

        current = state.get('current', '')
        tried = list(state.get('tried') or [])
        if current and current not in tried:
            tried.append(current)
        record_failure(current, now)

        next_url = pick_next(dict(state, tried=tried), current)
        if not next_url:
            write_json(STATE_FILE, {})
            xbmcgui.Dialog().notification(
                '哆啦搜索', '播放失败，且没有可用的备用线路，请返回选择其他源',
                xbmcgui.NOTIFICATION_WARNING, 4000)
            return

        state.update({
            'current': next_url,
            'attempts': int(state.get('attempts', 0)) + 1,
            'tried': tried,
            'written': now,
        })
        write_json(STATE_FILE, state)
        xbmcgui.Dialog().notification('哆啦搜索', '该线路不可用，正在自动切换线路…', time=3000, sound=False)
        try:
            self.play(with_headers(next_url))
        except Exception as e:
            print('duola_debug:service auto switch failed=' + str(e))


if __name__ == '__main__':
    watcher = FailureWatcher()
    monitor = xbmc.Monitor()
    while not monitor.abortRequested():
        if monitor.waitForAbort(5):
            break
    del watcher
