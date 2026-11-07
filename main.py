# -*- coding:utf-8 -*-
import os
import re
import sys
import json
import base64
import datetime
import urllib.parse
import requests
import xbmc
import xbmcgui
import xbmcvfs
import xbmcaddon
import xbmcplugin
import threading
import queue
import time

# plugin info
ADDON_name = '哆啦搜索'
ADDON_object = xbmcaddon.Addon()
ADDON_handle = int(sys.argv[1])  # 当前插件句柄
ADDON_address = sys.argv[0]  # 当前插件地址
ADDON_parm = sys.argv[2]  # 问号以后的内容
ADDON_dialog = xbmcgui.Dialog()
ADDON_PlayerStyle = int(ADDON_object.getSetting('Duola_play_style'))
ADDON_PlayerMimes = ['.m3u8', '.mpd', '.mp4', '.flv', '.ts', '.mkv', '.avi', '.mov', '.ogg', '.mp3']
ADDON_NonVideoMimes = ['.html', '.htm', '.php', '.asp', '.aspx', '.jsp', '.json', '.xml', '.jpg', '.jpeg', '.png', '.gif', '.css', '.js', '.txt']
MIME_HLS = 'application/vnd.apple.mpegurl'
MIME_DASH = 'application/dash+xml'

# Get addon base path
ADDON_path = xbmcvfs.translatePath(ADDON_object.getAddonInfo('path'))
ICONS_dir = os.path.join(ADDON_path, 'resources', 'images', 'icons')
FANART_dir = os.path.join(ADDON_path, 'resources', 'images', 'fanart')
ADDON_TempDir = os.path.join(xbmcvfs.translatePath('special://home/temp'), ADDON_object.getAddonInfo('id'), '')
ADDON_CloudCacheTxt = os.path.join(ADDON_TempDir, 'Duola_Local_Search_Engine.txt')
# 健康缓存放在 addon_data（持久化），不放 temp：设置里的"清理缓存"会删掉整个 temp 目录
ADDON_DataDir = os.path.join(xbmcvfs.translatePath('special://profile/addon_data'), ADDON_object.getAddonInfo('id'), '')
ADDON_HealthFile = os.path.join(ADDON_DataDir, 'engine_health.json')
ADDON_PlaybackHealthFile = os.path.join(ADDON_DataDir, 'playback_health.json')
ADDON_PlaybackStateFile = os.path.join(ADDON_DataDir, 'playback_state.json')

ADDON_api = 'https://raw.githubusercontent.com/super-flyer/plugin.video.duolasousuo/refs/heads/main/v1.json'

# bot config
UA_timeout = 30          # 单接口搜索/详情/栏目沿用原有超时
CLOUD_TIMEOUT = 8        # 云端引擎列表拉取
UA_head = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

# v1.8 聚合搜索限时与健康缓存
SEARCH_TIMEOUT = 5       # 聚合搜索单请求超时
SEARCH_DEADLINE = 12     # 聚合搜索整体截止
ENGINE_BUDGET = 5        # 单个引擎最长占用时间
ENGINE_COOLDOWN = 1800   # 死源冷却 30 分钟
SEARCH_MAX_WORKERS = 8   # 并发上限，避免树莓派上线程膨胀
CONFIG_PROBE_TIMEOUT = 4 # 配置页探测超时
HEALTH_LOCK = threading.Lock()

# v2.0: 播放域名健康缓存 / 线路预检 / 失败自动换线路
PLAYBACK_COOLDOWN = 1800
PLAYBACK_PROBE_TIMEOUT = 2
PLAYBACK_MAX_SWITCH = 3
LOCAL_BRIDGE_TIMEOUT = 12   # 本地 bridge 聚合多源+播放补全，单请求超时放宽
LOCAL_BRIDGE_BUDGET = 12    # 本地 bridge 引擎预算

# global debug
print('duola_debug: [' + str(ADDON_handle) + ']' + ADDON_address + ' || ' + ADDON_parm)


# custom function
def check_json(input_str):
    try:
        json.loads(input_str)
        return True
    except:
        return False


def safe_get_json(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


# ---------------------------------------------------------------------------
# v1.8: engine health cache (addon_data/engine_health.json)
# ---------------------------------------------------------------------------
def is_remote_version_newer(remote, local):
    """仅当云端版本严格高于本地版本时才提示升级。

    本地版本领先于云端配置时（例如本地 1.8.0、云端仍为 1.7.0）不能误报"发现新版本"。
    """
    def parts(value):
        try:
            return tuple(int(p) for p in str(value).strip().split('.')[:3])
        except (ValueError, AttributeError):
            return (0,)

    left, right = parts(remote), parts(local)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def remaining_timeout(default_timeout, deadline=None):
    """把全局截止时间向下传递到单次网络请求。"""
    if deadline is None:
        return default_timeout
    left = deadline - time.time()
    if left <= 0:
        return 0
    return max(1, min(default_timeout, left))


def health_load(path=None):
    target = path or ADDON_HealthFile
    try:
        with xbmcvfs.File(target) as f:
            data = json.loads(f.read() or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def health_is_cooling(url, health=None, now=None):
    data = health if health is not None else health_load()
    if now is None:
        now = int(time.time())
    entry = data.get(normalize_url(url).rstrip('/'))
    if not isinstance(entry, dict):
        return False
    try:
        return float(entry.get('fail_until', 0)) > now
    except (TypeError, ValueError):
        return False


def classify_engine_health(stats):
    """区分「网络失败」与「正常返回但没搜到」，后者不能拉黑死源。"""
    if stats.get('json_ok'):
        return 'ok' if stats.get('list_count', 0) > 0 else 'empty'
    if stats.get('timed_out'):
        return 'timeout'
    return 'failed'


def health_update(url, status, path=None, now=None):
    """status: ok/empty 清除失败标记，failed 冷却，timeout 不动。"""
    if status not in ('ok', 'empty', 'failed'):
        return None
    target = path or ADDON_HealthFile
    if now is None:
        now = int(time.time())
    key = normalize_url(url).rstrip('/')
    with HEALTH_LOCK:
        data = health_load(target)
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        if status == 'failed':
            entry['fail_until'] = now + ENGINE_COOLDOWN
            entry['fails'] = int(entry.get('fails', 0)) + 1
            entry['last_fail'] = now
        else:
            entry.pop('fail_until', None)
            entry['last_ok'] = now
            entry['fails'] = 0
        data[key] = entry
        try:
            xbmcvfs.mkdirs(ADDON_DataDir)
            with xbmcvfs.File(target, 'w') as f:
                f.write(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            print('duola_debug:health_update failed=' + str(e))
            return None
    return data


def health_clear(path=None):
    """供设置里的"清理缓存"调用：清空死源冷却，让所有接口重新参与搜索。"""
    target = path or ADDON_HealthFile
    with HEALTH_LOCK:
        try:
            if xbmcvfs.exists(target):
                xbmcvfs.delete(target)
                return True
        except Exception as e:
            print('duola_debug:health_clear failed=' + str(e))
    return False


def normalize_url(url):
    if not url:
        return ''
    url = str(url).strip()
    if not url:
        return ''
    if '://' not in url:
        url = 'https://' + url
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme or 'https'
        netloc = parsed.netloc
        if netloc:
            hostname = parsed.hostname or ''
            port = parsed.port
            try:
                hostname_idna = hostname.encode('idna').decode('ascii') if hostname else ''
            except Exception:
                hostname_idna = hostname
            if hostname_idna:
                netloc = f'{hostname_idna}:{port}' if port else hostname_idna
        rebuilt = urllib.parse.urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        return rebuilt.rstrip('/')
    except Exception:
        return url.rstrip('/')


def build_request_url(base, suffix=''):
    if not suffix:
        return base
    if '?' not in base:
        return base + suffix
    if base.endswith('?') or base.endswith('&'):
        return base + suffix.lstrip('?')
    if base.endswith('=') and suffix.startswith('?wd='):
        return base + suffix[len('?wd='):]
    if base.endswith('?wd=') and suffix.startswith('?wd='):
        return base + suffix[len('?wd='):]
    return base + '&' + suffix.lstrip('?')


def url_media_path(url):
    """取 URL 的 path：忽略 ?query、#fragment 与 Kodi 的 |header 后缀。"""
    clean = (url or '').split('|', 1)[0]
    try:
        return urllib.parse.urlparse(clean).path.lower()
    except Exception:
        return clean.lower()


def check_url_mime(url):
    """v1.9: 按 path 判断，带 ?token= 也能识别；无后缀/间接播放 id 交给播放器。"""
    name = url_media_path(url).rsplit('/', 1)[-1]
    if any(name.endswith(ext) for ext in ADDON_PlayerMimes):
        return True
    if any(name.endswith(ext) for ext in ADDON_NonVideoMimes):
        return False
    return bool(name)


def get_url_media_type(url):
    path = url_media_path(url)
    if path.endswith('.m3u8'):
        return 'hls'
    if path.endswith('.mpd'):
        return 'dash'
    file_exts = tuple(ext for ext in ADDON_PlayerMimes if ext not in ('.m3u8', '.mpd'))
    if file_exts and path.endswith(file_exts):
        return 'file'
    return 'unknown'


def build_stream_headers(url):
    """统一构造 header 文本，值做 URL 编码，adaptive 与直链 | 语法共用。"""
    clean = (url or '').split('|', 1)[0]
    try:
        parsed = urllib.parse.urlparse(clean)
    except Exception:
        return ''
    if not parsed.netloc:
        return ''
    ua = UA_head.get('User-Agent', '')
    referer = parsed.scheme + '://' + parsed.netloc + '/'
    origin = parsed.scheme + '://' + parsed.netloc
    pairs = []
    if ua:
        pairs.append('User-Agent=' + urllib.parse.quote(ua, safe=''))
    pairs.append('Referer=' + urllib.parse.quote(referer, safe=''))
    pairs.append('Origin=' + urllib.parse.quote(origin, safe=''))
    return '&'.join(pairs)


def split_play_headers(url):
    """v2.2: 把 Kodi |Key=Value&... 语法拆成 (纯 URL, header dict)。"""
    original = url or ''
    clean, _, suffix = original.partition('|')
    headers = {}
    for pair in suffix.split('&'):
        if '=' not in pair:
            continue
        key, value = pair.split('=', 1)
        if key:
            headers[key] = urllib.parse.unquote(value)
    return clean, headers


def merge_stream_headers(clean, custom_headers):
    """v2.2: Spider 给的 header 覆盖默认的 UA/Referer/Origin，避免 adaptive 分支丢头。"""
    merged = {}
    for part in build_stream_headers(clean).split('&'):
        if '=' in part:
            key = part.split('=', 1)[0]
            merged[key.lower()] = part
    for key, value in (custom_headers or {}).items():
        merged[str(key).lower()] = str(key) + '=' + urllib.parse.quote(str(value), safe='')
    return '&'.join(merged.values())


def get_kodi_major():
    try:
        return int(str(xbmc.getInfoLabel('System.BuildVersion')).split('.')[0])
    except Exception:
        return 19


def detect_inputstream_adaptive(addon_factory=None):
    factory = addon_factory or xbmcaddon.Addon
    try:
        factory('inputstream.adaptive')
        return True
    except Exception:
        return False


def prepare_play_item(list_item, url, use_adaptive=None, major=None):
    """v1.9: 统一设置 MIME / inputstream.adaptive / header，返回最终播放 URL。

    HLS/DASH 且 adaptive 可用 -> manifest_type + stream_headers；
    否则回退直链，在 URL 后拼 |User-Agent=...&Referer=...&Origin=...
    """
    original = url or ''
    clean, custom_headers = split_play_headers(original)
    media = get_url_media_type(clean)
    header_text = merge_stream_headers(clean, custom_headers)
    if use_adaptive is None:
        use_adaptive = detect_inputstream_adaptive()
    if major is None:
        major = get_kodi_major()

    try:
        list_item.setContentLookup(False)
        if media in ('hls', 'dash') and use_adaptive:
            if major >= 19:
                list_item.setProperty('inputstream', 'inputstream.adaptive')
            else:
                list_item.setProperty('inputstreamaddon', 'inputstream.adaptive')
            list_item.setProperty('inputstream.adaptive.manifest_type', 'hls' if media == 'hls' else 'dash')
            if header_text:
                list_item.setProperty('inputstream.adaptive.stream_headers', header_text)
            list_item.setMimeType(MIME_HLS if media == 'hls' else MIME_DASH)
            return clean
        if media == 'hls':
            list_item.setMimeType(MIME_HLS)
        elif media == 'dash':
            list_item.setMimeType(MIME_DASH)
        # mp4/flv 等不再强制 HLS MIME
    except Exception as e:
        print('duola_debug:prepare_play_item fallback=' + str(e))

    return clean + '|' + header_text if header_text else clean


# ---------------------------------------------------------------------------
# v2.0: playback host health + line probing + state for auto switch
# ---------------------------------------------------------------------------
def is_local_bridge(url):
    """本机/内网适配服务：聚合多个上游，需要比普通 CMS 更长的超时预算。"""
    try:
        host = urllib.parse.urlparse((url or '').split('|', 1)[0]).hostname or ''
    except Exception:
        return False
    host = host.lower()
    if host in ('127.0.0.1', 'localhost', '::1'):
        return True
    if host.startswith(('10.', '192.168.')):
        return True
    if host.startswith('172.'):
        try:
            return 16 <= int(host.split('.')[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def play_host(url):
    try:
        return urllib.parse.urlparse((url or '').split('|', 1)[0]).netloc.lower()
    except Exception:
        return ''


def playback_health_load(path=None):
    target = path or ADDON_PlaybackHealthFile
    try:
        with xbmcvfs.File(target) as f:
            data = json.loads(f.read() or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def playback_is_cooling(url, health=None, now=None):
    data = health if health is not None else playback_health_load()
    if now is None:
        now = int(time.time())
    host = play_host(url)
    if not host:
        return False
    entry = data.get(host)
    if not isinstance(entry, dict):
        return False
    try:
        return float(entry.get('fail_until', 0)) > now
    except (TypeError, ValueError):
        return False


def playback_record_failure(url, path=None, now=None):
    target = path or ADDON_PlaybackHealthFile
    if now is None:
        now = int(time.time())
    host = play_host(url)
    if not host:
        return None
    with HEALTH_LOCK:
        data = playback_health_load(target)
        entry = data.get(host) if isinstance(data.get(host), dict) else {}
        entry['fail_until'] = now + PLAYBACK_COOLDOWN
        entry['fails'] = int(entry.get('fails', 0)) + 1
        entry['last_fail'] = now
        data[host] = entry
        try:
            xbmcvfs.mkdirs(ADDON_DataDir)
            with xbmcvfs.File(target, 'w') as f:
                f.write(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            print('duola_debug:playback_record_failure failed=' + str(e))
            return None
    return data


def playback_clear(path=None):
    target = path or ADDON_PlaybackHealthFile
    with HEALTH_LOCK:
        try:
            if xbmcvfs.exists(target):
                xbmcvfs.delete(target)
                return True
        except Exception:
            pass
    return False


def is_indirect_play_url(url):
    """无后缀且属于分享/播放页路径的地址，Kodi 无法直接播放。"""
    path = url_media_path(url)
    name = path.rsplit('/', 1)[-1]
    if '.' in name:
        return False
    return any(marker in path.lower() for marker in ('/share/', '/play/', '/v/', '/video/', '/id/'))


def probe_play_url(url, timeout=None):
    """轻量探测播放地址；返回 True 表示可尝试播放。"""
    if not url:
        return False
    timeout = timeout or PLAYBACK_PROBE_TIMEOUT
    try:
        head = getattr(requests, 'head', None)
        clean, custom_headers = split_play_headers(url)
        probe_headers = dict(UA_head)
        probe_headers.update(custom_headers)
        if head:
            res = head(clean, headers=probe_headers, timeout=timeout, allow_redirects=True)
            return res.status_code < 400 or res.status_code in (405, 501)
        res = requests.get(clean, headers=probe_headers, timeout=timeout)
        return res.status_code < 400
    except requests.exceptions.RequestException:
        return False
    except Exception:
        return True


def probe_play_lines(lines, health=None, now=None, probe_func=None):
    """并发探测每条线路首集；失败记录域名冷却并标记线路不可用。"""
    probe = probe_func or probe_play_url
    data = health if health is not None else playback_health_load()
    if now is None:
        now = int(time.time())
    results = [None] * len(lines)
    threads = []

    def worker(i, original):
        first = original['episodes'][0]['url'] if original.get('episodes') else ''
        indirect = bool(first) and is_indirect_play_url(first)
        available = bool(first) and not indirect and not playback_is_cooling(first, data, now)
        if available and not probe(first):
            playback_record_failure(first, now=now)
            available = False
        item = dict(original)
        item['available'] = available
        item['indirect'] = indirect
        item['host'] = play_host(first)
        results[i] = item

    for i, line in enumerate(lines):
        t = threading.Thread(target=worker, args=(i, line))
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=PLAYBACK_PROBE_TIMEOUT + 2)
    return [item if item is not None else dict(line, available=True, indirect=False, host='')
            for item, line in zip(results, lines)]


def write_playback_state(state, path=None):
    target = path or ADDON_PlaybackStateFile
    try:
        xbmcvfs.mkdirs(ADDON_DataDir)
        with xbmcvfs.File(target, 'w') as f:
            f.write(json.dumps(state, ensure_ascii=False))
        return True
    except Exception as e:
        print('duola_debug:write_playback_state failed=' + str(e))
        return False


def pick_next_candidate(state, current_url, max_switch=None):
    """从备用线路里挑一个不同域名、未尝试过的地址。"""
    limit = max_switch or PLAYBACK_MAX_SWITCH
    if state.get('attempts', 0) >= limit:
        return None
    current_host = play_host(current_url)
    tried = set(state.get('tried', []))
    for url in state.get('candidates', []):
        if not url or url == current_url or url in tried:
            continue
        if play_host(url) == current_host:
            continue
        return url
    return None


def parse_play_lines(video, health=None, now=None):
    """v2.0: 线路模型 + 播放域名健康标记 + 间接地址识别。"""
    flags = str(video.get('vod_play_from', '') or '').split('$$$')
    urls = str(video.get('vod_play_url', '') or '').split('$$$')
    lines = []
    for index, raw_line in enumerate(urls):
        episodes = []
        for raw_ep in raw_line.split('#'):
            if '$' not in raw_ep:
                continue
            name, ep_url = raw_ep.split('$', 1)
            name, ep_url = name.strip(), ep_url.strip()
            if not ep_url or not check_url_mime(ep_url):
                continue
            episodes.append({
                'name': name or '第%d集' % (len(episodes) + 1),
                'url': ep_url,
            })
        if not episodes:
            continue
        flag = flags[index].strip() if index < len(flags) else ''
        first = episodes[0]['url']
        lines.append({
            'name': flag or '线路%d' % (index + 1),
            'episodes': episodes,
            'host': play_host(first),
            'indirect': is_indirect_play_url(first),
            'available': not playback_is_cooling(first, health, now),
        })
    return lines


def remove_html_tags(html):
    dr = re.compile(r'<[^>]+>', re.S)
    text = dr.sub('', html)
    return text


def is_direct_vod_api(url):
    """True if url is already a direct Maccms/苹果CMS VOD API endpoint."""
    lower = (url or '').lower()
    return 'api.php/provide/vod' in lower or ('/provide/vod' in lower and '/at/' in lower)


def build_engine_candidates(base_url):
    candidates = []
    if not base_url:
        return candidates

    cleaned = base_url.strip()
    if not cleaned:
        return candidates

    if '://' not in cleaned:
        cleaned = 'https://' + cleaned

    # For direct API URLs, only return the canonical form and no-trailing-slash variant.
    # Avoids wasting time probing root domains and path-stripped variants.
    if is_direct_vod_api(cleaned):
        base = cleaned.rstrip('/')
        if cleaned.endswith('/'):
            candidates.append(cleaned.rstrip('/') + '/')
            candidates.append(cleaned.rstrip('/'))
        else:
            candidates.append(cleaned)
            candidates.append(cleaned + '/')
        return candidates

    parsed = urllib.parse.urlparse(cleaned)
    scheme = parsed.scheme or 'https'
    netloc = parsed.netloc or parsed.path
    path = parsed.path or ''

    if not netloc:
        return candidates

    path_suffixes = [
        '/tv', '/config', '/config.json', '/json', '/api', '/api.php', '/api.php/provide', '/api.php/provide/vod', '/api.php/provide/vod/'
    ]
    for suffix in path_suffixes:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break

    base_variants = []
    for variant_scheme in [scheme, 'https' if scheme == 'http' else 'http']:
        base_variants.append(variant_scheme + '://' + netloc)

    for root_url in base_variants:
        root_url = root_url.rstrip('/')
        if root_url not in candidates:
            candidates.append(root_url)
        if path not in ('', '/'):
            candidate = root_url + path.rstrip('/')
            if candidate not in candidates:
                candidates.append(candidate)
        for extra in [
            root_url + '/api.php/provide/vod/',
            root_url + '/api.php/provide/vod',
        ]:
            if extra not in candidates:
                candidates.append(extra)

    if cleaned.rstrip('/') not in candidates:
        candidates.append(cleaned.rstrip('/'))

    return candidates


def is_likely_api_candidate(url):
    if not url:
        return False
    lower = url.lower()
    if any(marker in lower for marker in [
        '/api.php',
        'provide/vod',
        '/json',
        '.json',
        '/tv',
        '/config',
        '/api',
        '/urls',
        '/sites',
    ]):
        return True
    return False


def extract_candidate_urls(text, base_url):
    candidates = []
    if not text:
        return candidates

    for match in re.finditer(r'data-(?:clipboard-text|api|url)=["\']([^"\']+)["\']', text, re.I):
        raw = match.group(1).strip()
        if not raw:
            continue
        candidate = urllib.parse.urljoin(base_url, raw) if base_url else raw
        if is_likely_api_candidate(candidate):
            candidates.append(candidate)

    for match in re.finditer(r'(?:href|src)=["\']([^"\']+)["\']', text, re.I):
        raw = match.group(1).strip()
        if not raw or raw.startswith(('javascript:', 'mailto:', 'tel:')):
            continue
        candidate = urllib.parse.urljoin(base_url, raw) if base_url else raw
        if is_likely_api_candidate(candidate):
            candidates.append(candidate)

    for match in re.finditer(r'https?://[^\s"\'<>]+', text, re.I):
        url = match.group(0).rstrip('),.;')
        if url.endswith('/'):
            url = url[:-1]
        if 'javascript:' in url.lower():
            continue
        if is_likely_api_candidate(url):
            candidates.append(url)

    if base_url and base_url not in candidates and is_likely_api_candidate(base_url):
        candidates.insert(0, base_url)

    seen = set()
    result = []
    for candidate in candidates:
        normalized = candidate.rstrip('/')
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def parse_fongmi_config(text):
    if not text:
        return None

    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if isinstance(data, dict):
        if isinstance(safe_get_json(data, 'sites', None), list):
            return data
        if isinstance(safe_get_json(data, 'urls', None), list):
            return data

    if isinstance(text, str):
        lowered = text.lower()
        if '"sites"' in lowered or '"urls"' in lowered or '"api"' in lowered or '"key"' in lowered:
            try:
                candidate = re.search(r'\{.*\}', text, re.S)
                if candidate:
                    parsed = json.loads(candidate.group(0))
                    if isinstance(parsed, dict):
                        return parsed
            except Exception:
                pass

    return None


def extract_text_api_candidates(text):
    candidates = []
    if not text:
        return candidates

    seen = set()
    for match in re.finditer(r'https?://[^\s"\'<>]+', text, re.I):
        candidate = match.group(0).rstrip('),.;')
        if 'javascript:' in candidate.lower():
            continue
        candidate = candidate.rstrip('/')
        if is_likely_api_candidate(candidate) and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def resolve_config_sites(base_url, deadline=None):
    """Probe only a fixed set of seed URLs for FongMi-style config JSON.
    Never recurse into discovered HTML links to avoid unbounded crawling.
    v1.8: deadline 由聚合搜索向下传递，探测次数与耗时都有上界。"""
    if not base_url:
        return []

    normalized_base = normalize_url(base_url)
    # Fixed seed candidates only — do NOT grow this list from page content
    seed_candidates = []
    for candidate in [normalized_base, normalized_base + '/api.php/provide/vod/', normalized_base + '/api.php/provide/vod']:
        if candidate not in seed_candidates:
            seed_candidates.append(candidate)

    # 聚合搜索收紧探测预算；单接口入口保持原有的 8 秒/5 个页面级配置 URL
    probe_timeout = CONFIG_PROBE_TIMEOUT if deadline is not None else min(8, UA_timeout)
    max_page_urls = 3 if deadline is not None else 5
    api_candidates_from_page = []  # collect page-level config URLs to try ONCE, never recurse

    for candidate in seed_candidates:
        if deadline is not None and time.time() >= deadline:
            break
        current_timeout = remaining_timeout(probe_timeout, deadline)
        if current_timeout <= 0:
            break
        print('duola_debug:resolve_config_sites probe=' + candidate)
        try:
            res = requests.get(url=candidate, headers=UA_head, timeout=current_timeout)
        except requests.exceptions.RequestException as e:
            print('duola_debug:resolve_config_sites probe failed=' + str(e))
            continue
        if not res.ok:
            continue
        text = res.text
        config = parse_fongmi_config(text)
        if config:
            sites = safe_get_json(config, 'sites', [])
            if isinstance(sites, list) and sites:
                print('duola_debug:resolve_config_sites found sites count=' + str(len(sites)))
                return sites
            urls = safe_get_json(config, 'urls', [])
            if isinstance(urls, list) and urls:
                # Collect child config URLs but do NOT recurse from them
                for item in urls:
                    if isinstance(item, dict):
                        child_url = safe_get_json(item, 'url', '')
                        if child_url and child_url not in api_candidates_from_page:
                            api_candidates_from_page.append(child_url)
            continue  # config page found but no sites; move on

        # Not a config page — look only for data-clipboard-text config links on the page
        for m in re.finditer(r'data-(?:clipboard-text|api|url)=["\']([^"\']+)["\']', text, re.I):
            raw = m.group(1).strip()
            if raw and is_likely_api_candidate(raw) and raw not in api_candidates_from_page:
                api_candidates_from_page.append(raw)

    # Try page-level config URLs exactly once, no deeper recursion
    for config_url in api_candidates_from_page[:max_page_urls]:
        if deadline is not None and time.time() >= deadline:
            break
        current_timeout = remaining_timeout(probe_timeout, deadline)
        if current_timeout <= 0:
            break
        config_url = normalize_url(config_url)
        print('duola_debug:resolve_config_sites try page config url=' + config_url)
        try:
            res2 = requests.get(url=config_url, headers=UA_head, timeout=current_timeout)
        except requests.exceptions.RequestException as e:
            print('duola_debug:resolve_config_sites page config failed=' + str(e))
            continue
        if not res2.ok:
            continue
        config2 = parse_fongmi_config(res2.text)
        if config2:
            sites2 = safe_get_json(config2, 'sites', [])
            if isinstance(sites2, list) and sites2:
                print('duola_debug:resolve_config_sites found sites from page config count=' + str(len(sites2)))
                return sites2

    return []


def build_site_api_candidates(site_entry):
    candidates = []
    if not isinstance(site_entry, dict):
        return candidates

    api_value = safe_get_json(site_entry, 'api', '')
    if api_value:
        candidates.append(normalize_url(api_value))
        if not api_value.startswith('http'):
            candidates.append('http://' + api_value)
            candidates.append('https://' + api_value)

    ext_value = safe_get_json(site_entry, 'ext', '')
    if ext_value and isinstance(ext_value, str):
        candidates.append(normalize_url(ext_value))

    return candidates


def build_source_label(source_name, resolved_url):
    if source_name:
        return source_name
    if not resolved_url:
        return '未知源'
    try:
        parsed = urllib.parse.urlparse(resolved_url)
        host = parsed.netloc or parsed.path or ''
        if host:
            return host.split(':')[0]
    except Exception:
        pass
    return '未知源'


# ---------------------------------------------------------------------------
# v1.8: 本机 + 云端 + 额外接口
# ---------------------------------------------------------------------------
def parse_extra_search_engines(raw):
    """解析设置项（多个用 | 分隔）。

    支持三种写法：
      https://x/api.php/provide/vod/
      名称=https://x/api.php/provide/vod/
      名称=direct:http://10.0.0.2:8080/vod/   # 自定义路径，跳过接口探测
    非 http(s) 项丢弃，按规范化后的 URL 去重。
    """
    engines = []
    seen = set()
    for part in (raw or '').split('|'):
        item = part.strip()
        if not item:
            continue
        name = ''
        first, sep, rest = item.partition('=')
        if sep and first.strip() and '/' not in first and ':' not in first:
            name = first.strip()
            item = rest.strip()
        if not item:
            continue
        direct = False
        if item.lower().startswith('direct:'):
            direct = True
            item = item[7:].strip()
        if not item.lower().startswith(('http://', 'https://')):
            continue
        url = item.strip()
        key = normalize_url(url).rstrip('/').lower()
        if not key or key in seen:
            continue
        seen.add(key)
        engines.append({
            'name': name or build_source_label('', url),
            'url': url,
            'direct': bool(direct or is_direct_vod_api(url)),
        })
    return engines


def build_search_engines(local_url, extra_raw, cloud_engines=None, cloud_enabled=False):
    """聚合搜索列表 = 本机接口 + 云端接口（可选） + 额外接口，按 URL 去重。"""
    engines = []
    seen = set()

    def add(name, url, direct=None):
        value = (url or '').strip()
        if not value.lower().startswith(('http://', 'https://')):
            return
        key = normalize_url(value).rstrip('/').lower()
        if not key or key in seen:
            return
        seen.add(key)
        engines.append({
            'name': name or build_source_label('', value),
            'url': value,
            'direct': bool(direct) if direct is not None else is_direct_vod_api(value),
        })

    add('本机接口', local_url)
    if cloud_enabled:
        for engine in cloud_engines or []:
            if isinstance(engine, dict):
                add(engine.get('name', ''), engine.get('url', ''))
    for engine in parse_extra_search_engines(extra_raw):
        add(engine['name'], engine['url'], engine['direct'])
    return engines


def filter_healthy_engines(engines, health=None, now=None):
    """跳过仍在冷却期内的死源。"""
    data = health if health is not None else health_load()
    if now is None:
        now = int(time.time())
    return [e for e in engines if not health_is_cooling(e.get('url', ''), data, now)]


def request_engine_json_candidates(base_url, suffix='', deadline=None, stats=None, direct=None):
    """请求 CMS 接口并返回所有可用候选结果。

    v1.8 新增可选参数：
      deadline - 绝对截止时间戳（聚合搜索专用）
      stats    - 由调用方传入的 dict，记录网络失败/有效 JSON/超时，用于健康缓存判定
      direct   - True 时按用户填写的地址直连，跳过候选探测（自定义路径用）
    不传 deadline 时保持原有行为（单接口搜索、详情、栏目不受影响）。
    """
    if stats is None:
        stats = {}
    stats.setdefault('checked', 0)
    stats.setdefault('network_failures', 0)
    stats.setdefault('json_ok', False)
    stats.setdefault('list_count', 0)
    stats.setdefault('timed_out', False)

    is_direct = is_direct_vod_api(base_url) if direct is None else bool(direct)
    if is_direct and deadline is not None:
        # 聚合搜索：直连接口只试填写的原始地址（保留尾斜杠），避免 2 个变体拖过截止时间。
        # 单接口搜索/详情/栏目不传 deadline，仍保留原有的两个变体兜底，行为不变。
        candidate_urls = [build_request_url((base_url or '').strip(), suffix)]
    else:
        candidate_urls = []
        for candidate in build_engine_candidates(base_url):
            candidate_urls.append(build_request_url(candidate, suffix))

    print('duola_debug:request_engine_json_candidates base_url=' + str(base_url) + ' suffix=' + str(suffix))
    print('duola_debug:candidate_urls initial count=' + str(len(candidate_urls)))

    # Only resolve config sites for non-API URLs (config/navigation pages).
    # Direct VOD API URLs have no config to resolve.
    if not is_direct:
        config_sites = resolve_config_sites(base_url, deadline=deadline)
        if config_sites:
            print('duola_debug:config_sites count=' + str(len(config_sites)))
            for site_entry in config_sites:
                for api_candidate in build_site_api_candidates(site_entry):
                    url_with_suffix = build_request_url(api_candidate, suffix)
                    if url_with_suffix not in candidate_urls:
                        candidate_urls.append(url_with_suffix)

    if deadline is not None and is_local_bridge(base_url):
        request_timeout = LOCAL_BRIDGE_TIMEOUT
    else:
        request_timeout = SEARCH_TIMEOUT if deadline is not None else UA_timeout
    seen = set()
    results = []
    for candidate in candidate_urls:
        if candidate in seen:
            continue
        seen.add(candidate)
        if deadline is not None and time.time() >= deadline:
            stats['timed_out'] = True
            break
        current_timeout = remaining_timeout(request_timeout, deadline)
        if current_timeout <= 0:
            stats['timed_out'] = True
            break
        print('duola_debug:try candidate=' + candidate)
        stats['checked'] += 1
        try:
            res = requests.get(url=candidate, headers=UA_head, timeout=current_timeout)
            res_text = res.text
            print('duola_debug:response candidate=' + candidate + ' status=' + str(getattr(res, 'status_code', '?')) + ' len=' + str(len(res_text)))
        except requests.exceptions.RequestException as e:
            stats['network_failures'] += 1
            print('duola_debug:request failed=' + str(e) + ' candidate=' + candidate)
            continue

        if not check_json(res_text):
            print('duola_debug:invalid json from candidate=' + candidate)
            continue

        try:
            res_json = json.loads(res_text)
        except ValueError:
            continue

        if isinstance(res_json, dict) and res_json.get('code') == 1:
            stats['json_ok'] = True
            stats['list_count'] += len(res_json.get('list', []) or [])
            results.append((candidate, res_text, res_json))

    return results


def request_engine_json(base_url, suffix=''):
    candidates = request_engine_json_candidates(base_url, suffix)
    if not candidates:
        return None, '', None
    return candidates[0]


def prompt_search_keyword():
    try:
        keyword = xbmcgui.Dialog().input('请输入关键词')
        if keyword is None:
            return ''
        return keyword.strip()
    except Exception:
        keyboard = xbmc.Keyboard('', '请输入关键词')
        keyboard.doModal()
        if keyboard.isConfirmed():
            return keyboard.getText().strip()
        return ''


# request: https://123.com/api/provide/vod/?wd={keyword}
# return:
def Web_fetch_search_results(url, keyword):
    resolved_url, _, res_json = request_engine_json(url, '?wd=' + keyword)
    if resolved_url is None or res_json is None:
        return None
    return res_json.get('list', [])


def Web_load_search(url, keyword, source_name=''):
    candidates = request_engine_json_candidates(url, '?wd=' + keyword)
    if not candidates:
        ADDON_dialog.notification(
            heading=ADDON_name,
            message='抱歉，目标服务器返回的数据无法响应，服务暂不可用',
            icon=xbmcgui.NOTIFICATION_WARNING,
            time=3000
        )
        return

    merged_results = []
    seen_results = set()
    for resolved_url, _, res_json in candidates:
        base_engine_url = resolved_url.split('?')[0].rstrip('/')
        for video in res_json.get('list', []):
            v_id = str(video.get('vod_id', ''))
            v_name = video.get('vod_name', '')
            v_type = video.get('type_name', '')
            v_remarks = video.get('vod_remarks', '')
            result_key = (str(v_name).lower(), str(v_type).lower(), str(v_remarks).lower(), str(v_id).lower())
            if result_key in seen_results:
                continue
            seen_results.add(result_key)
            merged_results.append({
                'video': video,
                'engine_url': base_engine_url,
                'source_name': source_name,
                'source_label': build_source_label(source_name, base_engine_url),
            })

    if len(merged_results) == 0:
        print('duola_debug:找不到资源')
        ADDON_dialog.notification(heading=ADDON_name, message='抱歉，找不到相关资源', time=3000)
        return

    for item in merged_results:
        video = item['video']
        v_id = str(video['vod_id'])
        v_name = video['vod_name']
        v_type = video['type_name']
        v_remarks = video['vod_remarks']
        display_name = v_name + ' (' + v_type + ')'
        if item.get('source_label'):
            display_name += ' [COLOR green][' + item['source_label'] + '][/COLOR]'
        list_item = xbmcgui.ListItem(display_name + ' [COLOR yellow]' + v_remarks + '[/COLOR]')
        list_item.setArt({'icon': os.path.join(ICONS_dir, 'video.png')})
        a_url = urllib.parse.quote(item['engine_url'])
        xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_search_return=' + a_url + '&read_detail=' + v_id, list_item, True)
    xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)


def Web_load_search_multi(engine_list, keyword, deadline_seconds=None):
    """v1.8: 固定并发 + 整体截止 + 死源冷却的聚合搜索。"""
    if not engine_list:
        ADDON_dialog.notification(heading=ADDON_name, message='当前没有可用的搜索接口', time=3000)
        return

    deadline = time.time() + (deadline_seconds if deadline_seconds is not None else SEARCH_DEADLINE)
    merged_results = []
    seen_results = set()
    result_lock = threading.Lock()
    task_queue = queue.Queue()
    for engine in engine_list:
        task_queue.put(engine)

    def note_health(engine_url, stats):
        try:
            status = classify_engine_health(stats)
            health_update(engine_url, status)
            print('duola_debug:health ' + status + ' -> ' + str(engine_url))
        except Exception as e:
            print('duola_debug:health skipped=' + str(e))

    def search_engine_worker(worker_id):
        while True:
            if time.time() >= deadline:
                return
            try:
                engine = task_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if not engine:
                    continue
                engine_url = engine.get('url', '')
                source_name = engine.get('name', '')
                budget = LOCAL_BRIDGE_BUDGET if is_local_bridge(engine_url) else ENGINE_BUDGET
                engine_deadline = min(deadline, time.time() + budget)
                stats = {}
                candidates = request_engine_json_candidates(
                    engine_url, '?wd=' + keyword,
                    deadline=engine_deadline,
                    stats=stats,
                    direct=engine.get('direct'),
                )
                note_health(engine_url, stats)
                if not candidates:
                    print('duola_debug: [Worker-' + str(worker_id) + '] ' + source_name + ' 无可用候选地址')
                    continue

                thread_results = []
                for resolved_url, _, res_json in candidates:
                    base_engine_url = resolved_url.split('?')[0].rstrip('/')
                    for video in res_json.get('list', []) or []:
                        v_id = str(video.get('vod_id', ''))
                        v_name = video.get('vod_name', '')
                        v_type = video.get('type_name', '')
                        v_remarks = video.get('vod_remarks', '')
                        result_key = (str(v_name).lower(), str(v_type).lower(), str(v_remarks).lower(), str(v_id).lower())
                        with result_lock:
                            if result_key not in seen_results:
                                seen_results.add(result_key)
                                thread_results.append({
                                    'video': video,
                                    'engine_url': base_engine_url,
                                    'source_name': source_name,
                                    'source_label': build_source_label(source_name, base_engine_url),
                                })
                with result_lock:
                    merged_results.extend(thread_results)
                if thread_results:
                    print('duola_debug: [Worker-' + str(worker_id) + '] ' + source_name + ' 返回 ' + str(len(thread_results)) + ' 条结果')
            except Exception as e:
                print('duola_debug: [Worker-' + str(worker_id) + '] 搜索异常: ' + str(e))
            finally:
                task_queue.task_done()

    worker_count = min(SEARCH_MAX_WORKERS, len(engine_list))
    workers = []
    for idx in range(worker_count):
        t = threading.Thread(target=search_engine_worker, args=(idx + 1,))
        t.daemon = True
        t.start()
        workers.append(t)

    # 总截止等待：到点即用已拿到的部分结果，不再逐线程等待 60 秒
    for t in workers:
        t.join(max(0, deadline - time.time()))

    with result_lock:
        snapshot = list(merged_results)

    if len(snapshot) == 0:
        ADDON_dialog.notification(heading=ADDON_name, message='抱歉，当前所有服务器都没有找到相关资源', time=3000)
        return

    # 添加所有结果到 Kodi 列表
    for item in snapshot:
        video = item['video']
        v_id = str(video['vod_id'])
        v_name = video['vod_name']
        v_type = video['type_name']
        v_remarks = video['vod_remarks']
        display_name = v_name + ' (' + v_type + ')'
        if item.get('source_label'):
            display_name += ' [COLOR green][' + item['source_label'] + '][/COLOR]'
        list_item = xbmcgui.ListItem(display_name + ' [COLOR yellow]' + v_remarks + '[/COLOR]')
        list_item.setArt({'icon': os.path.join(ICONS_dir, 'video.png')})
        a_url = urllib.parse.quote(item['engine_url'])
        xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_search_return=' + a_url + '&read_detail=' + v_id, list_item, True)

    xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)


# request: https://123.com/api/provide/vod/?ac=detail&ids={detail_id}
# return: play list
def Web_load_detail_one(url, detail_id):
    detail_candidates = request_engine_json_candidates(url, '?ac=detail&ids=' + detail_id)
    if not detail_candidates:
        ADDON_dialog.notification(heading=ADDON_name, message='内容获取失败，暂不可用', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
        return

    res_json = None
    for _, _, candidate_json in detail_candidates:
        if candidate_json and candidate_json.get('code') == 1 and candidate_json.get('list'):
            res_json = candidate_json
            break
    if res_json is None:
        ADDON_dialog.notification(heading=ADDON_name, message='内容获取失败，暂不可用', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
        return

    video = res_json['list'][0]
    v_name = str(video.get('vod_name', ''))
    v_typename = str(video.get('type_name', ''))
    v_picture = str(video.get('vod_pic', ''))
    v_infos = {}
    try:
        v_infos['title'] = video['vod_name']
        v_infos['originaltitle'] = video['vod_sub']
        v_infos['sorttitle'] = video['vod_en']
        v_infos['country'] = video['vod_area'].split(',')
        v_infos['year'] = video['vod_year']
        v_infos['director'] = video['vod_director']
        v_infos['writer'] = video['vod_writer'].split(',')
        v_infos['cast'] = video['vod_actor'].split(',')
        v_infos['plotoutline'] = video['vod_blurb']
        v_infos['plot'] = remove_html_tags(video['vod_content'])
        v_infos['rating'] = float(video['vod_douban_score'])
    except (IndexError, KeyError, ValueError, TypeError):
        pass

    lines = parse_play_lines(video, health=playback_health_load())
    lines = probe_play_lines(lines)
    if not any(line.get('available') for line in lines):
        ADDON_dialog.notification(heading=ADDON_name, message='该资源站线路当前不可用，建议返回选择其他源', time=4000)
    for line in lines:
        if not line.get('available'):
            line['name'] = line['name'] + ' [不可用]'
    usable = [line for line in lines if line.get('available') and not line.get('indirect')]
    if not usable:
        usable = [line for line in lines if line.get('available')]
    if not usable:
        usable = lines
    lines = usable
    if not lines:
        ADDON_dialog.notification(heading=ADDON_name, message='抱歉，找不到播放列表', time=3000)
        xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
        return

    # v1.9: 多线路先选线路，单线路自动跳过
    line_index = 0
    if len(lines) > 1:
        line_index = ADDON_dialog.select('选择播放源【' + v_name + '】', [line['name'] for line in lines])
        if line_index < 0:
            xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
            return
    line_name = lines[line_index]['name']
    episodes = lines[line_index]['episodes']
    select_title = '请选择剧集开始播放【' + v_name + ' / ' + line_name + '】'
    print('duola_debug: lines=' + str(len(lines)) + ' picked=' + line_name + ' episodes=' + str(len(episodes)) + ' style=' + str(ADDON_PlayerStyle))

    adaptive = detect_inputstream_adaptive()
    major = get_kodi_major()

    def make_item(label, icon=None):
        item = xbmcgui.ListItem(label, v_typename)
        art = {'thumb': v_picture, 'poster': v_picture}
        if icon:
            art['icon'] = icon
        item.setArt(art)
        item.setInfo('video', v_infos)
        return item

    def finish_directory():
        xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)

    # player_style 0：列出当前线路的所有剧集
    if ADDON_PlayerStyle == 0:
        for ep in episodes:
            item = make_item('[COLOR blue]【播放】[/COLOR]' + v_name + ' (' + ep['name'] + ')', os.path.join(ICONS_dir, 'play.png'))
            final_url = prepare_play_item(item, ep['url'], use_adaptive=adaptive, major=major)
            xbmcplugin.addDirectoryItem(ADDON_handle, final_url, item, False)
        finish_directory()
        return

    select_i = ADDON_dialog.select(select_title, [ep['name'] for ep in episodes])
    print('duola_debug: select_i ' + str(select_i))
    if select_i < 0:
        finish_directory()
        return

    # v2.0: 写备用线路状态，service.py 在播放失败时自动换线路
    write_playback_state({
        'current': episodes[select_i]['url'],
        'candidates': [line['episodes'][select_i]['url'] for line in lines if len(line['episodes']) > select_i],
        'attempts': 0,
        'tried': [],
        'written': int(time.time()),
    })

    # player_style 1：只播当前线路的选中集
    if ADDON_PlayerStyle == 1:
        item = make_item(v_name + ': ' + episodes[select_i]['name'])
        final_url = prepare_play_item(item, episodes[select_i]['url'], use_adaptive=adaptive, major=major)
        xbmc.Player().play(item=final_url, listitem=item)
        ADDON_dialog.notification(heading=ADDON_name, message='视频即将播放，请耐心稍候一会', time=6000, sound=False)
        finish_directory()
        return

    # player_style 2：播放列表只包含当前线路，连播不再跨线路错集
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    try:
        playlist.clear()
    except Exception:
        pass
    selected_item = None
    for index, ep in enumerate(episodes):
        item = make_item(v_name + ': ' + ep['name'])
        final_url = prepare_play_item(item, ep['url'], use_adaptive=adaptive, major=major)
        playlist.add(url=final_url, listitem=item, index=index)
        if index == select_i:
            selected_item = item
    xbmc.Player().play(item=playlist, listitem=selected_item, windowed=False, startpos=select_i)
    ADDON_dialog.notification(heading=ADDON_name, message='视频即将播放，请耐心稍候一会', time=6000, sound=False)
    finish_directory()


# request: https://123.com/api/provide/vod/?ac=list
# return: channels list
def Web_load_channels(url):
    resolved_url, _, res_json = request_engine_json(url, '?ac=list')
    if resolved_url is None or res_json is None:
        ADDON_dialog.notification(heading=ADDON_name, message='栏目获取失败，暂不可用', time=3000)
        return

    if res_json.get('code') == 1:
        if len(res_json.get('class', [])) > 0:
            for channel in res_json['class']:
                type_id = str(channel['type_id'])
                type_name = channel['type_name']
                list_item = xbmcgui.ListItem(type_name)
                a_url = urllib.parse.quote(resolved_url)
                xbmcplugin.addDirectoryItem(
                    ADDON_handle,
                    ADDON_address + '?Bot_channel=' + a_url + '&channel_id=' + type_id,
                    list_item,
                    True
                )
            xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
        else:
            print('duola_debug:暂无栏目')
            ADDON_dialog.notification(heading=ADDON_name, message='当前引擎暂无栏目', time=3000)
    else:
        print('duola_debug:栏目暂时无法获取')
        ADDON_dialog.notification(
            heading=ADDON_name,
            message='当前引擎栏目暂时无法获取',
            icon=xbmcgui.NOTIFICATION_WARNING,
            time=3000
        )


# request: https://123.com/api/provide/vod/?ac=videolist&t={type_id}&pg={page}
# return: list list
def Web_load_list(url, type_id, page):
    resolved_url, _, res_json = request_engine_json(url, '?ac=videolist&t=' + type_id + '&pg=' + page)
    if resolved_url is None or res_json is None:
        ADDON_dialog.notification(heading=ADDON_name, message='列表获取失败，暂不可用', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
        return

    if res_json.get('code') == 1:
        if len(res_json.get('list', [])) > 0:
            xbmcplugin.setContent(ADDON_handle, 'movies')  # 内容类型
            for video in res_json['list']:
                vod_id = str(video['vod_id'])
                vod_name = video['vod_name']
                vod_remarks = video['vod_remarks']
                vod_pic = video['vod_pic']
                v_infos = {}
                try:
                    v_infos['title'] = video['vod_name']
                    v_infos['originaltitle'] = video['vod_sub']
                    v_infos['sorttitle'] = video['vod_en']
                    v_infos['tag'] = video['vod_remarks']
                    v_infos['country'] = video['vod_area']
                    v_infos['year'] = video['vod_year']
                    v_infos['director'] = video['vod_director']
                    v_infos['writer'] = video['vod_writer'].split(",")
                    v_infos['cast'] = video['vod_actor'].split(',')
                    v_infos['plotoutline'] = video['vod_blurb']
                    v_infos['plot'] = remove_html_tags(video['vod_content'])
                    v_infos['rating'] = float(video['vod_score'])
                except IndexError as e:
                    pass

                list_item = xbmcgui.ListItem(vod_name + ' [COLOR=blue]' + vod_remarks + '[/COLOR]')
                list_item.setArt({'icon': os.path.join(ICONS_dir, 'video.png'), 'poster': vod_pic})
                list_item.setInfo('video', v_infos)
                a_url = urllib.parse.quote(resolved_url)
                xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_search_return=' + a_url + '&read_detail=' + vod_id, list_item, True)

            page = str(int(page) + 1)
            list_item = xbmcgui.ListItem('[COLOR yellow]下一页[/COLOR]([COLOR blue]' + str(res_json['page']) + '[/COLOR]/' + str(res_json['pagecount']) + ')')
            xbmcplugin.addDirectoryItem(
                ADDON_handle,
                ADDON_address + '?Bot_page=' + urllib.parse.quote(resolved_url) + '&channel_id=' + type_id + '&page_id=' + page,
                list_item,
                True
            )
            xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
        else:
            print('duola_debug:暂无列表')
            ADDON_dialog.notification(heading=ADDON_name, message='当前栏目下列表是空的，请稍后重试', time=3000)
    else:
        print('duola_debug:列表暂时无法获取')
        ADDON_dialog.notification(
            heading=ADDON_name,
            message='当前栏目下节目列表暂时无法获取',
            icon=xbmcgui.NOTIFICATION_WARNING,
            time=3000
        )


# API->engine get new
def API_get_Cloud_Engine_new(CloudCache_file):
    print('duola_debug: API_get_Cloud_Engine_new', ADDON_api)
    try:
        res = requests.get(url=ADDON_api, headers=UA_head, timeout=CLOUD_TIMEOUT)
        cloud_engine_text = res.text
        # 写入缓存，降低服务器请求数
        expires_in = 3600  # 初始有效时间为1小时
        if check_json(cloud_engine_text):
            api_json = json.loads(cloud_engine_text)
            notice = base64.b64decode(api_json.get('notice', '')).decode('utf-8', 'ignore')
            notice_title = ADDON_name
            # version format: x.x.x
            client = str(ADDON_object.getAddonInfo('version'))
            if is_remote_version_newer(api_json.get('client', ''), client):
                notice = '当前插件可以下载新版本，如果需要升级需卸载旧版本后才可以安装新版本'
                notice_title = '发现新版本: ' + str(api_json['client'])
            if notice:
                ADDON_dialog.notification(heading=notice_title, message=notice, time=4000, sound=False)
            if 'expires_in' in api_json:
                expires_in = float(api_json['expires_in'])  # 使用服务器限定的有效期
            next_time = datetime.datetime.now() + datetime.timedelta(seconds=expires_in)  # 设定时间有效期在n秒后失效
            next_timestamp = str(int(next_time.timestamp()))
            with xbmcvfs.File(CloudCache_file, 'w') as f:
                time_value = 'next_timestamp=' + next_timestamp  # 有效时间
                f.write(time_value)  # time
                f.write('\n--------\n')  # 此处分隔符
                f.write(cloud_engine_text)  # json
    except requests.exceptions.RequestException as e:
        cloud_engine_text = ''
        print('duola_debug: API_get_Cloud_Engine_new => BAD', e)
    return cloud_engine_text


# API->engine get
def API_get_Cloud_Engine_text():
    print('duola_debug: API_get_Cloud_Engine_text')
    xbmcvfs.mkdirs(ADDON_TempDir)
    if xbmcvfs.exists(ADDON_TempDir):
        print('duola_debug: Cache directory read successfully ->' + ADDON_TempDir)
        if xbmcvfs.exists(ADDON_CloudCacheTxt):
            cloud_engine_text = ""
            with xbmcvfs.File(ADDON_CloudCacheTxt) as f:
                cache = f.read()
                a = cache.split('--------')
                a101 = a[1]  # json text
                b = a[0].split('timestamp=')
                cc = b[1].replace('\n', '')  # next_timestamp=1640507115
                print(b, cc)
                next_timestamp = int(cc)
                this_timestamp = int(datetime.datetime.now().timestamp())
                print('this_time:' + str(this_timestamp) + ',next_time:' + str(next_timestamp), cloud_engine_text)
                if this_timestamp < next_timestamp:
                    print('duola_debug: effective cache, read local ->' + ADDON_CloudCacheTxt)
                    cloud_engine_text = a101
                else:
                    print('duola_debug: cache invalidation, download again')
                    cloud_engine_text = API_get_Cloud_Engine_new(ADDON_CloudCacheTxt)
        else:
            print('duola_debug: First cloud download of data')
            cloud_engine_text = API_get_Cloud_Engine_new(ADDON_CloudCacheTxt)
        return cloud_engine_text
    else:
        print('duola_debug: 缓存目录读取失败->' + ADDON_TempDir)
        ADDON_dialog.ok(ADDON_name, '抱歉，由于缓存无法读写导致服务不可用。目录：' + ADDON_TempDir)
        return ''


def API_get_Cloud_Engine_list():
    cloud_engine_text = API_get_Cloud_Engine_text()
    if not check_json(cloud_engine_text):
        return []

    api = json.loads(cloud_engine_text)
    if api.get('code') != 1:
        return []

    engines = []
    for zy in api.get('data', {}).get('list', []):
        if zy.get('status') != 1:
            continue
        try:
            decoded_url = base64.b64decode(zy['api_url']).decode('utf-8', 'ignore')
        except Exception:
            continue
        engines.append({
            'name': zy.get('name', '未知云引擎'),
            'url': decoded_url,
        })
    return engines


def API_get_Cloud_Engine():
    print('duola_debug: API_get_Cloud_Engine')
    cloud_engine_text = API_get_Cloud_Engine_text()
    if not cloud_engine_text:
        return

    # ----- 解析json ------
    if check_json(cloud_engine_text):
        api = json.loads(cloud_engine_text)
        if api.get('code') == 1 and len(api.get('data', {}).get('list', [])) > 0:
            print('duola_debug:zy YES')
            for zy in api['data']['list']:
                if zy['status'] == 1:
                    _api_url = urllib.parse.quote(base64.b64decode(zy['api_url']).decode('utf-8', 'ignore'))
                    _api_title = ' [COLOR blue] (' + zy['name'] + ') [/COLOR]'
                    item_cloud = xbmcgui.ListItem(ADDON_name + _api_title)
                    item_cloud.setArt({'icon': os.path.join(ICONS_dir, 's2.png')})
                    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_engine=' + _api_url, item_cloud, True)
                else:
                    _api_title = ' [COLOR yellow] (' + zy['name'] + ') ' + ' 暂不可用[/COLOR]'
                    item_cloud = xbmcgui.ListItem(ADDON_name + _api_title)
                    item_cloud.setArt({'icon': os.path.join(ICONS_dir, 's0.png')})
                    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address, item_cloud, True)
        else:
            ADDON_dialog.notification(
                heading=ADDON_name,
                message='云端搜索引擎暂时故障,请稍后重试' + api.get('message', ''),
                icon=xbmcgui.NOTIFICATION_WARNING,
                time=3000
            )
    else:
        ADDON_dialog.notification(
            heading=ADDON_name,
            message='暂时无法获取云端搜索引擎列表,请稍后重试',
            icon=xbmcgui.NOTIFICATION_WARNING,
            time=3000
        )
    # ----- 解析json ------


# /
if ADDON_parm == '':
    print('duola_debug: load main')
    enable_cloud = ADDON_object.getSetting('Duola_Cloud_Search_Engine')
    _b = ""
    item_engine = xbmcgui.ListItem('[COLOR green]聚合搜索(全部服务器)[/COLOR]')
    item_engine.setArt({'icon': os.path.join(ICONS_dir, 'search.png')})
    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_search_all', item_engine, True)
    # add cloud menu
    if enable_cloud == 'true':
        _b = ' (本机内置接口)'
        API_get_Cloud_Engine()
    # add local menu
    _local_api_url = ADDON_object.getSetting('Duola_Local_Search_Engine')
    api_url = urllib.parse.quote(_local_api_url)
    item_engine = xbmcgui.ListItem(ADDON_name + _b)
    item_engine.setArt({'icon': os.path.join(ICONS_dir, 's1.png')})
    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_engine=' + api_url, item_engine, True)
    # add help menu
    item_engine = xbmcgui.ListItem('[COLOR=yellow]帮助&声明[/COLOR]')
    item_engine.setArt({'icon': os.path.join(ICONS_dir, 'doc.png')})
    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_help', item_engine, True)
    # exit menu build
    xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)

# /?Bot_help
if '?Bot_help' in ADDON_parm:
    help_text = '1, 搜索中文关键词时, 请确保已安装Kodi中文输入法\n'
    help_text += '2, 插件内置搜索接口允许自定义，可通过插件设置修改\n'
    help_text += '3, 插件内置云搜索引擎，可通过设置开启云端搜索列表\n'
    help_text += '4, 部分资源首次播放缓冲较慢，耐心等待后就会流畅\n'
    help_text += '5, 设置中可添加额外搜索接口，多个用 | 分隔；支持 名称=地址，自定义路径请用 名称=direct:地址\n'
    help_text += '\n插件声明：\n本插件是基于学习和研究目的通过爬虫技术所抓取的所有内容均来自于第三方网站的数据，插件不涉及资源存储或分发。'
    help_text += '作者不对其内容承担任何责任！仅供个人学习交流之用，24小时内请自觉卸载，勿作商业用途。'
    ADDON_dialog.ok(ADDON_name + ' 帮助&声明', help_text)
    # 纯弹窗分支也要结束目录，否则 Kodi 会记 GetDirectory 失败并退回上级
    xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=False)

# /?Bot_engine=https%3A%2F%2F123.com%2Fapi%2Fprovide%2Fvod%2F :: 云引擎
if '?Bot_engine=' in ADDON_parm:
    _parm_url = urllib.parse.unquote(ADDON_parm)
    engine_url = _parm_url.split("Bot_engine=")[1]
    item_menu = xbmcgui.ListItem('[COLOR=yellow]在线搜索[/COLOR]')
    item_menu.setArt({'icon': os.path.join(ICONS_dir, 'search.png')})
    xbmcplugin.addDirectoryItem(ADDON_handle, ADDON_address + '?Bot_search=' + engine_url, item_menu, True)
    Web_load_channels(engine_url)
    xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)

# /?Bot_search_all
if '?Bot_search_all' in ADDON_parm:
    keyword = prompt_search_keyword()
    if len(keyword) < 1:
        ADDON_dialog.ok(ADDON_name, '您必须输入关键词才可以搜索相关内容')
    else:
        print('duola_debug:search keyword=' + keyword)
        # v1.8: 本机接口 + 云端接口（可选） + 额外接口，云端开关只控制云端列表
        cloud_enabled = ADDON_object.getSetting('Duola_Cloud_Search_Engine') == 'true'
        cloud_engines = API_get_Cloud_Engine_list() if cloud_enabled else []
        search_engines = build_search_engines(
            ADDON_object.getSetting('Duola_Local_Search_Engine'),
            ADDON_object.getSetting('Duola_Extra_Search_Engines'),
            cloud_engines,
            cloud_enabled,
        )
        print('duola_debug:aggregate engines total=' + str(len(search_engines)))
        active_engines = filter_healthy_engines(search_engines)
        print('duola_debug:aggregate engines active=' + str(len(active_engines)))
        if active_engines:
            Web_load_search_multi(active_engines, keyword)
        elif search_engines:
            ADDON_dialog.notification(heading=ADDON_name, message='所有搜索接口都在冷却中，请稍后重试或在设置中清理缓存', time=4000)
        else:
            ADDON_dialog.notification(heading=ADDON_name, message='当前没有可用的搜索接口，请在设置中检查接口配置', time=4000)

# /?Bot_search=https%3A%2F%2F123.com%2Fapi%2Fprovide%2Fvod%2F
if '?Bot_search=' in ADDON_parm:
    _parm_url = urllib.parse.unquote(ADDON_parm)
    engine_url = _parm_url.split("Bot_search=")[1]
    keyword = prompt_search_keyword()
    if len(keyword) < 1:
        ADDON_dialog.ok(ADDON_name, '您必须输入关键词才可以搜索相关内容')
    else:
        print('duola_debug:search keyword=' + keyword)
        Web_load_search(engine_url, keyword)

# /?Bot_channel=https%3A%2F%2F123.com%2Fapi%2Fprovide%2Fvod%2F&channel_id=123
if '?Bot_channel=' in ADDON_parm and '&channel_id=' in ADDON_parm:
    _parm_url = urllib.parse.unquote(ADDON_parm)
    channel_id = _parm_url.split("&channel_id=")[1]
    engine_url = _parm_url.replace('&channel_id=' + channel_id, '').split("Bot_channel=")[1]
    print('duola_debug:', engine_url, channel_id)
    if channel_id != "":
        Web_load_list(engine_url, channel_id, '1')
    else:
        print('duola_debug:传入的 channel_id 地址为空')
        ADDON_dialog.notification(heading=ADDON_name, message='此栏目无效', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)

# /?Bot_page=https%3A%2F%2F123.com%2Fapi%2Fprovide%2Fvod%2F&channel_id=123&page_id=2
if '?Bot_page=' in ADDON_parm and '&channel_id=' in ADDON_parm and '&page_id=' in ADDON_parm:
    _parm_url = urllib.parse.unquote(ADDON_parm)
    page_number = _parm_url.split("&page_id=")[1]
    channel_id = _parm_url.replace('&page_id=' + page_number, '').split("&channel_id=")[1]
    engine_url = _parm_url.replace('&channel_id=' + channel_id + '&page_id=' + page_number, '').split("Bot_page=")[1]
    if channel_id != "":
        Web_load_list(engine_url, channel_id, page_number)
    else:
        print('duola_debug:传入的 channel_id 地址为空')
        ADDON_dialog.notification(heading=ADDON_name, message='此栏目无效', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)

# /?Bot_search_return=https%3A%2F%2F123.com%2Fapi%2Fprovide%2Fvod%2F&read_detail=123
if '?Bot_search_return=' in ADDON_parm and '&read_detail' in ADDON_parm:
    _parm_url = urllib.parse.unquote(ADDON_parm)
    detail_id = _parm_url.split("&read_detail=")[1]
    engine_url = _parm_url.replace('&read_detail=' + detail_id, '').split("Bot_search_return=")[1]
    print('duola_debug', engine_url, detail_id)
    if detail_id != "":
        Web_load_detail_one(engine_url, detail_id)
    else:
        print('duola_debug:传入的 read_detail 地址为空')
        ADDON_dialog.notification(heading=ADDON_name, message='此视频无效', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)


# /?action=clearCache
if '?action=clearCache' in ADDON_parm:
    print("duola_debug", ADDON_parm)
    # v1.8: 同时清空死源冷却，让所有接口重新参与聚合搜索
    health_clear()
    playback_clear()
    if xbmcvfs.exists(ADDON_TempDir):
        if xbmcvfs.rmdir(ADDON_TempDir, True):
            ADDON_dialog.notification(heading=ADDON_name, message="清理成功", time=3000)
        else:
            ADDON_dialog.notification(heading=ADDON_name, message="清理失败", icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
    else:
        ADDON_dialog.notification(heading=ADDON_name, message="无需清理", time=3000)

