import importlib.util
import json
import os
import sys
import time
import types

sys.argv = ['plugin.py', '1', '']

# Kodi / requests stubs: 让测试在本机无 Kodi、无第三方依赖时也能运行
_VFS = {}


class DummyFile:
    def __init__(self, path, mode='r'):
        self.path = path
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return _VFS.get(self.path, '')

    def write(self, data):
        _VFS[self.path] = data


class DummyListItem:
    def __init__(self, *a, **k):
        self.args = a
        self.kwargs = k

    def __getattr__(self, name):
        return lambda *a, **k: None


class RecordingListItem:
    """记录 v1.9 prepare_play_item 设置的属性。"""

    def __init__(self):
        self.properties = {}
        self.mime = None
        self.content_lookup = None

    def setProperty(self, key, value):
        self.properties[key] = value

    def setMimeType(self, value):
        self.mime = value

    def setContentLookup(self, value):
        self.content_lookup = value

    def setArt(self, *a, **k):
        pass

    def setInfo(self, *a, **k):
        pass


class DummyDialog:
    def notification(self, *a, **k):
        pass

    def ok(self, *a, **k):
        return True

    def select(self, *a, **k):
        return -1

    def input(self, *a, **k):
        return ''


requests = types.ModuleType('requests')


class _RequestException(Exception):
    pass


requests.exceptions = types.SimpleNamespace(RequestException=_RequestException, Timeout=_RequestException)


def _offline_get(url=None, headers=None, timeout=None):
    raise _RequestException('network disabled in tests')


requests.get = _offline_get


class _HeadResponse:
    status_code = 200


def _offline_head(url=None, headers=None, timeout=None, allow_redirects=True):
    return _HeadResponse()


requests.head = _offline_head
sys.modules['requests'] = requests

xbmc = types.ModuleType('xbmc')
xbmc.PLAYLIST_VIDEO = 1
xbmc.getInfoLabel = lambda *a, **k: '21.3'


class _DummyPlayer:
    def play(self, *a, **k):
        pass

    def getTime(self):
        return 0

    def getTotalTime(self):
        return 0


class _DummyMonitor:
    def abortRequested(self):
        return True

    def waitForAbort(self, timeout):
        return True


xbmc.Player = _DummyPlayer
xbmc.Monitor = _DummyMonitor
xbmcgui = types.ModuleType('xbmcgui')
xbmcgui.ListItem = DummyListItem
xbmcgui.Dialog = DummyDialog
xbmcgui.NOTIFICATION_WARNING = 1
xbmcvfs = types.ModuleType('xbmcvfs')
xbmcvfs.translatePath = lambda p: p
xbmcvfs.exists = lambda p: p in _VFS
xbmcvfs.mkdirs = lambda p: None
xbmcvfs.delete = lambda p: _VFS.pop(p, None)
xbmcvfs.File = lambda path, mode='r': DummyFile(path, mode)
xbmcaddon = types.ModuleType('xbmcaddon')


class Addon:
    def __init__(self):
        self._settings = {'Duola_play_style': '0'}

    def getSetting(self, key):
        return self._settings.get(key, '')

    def getAddonInfo(self, key):
        return {'path': '.', 'id': 'plugin.video.duolasousuo', 'version': '1.0.0'}.get(key, '')


xbmcaddon.Addon = Addon
xbmcplugin = types.ModuleType('xbmcplugin')
xbmcplugin.addDirectoryItem = lambda *a, **k: None
xbmcplugin.endOfDirectory = lambda *a, **k: None
xbmcplugin.setContent = lambda *a, **k: None
sys.modules['xbmc'] = xbmc
sys.modules['xbmcgui'] = xbmcgui
sys.modules['xbmcvfs'] = xbmcvfs
sys.modules['xbmcaddon'] = xbmcaddon
sys.modules['xbmcplugin'] = xbmcplugin

spec = importlib.util.spec_from_file_location('main_mod', 'main.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HEALTH_TEST_FILE = 'special://profile/addon_data/test/engine_health.json'


def test_parse_fongmi_config():
    text = '{"sites":[{"key":"demo","name":"Demo","api":"https://example.com/api.php/provide/vod/"}]}'
    parsed = mod.parse_fongmi_config(text)
    assert isinstance(parsed, dict)
    assert parsed['sites'][0]['key'] == 'demo'


def test_extract_text_api_candidates():
    candidates = mod.extract_text_api_candidates('接口：https://www.example.com/api.php/provide/vod/ 备用')
    assert any('example.com/api.php/provide/vod' in item for item in candidates)


def test_build_site_api_candidates():
    site = {'api': 'https://example.com/api.php/provide/vod/'}
    candidates = mod.build_site_api_candidates(site)
    assert candidates[0].startswith('https://')


def test_normalize_url():
    assert mod.normalize_url('example.com') == 'https://example.com'


def test_resolve_config_sites_from_interface_page(monkeypatch):
    # 配置页探测只解析固定 seed 返回的站点列表，不递归抓页面链接
    class FakeResponse:
        ok = True
        text = '{"sites":[{"key":"demo","api":"https://example.com/api.php/provide/vod/"}]}'

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(mod.requests, 'get', fake_get)
    sites = mod.resolve_config_sites('https://example.com/tv')
    assert any(site.get('api') == 'https://example.com/api.php/provide/vod/' for site in sites)


def test_extract_candidate_urls_from_relative_links():
    urls = mod.extract_candidate_urls('<a href="/api.php/provide/vod/">link</a>', 'https://example.com/tv')
    assert any(url == 'https://example.com/api.php/provide/vod' for url in urls)


def test_build_source_label_uses_explicit_name_and_host():
    assert mod.build_source_label('本机接口', 'https://example.com/api.php/provide/vod') == '本机接口'
    assert mod.build_source_label('', 'https://example.com/api.php/provide/vod') == 'example.com'


def test_request_engine_json_candidates_collects_multiple_sources(monkeypatch):
    class FakeResponse:
        def __init__(self, text, ok=True):
            self.text = text
            self.ok = ok

    def fake_get(url, headers=None, timeout=None):
        # 候选地址都带 ?wd= 后缀，不能用 endswith 判断接口路径
        if '/api.php/provide/vod' in url:
            return FakeResponse('{"code":1,"list":[{"vod_id":"1","vod_name":"A","type_name":"t","vod_remarks":"r"}]}')
        return FakeResponse('{"code":0}', ok=False)

    monkeypatch.setattr(mod.requests, 'get', fake_get)
    results = mod.request_engine_json_candidates('https://example.com/tv', '?wd=测试')
    assert len(results) >= 2


# ---------------------------------------------------------------------------
# v1.8: 增源 / 限时 / 健康缓存
# ---------------------------------------------------------------------------
def test_parse_extra_search_engines():
    raw = '家里=direct:http://10.0.0.2:8080/vod/|https://a.com/api.php/provide/vod/|https://a.com/api.php/provide/vod|ftp://bad|'
    items = mod.parse_extra_search_engines(raw)
    assert len(items) == 2
    assert items[0]['name'] == '家里'
    assert items[0]['url'] == 'http://10.0.0.2:8080/vod/'
    assert items[0]['direct'] is True
    assert items[1]['name'] == 'a.com'
    assert items[1]['direct'] is True
    assert mod.parse_extra_search_engines('') == []


def test_build_search_engines_local_cloud_extra():
    local = 'https://local.example/api.php/provide/vod/'
    cloud = [
        {'name': '云1', 'url': 'https://cloud.example/api.php/provide/vod/'},
        {'name': '重复本机', 'url': local},
    ]
    engines = mod.build_search_engines(local, '额外=https://extra.example/api.php/provide/vod/', cloud, True)
    assert [e['name'] for e in engines] == ['本机接口', '云1', '额外']
    assert all(e['direct'] for e in engines)
    off = mod.build_search_engines(local, '', cloud, False)
    assert len(off) == 1 and off[0]['name'] == '本机接口'


def test_classify_engine_health():
    assert mod.classify_engine_health({'json_ok': True, 'list_count': 0}) == 'empty'
    assert mod.classify_engine_health({'json_ok': True, 'list_count': 3}) == 'ok'
    assert mod.classify_engine_health({'timed_out': True}) == 'timeout'
    assert mod.classify_engine_health({'network_failures': 2}) == 'failed'


def test_health_update_and_filter():
    _VFS.pop(HEALTH_TEST_FILE, None)
    url = 'https://dead.example/api.php/provide/vod/'
    key = mod.normalize_url(url)
    now = 1000000
    data = mod.health_update(url, 'failed', path=HEALTH_TEST_FILE, now=now)
    assert data[key]['fail_until'] == now + mod.ENGINE_COOLDOWN
    assert mod.health_is_cooling(url, data, now + 10) is True
    engines = [{'name': 'dead', 'url': url}, {'name': 'ok', 'url': 'https://ok.example/'}]
    assert [e['name'] for e in mod.filter_healthy_engines(engines, data, now + 10)] == ['ok']
    # 正常返回（哪怕没有结果）必须解除冷却，不能误杀可用源
    data2 = mod.health_update(url, 'empty', path=HEALTH_TEST_FILE, now=now + 20)
    assert 'fail_until' not in data2[key]
    assert len(mod.filter_healthy_engines(engines, data2, now + 30)) == 2
    assert mod.health_clear(path=HEALTH_TEST_FILE) is True
    assert HEALTH_TEST_FILE not in _VFS


def test_remaining_timeout():
    assert mod.remaining_timeout(5) == 5
    assert mod.remaining_timeout(5, time.time() + 100) == 5
    assert mod.remaining_timeout(5, time.time() + 2) <= 2
    assert mod.remaining_timeout(5, time.time() - 1) == 0


def test_direct_engine_uses_single_request(monkeypatch):
    calls = []

    class FakeResponse:
        text = '{"code":1,"list":[{"vod_id":"1"}]}'
        status_code = 200

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, timeout))
        return FakeResponse()

    monkeypatch.setattr(mod.requests, 'get', fake_get)
    stats = {}
    results = mod.request_engine_json_candidates(
        'http://10.0.0.2:8080/vod/', '?wd=测试',
        deadline=time.time() + 5, stats=stats, direct=True)
    assert len(calls) == 1
    assert calls[0][0].startswith('http://10.0.0.2:8080/vod/?wd=')
    assert len(results) == 1 and stats['json_ok'] is True


def test_deadline_stops_candidate_probing(monkeypatch):
    calls = []

    class SlowResponse:
        text = '{"code":0}'
        status_code = 200
        ok = True

    def fake_get(url, headers=None, timeout=None):
        time.sleep(0.02)
        calls.append(url)
        return SlowResponse()

    monkeypatch.setattr(mod.requests, 'get', fake_get)
    stats = {}
    start = time.time()
    mod.request_engine_json_candidates(
        'https://example.com/tv', '?wd=x',
        deadline=start + 0.05, stats=stats)
    assert stats['timed_out'] is True
    assert time.time() - start < 0.5
    assert len(calls) < 7


def test_web_load_search_multi_returns_partial_results(monkeypatch):
    added = []
    monkeypatch.setattr(mod.xbmcplugin, 'addDirectoryItem', lambda *a, **k: added.append(a))

    class FakeResponse:
        text = '{"code":1,"list":[{"vod_id":"1","vod_name":"影片","type_name":"电影","vod_remarks":"HD"}]}'
        status_code = 200

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(mod.requests, 'get', fake_get)
    engines = mod.build_search_engines('https://local.example/api.php/provide/vod/', '', [], False)
    start = time.time()
    mod.Web_load_search_multi(engines, '测试', deadline_seconds=1)
    assert time.time() - start < 2.0
    assert added


def test_is_remote_version_newer():
    assert mod.is_remote_version_newer('1.9.0', '1.8.0') is True
    assert mod.is_remote_version_newer('2.0', '1.8.0') is True
    assert mod.is_remote_version_newer('1.7.0', '1.8.0') is False
    assert mod.is_remote_version_newer('1.8.0', '1.8.0') is False
    assert mod.is_remote_version_newer('1.8', '1.8.0') is False
    assert mod.is_remote_version_newer('garbage', '1.8.0') is False


def test_extra_engine_setting_has_localized_label():
    with open('resources/settings.xml', encoding='utf-8') as f:
        text = f.read()
    assert '额外搜索接口' in text


# ---------------------------------------------------------------------------
# v1.9: 线路分组 / MIME / adaptive / header
# ---------------------------------------------------------------------------
def test_check_url_mime_handles_query_and_no_extension():
    assert mod.check_url_mime('https://x/a.m3u8?token=abc') is True
    assert mod.check_url_mime('https://x/a.mp4?x=1') is True
    assert mod.check_url_mime('https://x/play/abc123') is True
    assert mod.check_url_mime('https://x/index.html') is False
    assert mod.check_url_mime('https://x/pic.jpg') is False


def test_get_url_media_type():
    assert mod.get_url_media_type('https://x/a.m3u8?token=1') == 'hls'
    assert mod.get_url_media_type('https://x/a.mpd') == 'dash'
    assert mod.get_url_media_type('https://x/a.mp4?x=1') == 'file'
    assert mod.get_url_media_type('https://x/play/id') == 'unknown'


def test_build_stream_headers():
    header = mod.build_stream_headers('https://cdn.example.com/a.m3u8?token=1')
    assert 'User-Agent=' in header
    assert 'Referer=https%3A%2F%2Fcdn.example.com%2F' in header
    assert 'Origin=https%3A%2F%2Fcdn.example.com' in header
    assert mod.build_stream_headers('') == ''


def test_get_kodi_major():
    assert mod.get_kodi_major() == 21


def test_detect_inputstream_adaptive():
    assert mod.detect_inputstream_adaptive(lambda _id: object()) is True

    def missing(_id):
        raise RuntimeError('not installed')

    assert mod.detect_inputstream_adaptive(missing) is False


def test_prepare_play_item_hls_with_adaptive():
    item = RecordingListItem()
    url = mod.prepare_play_item(item, 'https://cdn.example.com/a.m3u8?token=1', use_adaptive=True, major=21)
    assert url == 'https://cdn.example.com/a.m3u8?token=1'
    assert item.properties['inputstream'] == 'inputstream.adaptive'
    assert item.properties['inputstream.adaptive.manifest_type'] == 'hls'
    assert item.mime == mod.MIME_HLS
    assert 'Referer=' in item.properties['inputstream.adaptive.stream_headers']
    assert item.content_lookup is False


def test_prepare_play_item_kodi18_property():
    item = RecordingListItem()
    mod.prepare_play_item(item, 'https://x/a.m3u8', use_adaptive=True, major=18)
    assert item.properties['inputstreamaddon'] == 'inputstream.adaptive'
    assert 'inputstream' not in item.properties


def test_prepare_play_item_fallback_direct():
    item = RecordingListItem()
    url = mod.prepare_play_item(item, 'https://x/a.mp4?x=1', use_adaptive=False, major=21)
    assert url.startswith('https://x/a.mp4?x=1|')
    assert 'Origin=' in url
    assert item.mime is None


def test_prepare_play_item_hls_without_adaptive():
    item = RecordingListItem()
    url = mod.prepare_play_item(item, 'https://x/a.m3u8', use_adaptive=False, major=21)
    assert url.startswith('https://x/a.m3u8|')
    assert item.mime == mod.MIME_HLS
    assert 'inputstream' not in item.properties


def test_parse_play_lines_groups_flags():
    video = {
        'vod_play_from': '线路A$$$线路B',
        'vod_play_url': '01集$https://a/1.m3u8#02集$https://a/2.m3u8$$$01集$https://b/1.mp4',
    }
    lines = mod.parse_play_lines(video)
    assert [line['name'] for line in lines] == ['线路A', '线路B']
    assert len(lines[0]['episodes']) == 2
    assert lines[0]['episodes'][0] == {'name': '01集', 'url': 'https://a/1.m3u8'}
    assert len(lines[1]['episodes']) == 1


def test_parse_play_lines_single_line_fallback_name():
    video = {'vod_play_from': '', 'vod_play_url': '01集$https://a/1.m3u8?token=x'}
    lines = mod.parse_play_lines(video)
    assert len(lines) == 1 and lines[0]['name'] == '线路1'
    assert lines[0]['episodes'][0]['url'].endswith('?token=x')


def test_parse_play_lines_skips_non_video():
    video = {'vod_play_from': '', 'vod_play_url': '广告$https://a/index.html#正片$https://a/1.mp4'}
    lines = mod.parse_play_lines(video)
    assert len(lines[0]['episodes']) == 1
    assert lines[0]['episodes'][0]['name'] == '正片'


# ---------------------------------------------------------------------------
# v2.0: 播放域名健康缓存 / 线路预检 / 自动换线路 / bridge
# ---------------------------------------------------------------------------
def _load_sibling(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service_mod = _load_sibling('service_mod', 'service.py')
bridge_mod = _load_sibling('bridge_mod', 'bridge/duola_bridge.py')
PLAYBACK_TEST_FILE = 'special://profile/addon_data/test/playback_health.json'
STATE_TEST_FILE = 'special://profile/addon_data/test/playback_state.json'


def test_play_host():
    assert mod.play_host('https://cdn.example.com:8443/a.m3u8?token=1|Referer=x') == 'cdn.example.com:8443'
    assert mod.play_host('') == ''


def test_is_indirect_play_url():
    assert mod.is_indirect_play_url('https://x/share/7d3127abc') is True
    assert mod.is_indirect_play_url('https://x/play/abc123') is True
    assert mod.is_indirect_play_url('https://x/a.m3u8?token=1') is False
    assert mod.is_indirect_play_url('https://x/play/movie.m3u8') is False


def test_playback_health_cools_and_clears():
    _VFS.pop(PLAYBACK_TEST_FILE, None)
    url = 'https://super.ffzy-online6.com/20261025/44063/index.m3u8'
    now = 2000000
    data = mod.playback_record_failure(url, path=PLAYBACK_TEST_FILE, now=now)
    assert mod.play_host(url) in data
    assert mod.playback_is_cooling(url, data, now + 10) is True
    assert mod.playback_is_cooling('https://other.example/a.m3u8', data, now + 10) is False
    assert mod.playback_clear(path=PLAYBACK_TEST_FILE) is True
    assert PLAYBACK_TEST_FILE not in _VFS


def test_parse_play_lines_marks_indirect_and_cooling():
    video = {
        'vod_play_from': 'feifan$$$ffm3u8',
        'vod_play_url': ('第01集$https://super.ffzy-online6.com/share/abc123$$$'
                         '第01集$https://cdn.example.com/1.m3u8'),
    }
    lines = mod.parse_play_lines(video, health={}, now=1)
    assert lines[0]['indirect'] is True and lines[0]['available'] is True
    assert lines[1]['indirect'] is False
    cooled = {'cdn.example.com': {'fail_until': 9999999999}}
    assert mod.parse_play_lines(video, health=cooled, now=1)[1]['available'] is False


def test_probe_play_lines_marks_failed(monkeypatch):
    failures = []
    monkeypatch.setattr(mod, 'playback_record_failure', lambda url, **kw: failures.append(url) or {})
    lines = [
        {'name': 'A', 'episodes': [{'name': '1', 'url': 'https://a.example/1.m3u8'}]},
        {'name': 'B', 'episodes': [{'name': '1', 'url': 'https://b.example/1.m3u8'}]},
        {'name': 'C', 'episodes': [{'name': '1', 'url': 'https://c.example/share/abc'}]},
    ]
    probed = mod.probe_play_lines(lines, health={}, now=1,
                                  probe_func=lambda url: not url.startswith('https://b.'))
    assert probed[0]['available'] is True
    assert probed[1]['available'] is False and failures == ['https://b.example/1.m3u8']
    assert probed[2]['indirect'] is True and probed[2]['available'] is False


def test_pick_next_candidate():
    state = {
        'current': 'https://a.example/1.m3u8',
        'candidates': ['https://a.example/1.m3u8', 'https://b.example/1.m3u8', 'https://c.example/1.m3u8'],
        'attempts': 0, 'tried': [],
    }
    assert mod.pick_next_candidate(state, state['current']) == 'https://b.example/1.m3u8'
    assert mod.pick_next_candidate(dict(state, attempts=3), state['current']) is None
    assert mod.pick_next_candidate(dict(state, tried=['https://b.example/1.m3u8']), state['current']) == 'https://c.example/1.m3u8'
    assert mod.pick_next_candidate(dict(state, candidates=[state['current']]), state['current']) is None


def test_write_playback_state():
    _VFS.pop(STATE_TEST_FILE, None)
    assert mod.write_playback_state({'current': 'https://x/1.m3u8', 'candidates': []}, path=STATE_TEST_FILE) is True
    assert json.loads(_VFS[STATE_TEST_FILE])['current'] == 'https://x/1.m3u8'


def test_service_pick_next_and_headers():
    state = {'candidates': ['https://a.example/1.m3u8', 'https://b.example/1.m3u8'], 'attempts': 0, 'tried': []}
    assert service_mod.pick_next(state, 'https://a.example/1.m3u8') == 'https://b.example/1.m3u8'
    assert service_mod.pick_next(dict(state, attempts=3), 'https://a.example/1.m3u8') is None
    headered = service_mod.with_headers('https://b.example/1.m3u8')
    assert headered.startswith('https://b.example/1.m3u8|') and 'User-Agent=' in headered and 'Referer=' in headered
    assert service_mod.with_headers('https://b.example/1.m3u8|Referer=x') == 'https://b.example/1.m3u8|Referer=x'


def test_bridge_encode_decode_id():
    token = bridge_mod.encode_id('s3', '161776')
    assert '&' not in token and '=' not in token
    assert bridge_mod.decode_id(token) == ('s3', '161776')
    assert bridge_mod.decode_id('bad') is None


def test_bridge_strip_jsonc_and_load_sources():
    text = '{\n // comment\n "sites": [\n  {"type": 1, "api": "https://a.example/vod/", "name": "A",},\n  {"type": 3, "api": "csp_XGuard", "name": "B",},\n ],\n}'
    parsed = json.loads(bridge_mod.strip_jsonc(text))
    assert len(parsed['sites']) == 2
    sources, stats = bridge_mod.load_sources(
        {'sources': [], 'fongmi_config': 'dummy'}, loader=lambda _p: parsed)
    assert len(sources) == 1 and sources[0]['url'] == 'https://a.example/vod/'
    assert stats['spider_skipped'] == 1 and stats['cms_from_fongmi'] == 1


def test_bridge_health_cooling():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'duola_bridge_health_test.json')
    if os.path.exists(path):
        os.remove(path)
    url = 'https://dead.example/api.php/provide/vod/'
    now = 3000000
    data = bridge_mod.record_failure(url, now=now, cooldown=100, path=path)
    assert bridge_mod.is_cooling(url, data, now + 10, cooldown=100, path=path) is True
    assert bridge_mod.is_cooling(url, data, now + 200, cooldown=100, path=path) is False
    os.remove(path)


# ---------------------------------------------------------------------------
# v2.1: Spider Host 运行时
# ---------------------------------------------------------------------------
spider_mod = _load_sibling('spider_mod', 'bridge/spider_host.py')


def test_spider_host_status_and_demo_contract():
    cfg = {'spider_host': {
        'enabled': True, 'timeout': 8, 'dex_sites': 36,
        'python': {'enabled': True, 'spiders': [
            {'name': 'Demo', 'file': 'spiders/demo.py', 'ext': '{}'}]},
        'js': {'spiders': []},
        'java': {'enabled': False},
    }}
    host = spider_mod.SpiderHost(cfg, base_dir='bridge')
    status = host.status()
    assert status['enabled'] is True and status['notes']['dex_skipped'] == 36
    assert any(p['kind'] == 'python' and p['available'] for p in status['providers'])
    found = host.search('demo')
    assert len(found) == 1 and found[0][0] == 'p0'
    detail = host.detail('p0', '1')
    assert detail and detail[0]['vod_play_from'] == 'demo'
    play = host.play('p0', 'demo', '1')
    assert play.get('url', '').endswith('.m3u8')
    assert host.self_test()['ok'] is True


def test_spider_host_disabled_is_noop():
    host = spider_mod.SpiderHost({'spider_host': {'enabled': False}}, base_dir='bridge')
    assert host.status()['enabled'] is False and host.providers == {}
    assert host.search('x') == []


def test_java_provider_command_shape():
    provider = spider_mod.JavaSpiderProvider('j0', 'Bili', 'Bili', '{}', '/tmp/cp', java_bin='java')
    cmd = provider.command('search', '小城良方')
    assert cmd[:3] == ['java', '-cp', '/tmp/cp']
    assert cmd[3:6] == ['SpiderHostRunner', 'search', 'Bili']
    assert cmd[-1] == '小城良方'


def test_js_provider_requires_runtime():
    provider = spider_mod.JsSpiderProvider('k0', 'js', 'bridge/spiders/_js_runner.js', '{}', node_bin=None)
    if not provider.available:
        assert 'node' in provider.last_error
    else:
        assert provider.command('home')[0] == provider.node_bin


def test_build_classpath_is_absolute():
    cp = spider_mod.build_classpath('bridge', jar_dir='jars')
    assert os.path.isabs(cp.split(os.pathsep)[0])


def test_spider_host_search_runs_providers_in_parallel():
    import threading
    barrier = threading.Barrier(2, timeout=2)

    class BarrierProvider(spider_mod.BaseProvider):
        kind = 'fake'

        def search(self, keyword):
            barrier.wait()  # 只在两个 provider 同时执行时才会通过
            return [{'vod_id': self.id, 'vod_name': self.id}]

    host = spider_mod.SpiderHost({'spider_host': {'enabled': False}}, base_dir='bridge')
    host.enabled = True
    host.providers = {'a': BarrierProvider('a', 'a'), 'b': BarrierProvider('b', 'b')}
    assert len(host.search('x', budget=3)) == 2


def test_is_local_bridge_budget():
    assert mod.is_local_bridge('http://127.0.0.1:8765/api.php/provide/vod/') is True
    assert mod.is_local_bridge('http://localhost:8765/vod/') is True
    assert mod.is_local_bridge('http://10.0.0.2:8765/vod/') is True
    assert mod.is_local_bridge('http://192.168.1.10:8765/vod/') is True
    assert mod.is_local_bridge('http://172.16.0.5/vod/') is True
    assert mod.is_local_bridge('http://172.32.0.5/vod/') is False
    assert mod.is_local_bridge('https://api.wujinapi.com/api.php/provide/vod/') is False
    assert mod.LOCAL_BRIDGE_TIMEOUT > mod.SEARCH_TIMEOUT
    assert mod.LOCAL_BRIDGE_BUDGET > mod.ENGINE_BUDGET


# ---------------------------------------------------------------------------
# v2.2: 播放地址补全
# ---------------------------------------------------------------------------
resolver_mod = _load_sibling('resolver_mod', 'bridge/play_resolver.py')


def test_resolver_split_and_rebuild_lines():
    video = {
        'vod_play_from': '线路A$$$线路B',
        'vod_play_url': '第1集$/vodplay/1.html#第2集$/vodplay/2.html$$$第1集$https://x/1.m3u8',
    }
    lines = resolver_mod.split_play_lines(video)
    assert len(lines) == 2 and len(lines[0]['episodes']) == 2
    flags, urls = resolver_mod.build_play_fields(lines)
    assert flags == '线路A$$$线路B' and '/vodplay/2.html' in urls


def test_resolver_needs_resolution_classification():
    # 带媒体后缀或无明显播放页标记的 http 地址保持原样（v1.9 已放行无后缀直链）
    direct = ['https://x/a.m3u8?token=1', 'https://x/a.mp4', 'https://x/stream/token']
    indirect = ['/vodplay/2872-1-1.html', 'https://x/share/abc', 'demo1',
                'https://x/vodplay/1.html', 'https://x/play/abc123']
    for url in direct:
        assert resolver_mod.needs_resolution(url) is False, url
    for url in indirect:
        assert resolver_mod.needs_resolution(url) is True, url
    assert resolver_mod.is_opaque_id('demo1') is True
    assert resolver_mod.is_opaque_id('/vodplay/1.html') is False


def test_resolver_relative_and_headers():
    assert resolver_mod.resolve_relative('/vodplay/1.html', 'https://www.zxzj.run/') == 'https://www.zxzj.run/vodplay/1.html'
    assert resolver_mod.resolve_relative('//cdn.x/a.m3u8', 'https://www.zxzj.run/') == 'https://cdn.x/a.m3u8'
    assert resolver_mod.resolve_relative('https://y/a.m3u8', '') == 'https://y/a.m3u8'
    assert resolver_mod.resolve_relative('demo1', '') == ''
    suffix = resolver_mod.headers_to_suffix({'Referer': 'https://www.zxzj.run/', 'User-Agent': 'UA x'})
    assert 'Referer=https%3A%2F%2Fwww.zxzj.run%2F' in suffix
    assert 'User-Agent=UA%20x' in suffix
    assert resolver_mod.attach_headers('https://x/a.m3u8|old=1', 'Referer=x') == 'https://x/a.m3u8|Referer=x'


def test_normalize_player_content_variants():
    plain = resolver_mod.normalize_player_content({'url': 'https://x/1.m3u8', 'parse': 0})
    assert plain['resolved'] and plain['url'] == 'https://x/1.m3u8'
    headered = resolver_mod.normalize_player_content(
        {'url': 'https://x/1.m3u8', 'header': {'Referer': 'https://site.x/'}})
    assert headered['url'].startswith('https://x/1.m3u8|Referer=')
    listed = resolver_mod.normalize_player_content({'url': ['https://x/1.m3u8', 'https://x/2.m3u8']})
    assert listed['url'] == 'https://x/1.m3u8'
    parse = resolver_mod.normalize_player_content({'url': 'https://x/1.m3u8', 'parse': 1})
    assert parse['resolved'] is False and parse['method'] == 'parser'
    parsed = resolver_mod.normalize_player_content(
        {'url': 'https://x/1.m3u8', 'jx': 1}, parse_url='https://p.x/?url=')
    assert parsed['url'] == 'https://p.x/?url=https://x/1.m3u8'
    assert resolver_mod.normalize_player_content({})['resolved'] is False


def test_extract_media_from_player_json_and_plain_html():
    page = ('<script>var player_aaaa={"flag":"play","encrypt":3,'
            '"vod_data":{"vod_name":"x"},"url":"https:\\/\\/ex.zxzja.com:9876\\/player-v2\\/common\\/'
            '1718579979\\/ABCDEF1234567890","from":"play"};</script>')
    assert resolver_mod.extract_media_from_html(page) == (
        'https://ex.zxzja.com:9876/player-v2/common/1718579979/ABCDEF1234567890')
    plain = '<video src="https://cdn.example.com/play/abc.m3u8?token=1"></video>'
    assert resolver_mod.extract_media_from_html(plain) == 'https://cdn.example.com/play/abc.m3u8?token=1'
    assert resolver_mod.extract_media_from_html('<a href="https://site.x/a.html">x</a>') == ''


def test_resolver_complete_via_player_and_page():
    class FakeHost:
        providers = {'p0': object()}

        def play(self, source_id, flag, play_id):
            return {'url': 'https://cdn.x/%s.m3u8' % play_id,
                    'header': {'Referer': 'https://site.x/'}}

    def fake_fetch(url, timeout, headers):
        return '<script>player_aaaa={"url":"https://media.x/play/1.m3u8"};</script>'

    resolver = resolver_mod.PlayResolver(
        {'max_workers': 4, 'budget': 3}, spider_host=FakeHost(), fetcher=fake_fetch)
    video = {
        'vod_play_from': 'player$$$page',
        'vod_play_url': '第1集$abc1$$$第1集$/vodplay/1.html#第2集$https://cdn.x/2.m3u8',
    }
    original_video = dict(video)
    summary = resolver.complete(video, 'p0', base='https://site.x/')
    assert summary['changed'] is True and summary['resolved'] == 2
    assert 'https://cdn.x/abc1.m3u8|Referer=' in video['vod_play_url']
    assert 'https://media.x/play/1.m3u8|' in video['vod_play_url']
    assert 'https://cdn.x/2.m3u8' in video['vod_play_url']
    assert resolver.stats['direct'] == 1
    # 同样的原始 id 再次解析应命中缓存
    again = resolver.complete(original_video, 'p0', base='https://site.x/')
    assert again['by_method'].get('cached', 0) >= 1


def test_resolver_leaves_direct_urls_untouched():
    resolver = resolver_mod.PlayResolver({'enabled': True}, spider_host=None,
                                         fetcher=lambda *a: (_ for _ in ()).throw(AssertionError('should not fetch')))
    video = {'vod_play_from': 'A', 'vod_play_url': '第1集$https://cdn.x/1.m3u8#第2集$https://cdn.x/2.mp4'}
    original = video['vod_play_url']
    summary = resolver.complete(video, 's0')
    assert summary['changed'] is False and video['vod_play_url'] == original


def test_resolver_failure_keeps_original():
    class EmptyHost:
        providers = {'p0': object()}

        def play(self, *a):
            return {}

    resolver = resolver_mod.PlayResolver({'budget': 2}, spider_host=EmptyHost(),
                                         fetcher=lambda *a: '')
    video = {'vod_play_from': 'A', 'vod_play_url': '第1集$abc1'}
    summary = resolver.complete(video, 'p0', base='https://site.x/')
    assert summary['resolved'] == 0 and video['vod_play_url'] == '第1集$abc1'
    assert resolver.stats['failed'] == 1


def test_provider_base_prefers_explicit_then_ext():
    class P:
        base_url = ''
        ext = '{"site":["https://wogg.net","https://wogg.live"]}'

    class Host:
        providers = {'a': P()}

    host = Host()
    assert resolver_mod.provider_base(host, 'a') == 'https://wogg.net'
    host.providers['a'].base_url = 'https://explicit.x/'
    assert resolver_mod.provider_base(host, 'a') == 'https://explicit.x/'
    assert resolver_mod.provider_base(host, 'missing', 'fallback') == 'fallback'


def test_bridge_do_detail_completes_spider_play_url():
    class FakeResolver:
        def complete(self, video, source_id, base=''):
            video['vod_play_url'] = video['vod_play_url'].replace('/vodplay/1.html', 'https://cdn.x/1.m3u8')
            return {'resolved': 1}

    class Host:
        providers = {'p0': object()}

        def detail(self, source_id, vod_id):
            return [{'vod_name': 'X', 'vod_play_from': 'A',
                     'vod_play_url': '第1集$/vodplay/1.html'}]

    token = bridge_mod.encode_id('p0', '1')
    old_host, old_resolver = bridge_mod.SPIDER_HOST, bridge_mod.PLAY_RESOLVER
    try:
        bridge_mod.SPIDER_HOST, bridge_mod.PLAY_RESOLVER = Host(), FakeResolver()
        payload = bridge_mod.do_detail(token)
    finally:
        bridge_mod.SPIDER_HOST, bridge_mod.PLAY_RESOLVER = old_host, old_resolver
    assert payload['code'] == 1 and payload['resolved'] == 1
    assert payload['list'][0]['vod_play_url'].endswith('https://cdn.x/1.m3u8')


def test_plugin_split_and_merge_stream_headers():
    clean, headers = mod.split_play_headers(
        'https://cdn.x/1.m3u8|Referer=https%3A%2F%2Fsite.x%2F&User-Agent=UA%20x')
    assert clean == 'https://cdn.x/1.m3u8'
    assert headers['Referer'] == 'https://site.x/' and headers['User-Agent'] == 'UA x'
    merged = mod.merge_stream_headers(clean, headers)
    assert 'Referer=https%3A%2F%2Fsite.x%2F' in merged
    assert 'User-Agent=UA%20x' in merged
    assert 'Origin=' in merged  # 默认头仍保留


def test_prepare_play_item_keeps_custom_headers_with_adaptive():
    item = RecordingListItem()
    url = mod.prepare_play_item(item, 'https://cdn.x/1.m3u8|Referer=https%3A%2F%2Fsite.x%2F',
                                use_adaptive=True, major=21)
    assert url == 'https://cdn.x/1.m3u8'
    assert 'Referer=https%3A%2F%2Fsite.x%2F' in item.properties['inputstream.adaptive.stream_headers']
    assert 'Origin=' in item.properties['inputstream.adaptive.stream_headers']


def test_probe_play_url_passes_custom_headers(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

    def fake_head(url=None, headers=None, timeout=None, allow_redirects=True):
        seen.update(url=url, headers=headers)
        return Resp()

    monkeypatch.setattr(mod.requests, 'head', fake_head)
    assert mod.probe_play_url('https://cdn.x/1.m3u8|Referer=https%3A%2F%2Fsite.x%2F') is True
    assert seen['url'] == 'https://cdn.x/1.m3u8'
    assert seen['headers']['Referer'] == 'https://site.x/'


# 无 pytest 环境也能直接执行：python tests_test_config.py
if __name__ == '__main__':
    class _MonkeyPatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo = []

    failed = []
    cases = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    for name, func in cases:
        patcher = _MonkeyPatch()
        try:
            func(patcher) if func.__code__.co_argcount else func()
            print('PASS ' + name)
        except Exception as exc:
            failed.append(name)
            print('FAIL ' + name + ': ' + repr(exc))
        finally:
            patcher.undo()
    print('%d passed, %d failed' % (len(cases) - len(failed), len(failed)))
    if failed:
        sys.exit(1)

