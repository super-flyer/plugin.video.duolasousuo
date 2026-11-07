import importlib.util
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
sys.modules['requests'] = requests

xbmc = types.ModuleType('xbmc')
xbmc.PLAYLIST_VIDEO = 1
xbmc.getInfoLabel = lambda *a, **k: '21.3'
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

