#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放地址补全（v2.2）：把详情里的间接播放 id / 播放页解析成 Kodi 可直接播放的 URL。

Spider 的 detailContent 常返回 `.html` 播放页或不含协议的字符串 id，Kodi 无法直接播。
本层提供两条解析路径：

- playerContent：调用 Spider 的二级取址契约（flag + id -> url/header/parse）；
- 播放页抓取：把相对地址补全为绝对 URL，抓页面里的 player_aaaa / player_data
  JSON 或媒体直链，并带上页面 Referer/UA 作为防盗链头。

解析结果写回 vod_play_url，插件侧无需感知；已是可播直链的分集完全不改写。
所有网络/子进程调用都有并发上限、单次超时与整体预算，并按 (源, 线路, id) 缓存。
"""
import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

MEDIA_EXTS = ('.m3u8', '.mpd', '.mp4', '.flv', '.ts', '.mkv', '.avi',
              '.mov', '.m4v', '.wmv', '.rmvb', '.webm', '.mp3', '.m4a', '.ogg')
PAGE_EXTS = ('.html', '.htm', '.php', '.asp', '.aspx', '.jsp', '.shtml', '.do')
IGNORE_EXTS = PAGE_EXTS + ('.js', '.css', '.json', '.png', '.jpg', '.jpeg',
                           '.gif', '.ico', '.svg', '.woff', '.ttf')
INDIRECT_MARKERS = ('/share/', '/vodplay/', '/video/', '/id/', '/play/')
PLAYER_ASSIGN_RE = re.compile(
    r'(?:player_aaaa|player_data|player_[A-Za-z0-9_]*|vod_data|config)\s*=\s*\{', re.I)
URL_IN_TEXT_RE = re.compile(r'https?://[^\s"\'<>()]+', re.I)


# ---------------------------------------------------------------------------
# 纯函数：结构拆分 / 判定 / 组装
# ---------------------------------------------------------------------------
def split_play_lines(video):
    """把 vod_play_from + vod_play_url 拆成 [{'name','episodes':[{'name','url'}]}]。"""
    flags = str(video.get('vod_play_from') or '').split('$$$')
    raw_lines = str(video.get('vod_play_url') or '').split('$$$')
    lines = []
    for index, raw in enumerate(raw_lines):
        episodes = []
        for piece in raw.split('#'):
            if '$' not in piece:
                continue
            name, url = piece.split('$', 1)
            name, url = name.strip(), url.strip()
            if not url:
                continue
            episodes.append({'name': name or '第%d集' % (len(episodes) + 1), 'url': url})
        if not episodes:
            continue
        flag = flags[index].strip() if index < len(flags) else ''
        lines.append({'name': flag or '线路%d' % (index + 1), 'episodes': episodes})
    return lines


def build_play_fields(lines):
    """把线路模型重新组装成 vod_play_from / vod_play_url。"""
    flags = [str(line.get('name') or '') for line in lines]
    urls = ['#'.join('%s$%s' % (ep.get('name') or '', ep.get('url') or '')
                     for ep in line.get('episodes') or []) for line in lines]
    return '$$$'.join(flags), '$$$'.join(urls)


def _media_path(url):
    clean = (url or '').split('|', 1)[0]
    try:
        return urllib.parse.urlparse(clean).path.lower()
    except Exception:
        return ''


def is_direct_play_url(url):
    """已是可播直链，无需补全。"""
    clean = (url or '').split('|', 1)[0].strip()
    if not clean:
        return False
    try:
        parsed = urllib.parse.urlparse(clean)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    return _media_path(clean).endswith(MEDIA_EXTS)


def needs_resolution(url):
    """只处理明显的播放页 / 间接 id，无后缀的 http 直链保持原样（v1.9 已放行）。"""
    clean = (url or '').split('|', 1)[0].strip()
    if not clean or is_direct_play_url(clean):
        return False
    if is_opaque_id(clean):
        return True  # 不含协议/路径的播放 id，只能走 playerContent
    try:
        parsed = urllib.parse.urlparse(clean)
    except Exception:
        return True
    path = _media_path(clean)
    if path.endswith(PAGE_EXTS):
        return True
    if not parsed.scheme or parsed.scheme not in ('http', 'https'):
        return True  # 相对路径或非 http 协议
    tail = path.rsplit('/', 1)[-1]
    if '.' not in tail and any(marker in path for marker in INDIRECT_MARKERS):
        return True
    return False


def is_opaque_id(url):
    """不含协议、不含路径分隔符的播放 id：只能走 playerContent。"""
    clean = (url or '').split('|', 1)[0].strip()
    return bool(clean) and '://' not in clean and '/' not in clean and '.' not in clean


def resolve_relative(url, base=''):
    """把 /vodplay/x.html 之类相对地址补全为绝对 URL。"""
    clean = (url or '').split('|', 1)[0].strip()
    if not clean:
        return ''
    if clean.startswith(('http://', 'https://')):
        return clean
    try:
        parsed = urllib.parse.urlparse(base or '')
        if not parsed.scheme or not parsed.netloc:
            return ''
        if clean.startswith('//'):
            return parsed.scheme + ':' + clean
        if clean.startswith('/'):
            return parsed.scheme + '://' + parsed.netloc + clean
        return urllib.parse.urljoin(parsed.scheme + '://' + parsed.netloc + '/', clean)
    except Exception:
        return ''


def headers_to_suffix(headers):
    """Spider 的 header dict -> Kodi |Key=Value&... 语法，值 URL 编码。"""
    if isinstance(headers, str):
        return headers.lstrip('|')
    if not isinstance(headers, dict):
        return ''
    pairs = []
    for key, value in headers.items():
        if not key or value is None:
            continue
        pairs.append(str(key) + '=' + urllib.parse.quote(str(value), safe=''))
    return '&'.join(pairs)


def attach_headers(url, suffix):
    clean = (url or '').split('|', 1)[0]
    return clean + '|' + suffix if suffix else clean


def page_headers(url):
    """抓取播放页时带的请求头，解析结果作为防盗链头回填给 Kodi。"""
    try:
        parsed = urllib.parse.urlparse(url)
        referer = parsed.scheme + '://' + parsed.netloc + '/'
    except Exception:
        referer = ''
    result = {'User-Agent': UA}
    if referer:
        result['Referer'] = referer
    return result


def normalize_player_content(payload, parse_url=''):
    """统一 playerContent 返回值 -> {'url', 'resolved', 'method', 'reason'}。"""
    if not isinstance(payload, dict):
        return {'url': '', 'resolved': False, 'method': 'player', 'reason': 'empty result'}
    raw_url = payload.get('url') or payload.get('data') or payload.get('playUrl') or ''
    if isinstance(raw_url, (list, tuple)):
        raw_url = raw_url[0] if raw_url else ''
    if isinstance(raw_url, dict):
        raw_url = raw_url.get('url', '')
    url = str(raw_url or '').strip()
    if not url:
        return {'url': '', 'resolved': False, 'method': 'player', 'reason': 'no url'}
    suffix = headers_to_suffix(payload.get('header') or payload.get('headers') or {})
    parse = payload.get('parse') or payload.get('jx') or 0
    if str(parse).lower() not in ('0', 'false', 'none', ''):
        if parse_url:
            url = str(parse_url) + url
            suffix = ''
        else:
            return {'url': '', 'resolved': False, 'method': 'parser',
                    'reason': 'needs external parse api'}
    return {'url': attach_headers(url, suffix), 'resolved': True,
            'method': 'player', 'reason': ''}


def _unescape(text):
    return (str(text or '').replace('\\/', '/')
            .replace('\\u0026', '&').replace('&amp;', '&')
            .replace('\\u003d', '=').replace('\\u003f', '?'))


def _balanced_json(text, start):
    depth, in_str, esc = 0, False, False
    limit = min(len(text), start + 200000)
    for i in range(start, limit):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ''


def _clean_media_url(value, loose=False):
    """loose=True 用于播放器 JSON 的 url 字段：允许无扩展名的 hex 播放串。"""
    text = _unescape(value).strip().rstrip('",;')
    if not text.startswith(('http://', 'https://')):
        return ''
    path = _media_path(text)
    if path.endswith(IGNORE_EXTS):
        return ''
    if path.endswith(MEDIA_EXTS) or '.m3u8' in path or '.mp4' in path:
        return text
    if loose:
        return text
    return ''


PLAYER_URL_KEYS = ('url', 'playUrl', 'playurl', 'm3u8', 'src', 'file')


def _candidate_from_obj(obj, depth=0, loose=False):
    if depth > 4:
        return ''
    if isinstance(obj, str):
        return _clean_media_url(obj, loose)
    if isinstance(obj, dict):
        for key in PLAYER_URL_KEYS + ('data', 'link'):
            if key in obj:
                got = _candidate_from_obj(obj[key], depth + 1,
                                          loose=loose or key in PLAYER_URL_KEYS)
                if got:
                    return got
        for value in obj.values():
            got = _candidate_from_obj(value, depth + 1, loose)
            if got:
                return got
    if isinstance(obj, (list, tuple)):
        for value in obj[:3]:
            got = _candidate_from_obj(value, depth + 1, loose)
            if got:
                return got
    return ''


def extract_media_from_html(html, page_url=''):
    """从播放页 HTML 提取真实媒体地址；纯函数，便于测试。"""
    text = str(html or '')
    if not text:
        return ''
    for match in PLAYER_ASSIGN_RE.finditer(text):
        start = text.find('{', match.start())
        if start < 0:
            continue
        block = _balanced_json(text, start)
        if not block:
            continue
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        got = _candidate_from_obj(obj)
        if got:
            return got
    for match in URL_IN_TEXT_RE.finditer(_unescape(text)):
        got = _clean_media_url(match.group(0))
        if got:
            return got
    for match in re.finditer(r'(?:atob|base64_decode)\s*\(\s*[\'"]([A-Za-z0-9+/=]{16,})[\'"]\s*\)', text):
        try:
            decoded = base64.b64decode(match.group(1)).decode('utf-8', 'ignore')
        except Exception:
            continue
        for found in URL_IN_TEXT_RE.finditer(_unescape(decoded)):
            got = _clean_media_url(found.group(0))
            if got:
                return got
    return ''


def fetch_page(url, timeout=3, headers=None):
    """默认抓取器；测试可注入替代实现。限制 512KB，避免大页面拖慢详情。"""
    req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(512 * 1024)
    return raw.decode('utf-8', 'ignore')


def provider_base(spider_host, source_id, fallback=''):
    """从 Spider 的 ext.site 或显式 base_url 推导播放页域名。"""
    provider = (getattr(spider_host, 'providers', None) or {}).get(source_id)
    if not provider:
        return fallback
    explicit = getattr(provider, 'base_url', '') or ''
    if explicit:
        return explicit
    try:
        ext = json.loads(getattr(provider, 'ext', '') or '{}')
    except (ValueError, TypeError):
        ext = {}
    sites = ext.get('site') or ext.get('siteUrl') or []
    if isinstance(sites, str):
        sites = [sites]
    for site in sites:
        if isinstance(site, str) and site.startswith(('http://', 'https://')):
            return site
    return fallback


# ---------------------------------------------------------------------------
# 解析器：并发 + 缓存 + 预算
# ---------------------------------------------------------------------------
class PlayResolver(object):
    def __init__(self, config=None, spider_host=None, fetcher=None, clock=None):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get('enabled', True))
        self.max_workers = max(1, int(cfg.get('max_workers', 8) or 8))
        self.max_episodes = max(1, int(cfg.get('max_episodes', 40) or 40))
        self.fetch_timeout = float(cfg.get('fetch_timeout', 3) or 3)
        self.budget = float(cfg.get('budget', 6) or 6)
        self.parse_url = cfg.get('parse_url') or ''
        self.cache_ttl = float(cfg.get('cache_ttl', 600) or 600)
        self.spider_host = spider_host
        self.fetcher = fetcher or fetch_page
        self.clock = clock or time.time
        self._cache = {}
        self._lock = threading.Lock()
        self.stats = {'total': 0, 'direct': 0, 'player': 0, 'page': 0,
                      'failed': 0, 'cached': 0, 'capped': 0}

    def _cached(self, key):
        with self._lock:
            item = self._cache.get(key)
        if item and item[0] > self.clock():
            return item[1]
        return None

    def _put_cache(self, key, value):
        with self._lock:
            self._cache[key] = (self.clock() + self.cache_ttl, value)

    def _via_player(self, source_id, flag, play_id):
        host = self.spider_host
        if not host or source_id not in (getattr(host, 'providers', None) or {}):
            return ''
        try:
            payload = host.play(source_id, flag, play_id)
        except Exception:
            return ''
        result = normalize_player_content(payload, self.parse_url)
        return result['url'] if result['resolved'] else ''

    def _via_page(self, play_id, base):
        target = resolve_relative(play_id, base)
        if not target:
            return ''
        try:
            headers = page_headers(target)
            html = self.fetcher(target, self.fetch_timeout, headers)
        except Exception:
            return ''
        media = extract_media_from_html(html, target)
        if not media:
            return ''
        return attach_headers(media, headers_to_suffix(headers))

    def resolve_one(self, source_id, flag, play_id, base=''):
        """返回 (最终 URL, 方法)。失败时原样返回，交给插件侧探测标记不可用。"""
        original = play_id or ''
        if not self.enabled or not needs_resolution(original):
            return original, 'direct'
        key = (source_id, flag, original)
        hit = self._cached(key)
        if hit:
            return hit, 'cached'
        final, method = '', 'failed'
        if is_opaque_id(original):
            final = self._via_player(source_id, flag, original)
            method = 'player' if final else 'failed'
            if not final:
                final = self._via_page(original, base)
                method = 'page' if final else 'failed'
        else:
            final = self._via_page(original, base)
            method = 'page' if final else 'failed'
            if not final and self.spider_host and source_id in (getattr(self.spider_host, 'providers', None) or {}):
                final = self._via_player(source_id, flag, original)
                method = 'player' if final else 'failed'
        if not final:
            return original, 'failed'
        self._put_cache(key, final)
        return final, method

    def complete(self, video, source_id, base=''):
        """就地改写 video 的播放字段；返回统计字典。"""
        summary = {'changed': False, 'episodes': 0, 'resolved': 0,
                   'by_method': {}, 'elapsed': 0.0}
        if not self.enabled or not isinstance(video, dict):
            return summary
        lines = split_play_lines(video)
        if not lines:
            return summary
        tasks = []
        for line_index, line in enumerate(lines):
            for ep_index, ep in enumerate(line['episodes']):
                self.stats['total'] += 1
                summary['episodes'] += 1
                if needs_resolution(ep['url']):
                    if len(tasks) < self.max_episodes:
                        tasks.append((line_index, ep_index, line['name'], ep['url']))
                    else:
                        self.stats['capped'] += 1
                else:
                    self.stats['direct'] += 1
        if not tasks:
            return summary

        started = self.clock()
        futures = {}
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks)))
        try:
            for line_index, ep_index, flag, url in tasks:
                fut = pool.submit(self.resolve_one, source_id, flag, url, base)
                futures[fut] = (line_index, ep_index, url)
            try:
                for future in as_completed(futures, timeout=self.budget):
                    line_index, ep_index, original = futures[future]
                    try:
                        final, method = future.result()
                    except Exception:
                        final, method = original, 'failed'
                    if final and final != original:
                        lines[line_index]['episodes'][ep_index]['url'] = final
                        summary['resolved'] += 1
                        summary['by_method'][method] = summary['by_method'].get(method, 0) + 1
                        if method in ('player', 'page', 'cached'):
                            self.stats[method] = self.stats.get(method, 0) + 1
            except Exception:
                pass  # 到整体预算，用已解析完成的部分结果
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self.stats['failed'] += len(tasks) - summary['resolved']
        summary['elapsed'] = round(self.clock() - started, 3)
        if summary['resolved']:
            flags, urls = build_play_fields(lines)
            video['vod_play_from'] = flags
            video['vod_play_url'] = urls
            summary['changed'] = True
        return summary

    def status(self):
        return {'enabled': self.enabled,
                'cache_entries': len(self._cache),
                'stats': dict(self.stats),
                'config': {'max_workers': self.max_workers, 'max_episodes': self.max_episodes,
                           'fetch_timeout': self.fetch_timeout, 'budget': self.budget,
                           'parse_url': self.parse_url}}
