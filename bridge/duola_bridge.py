#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""duola-bridge: FongMi/TVBox 配置 -> MACCMS JSON 适配服务（v2.0）。

在家里的服务器（与 Kodi 同机）只监听 127.0.0.1，把多个 CMS 资源站聚合成
标准 /api.php/provide/vod/ 接口，Kodi 插件的「额外搜索接口」直接填它即可。

- 支持读取 FongMi/TVBox 配置（URL 或本地文件，允许 jsonc 注释），把其中
  type 0/1 的 HTTP CMS 站点自动纳入；type 3 的 csp_*/js/py 需要 Spider Host，
  这里只统计并跳过，不假装能执行。
- 搜索并发聚合 + 去重 + 上游死源冷却；详情按编码后的 vod_id 回源。
- 不存储、不转发任何媒体内容，只做 JSON 转换。
"""
import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spider_host as sh

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

CFG = {}
SOURCES = []
STATS = {}
SPIDER_HOST = None
HEALTH_LOCK = threading.Lock()
HEALTH_PATH = os.path.expanduser('~/.local/share/duola-bridge/health.json')


def strip_jsonc(text):
    """去掉 // 与 /* */ 注释以及尾随逗号，兼容 TVBox 的 jsonc 配置。"""
    out, i, n = [], 0, len(text)
    in_str, quote = False, ''
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str, quote = True, c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n:
            if text[i + 1] == '/':
                j = text.find('\n', i)
                i = n if j < 0 else j
                continue
            if text[i + 1] == '*':
                j = text.find('*/', i + 2)
                i = n if j < 0 else j + 2
                continue
        out.append(c)
        i += 1
    return re.sub(r',\s*([}\]])', r'\1', ''.join(out))


def load_jsonc(location, timeout=8):
    if location.startswith('http://') or location.startswith('https://'):
        req = urllib.request.Request(location, headers={'User-Agent': UA, 'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8-sig', 'ignore')
    else:
        with open(location, encoding='utf-8-sig') as f:
            text = f.read()
    return json.loads(strip_jsonc(text))


def encode_id(source_id, vod_id):
    raw = (str(source_id) + '|' + str(vod_id)).encode('utf-8')
    return 'b' + base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_id(token):
    if not token or not token.startswith('b'):
        return None
    try:
        body = token[1:]
        raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)).decode('utf-8')
        source_id, vod_id = raw.split('|', 1)
        return source_id, vod_id
    except Exception:
        return None


def is_spider_site(site):
    api = str(site.get('api', '') or '').lower()
    if str(site.get('type', '')) == '3':
        return True
    return api.startswith('csp_') or api.endswith('.js') or api.endswith('.py')


def load_sources(config, loader=None):
    """返回 (规范化后的源列表, 统计信息)。loader 可注入以便测试。"""
    loader = loader or load_jsonc
    raw = [dict(item) for item in (config.get('sources') or [])]
    stats = {'configured': len(raw), 'fongmi_sites': 0, 'cms_from_fongmi': 0, 'spider_skipped': 0}

    location = config.get('fongmi_config') or ''
    if location:
        try:
            data = loader(location) or {}
            sites = data.get('sites') or []
            stats['fongmi_sites'] = len(sites)
            for site in sites:
                if not isinstance(site, dict):
                    continue
                if is_spider_site(site):
                    stats['spider_skipped'] += 1
                    continue
                api = str(site.get('api', '') or '')
                if not api.startswith('http'):
                    continue
                raw.append({'name': site.get('name') or site.get('key') or 'FongMi源', 'url': api})
                stats['cms_from_fongmi'] += 1
        except Exception as e:
            stats['fongmi_error'] = str(e)

    sources, seen = [], set()
    for index, item in enumerate(raw):
        url = str(item.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')):
            continue
        key = url.rstrip('/').lower()
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            'id': 's%d' % index,
            'name': str(item.get('name') or urllib.parse.urlparse(url).netloc),
            'url': url,
        })
    stats['active'] = len(sources)
    return sources, stats


def build_query(base, params):
    query = urllib.parse.urlencode(params)
    return base + ('&' if '?' in base else '?') + query


def fetch_json(url, timeout=6):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json, */*'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8-sig', 'ignore'))


def health_load(path=None):
    target = path or HEALTH_PATH
    try:
        with open(target, encoding='utf-8') as f:
            data = json.loads(f.read() or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_cooling(url, health=None, now=None, cooldown=1800, path=None):
    data = health if health is not None else health_load(path)
    now = now or int(time.time())
    entry = data.get(url.rstrip('/'))
    if not isinstance(entry, dict):
        return False
    try:
        return float(entry.get('fail_until', 0)) > now
    except (TypeError, ValueError):
        return False


def record_failure(url, now=None, cooldown=1800, path=None):
    target = path or HEALTH_PATH
    now = now or int(time.time())
    key = url.rstrip('/')
    with HEALTH_LOCK:
        data = health_load(target)
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        entry['fail_until'] = now + cooldown
        entry['fails'] = int(entry.get('fails', 0)) + 1
        data[key] = entry
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
    return data


def search_source(source, keyword, timeout=6):
    data = fetch_json(build_query(source['url'], {'wd': keyword}), timeout)
    return data.get('list') or []


def do_search(keyword):
    deadline = time.time() + float(CFG.get('deadline', 10))
    timeout = float(CFG.get('timeout', 6))
    cooldown = int(CFG.get('cooldown', 1800))
    health = health_load()
    active = [s for s in SOURCES if not is_cooling(s['url'], health, cooldown=cooldown)]
    results, seen = [], set()
    pool = ThreadPoolExecutor(max_workers=int(CFG.get('max_workers', 8)))
    futures = {pool.submit(search_source, s, keyword, timeout): s for s in active}
    # Spider Host 与 CMS 源并行，避免串行叠加把插件的本地请求拖超时
    spider_future = None
    if SPIDER_HOST and SPIDER_HOST.enabled:
        spider_future = pool.submit(SPIDER_HOST.search, keyword, float(CFG.get('spider_budget', 8)))
        futures[spider_future] = None
    wait_timeout = max(1, deadline - time.time())
    if spider_future:
        wait_timeout = max(wait_timeout, float(CFG.get('spider_budget', 8)))
    try:
        for future in as_completed(futures, timeout=wait_timeout):
            source = futures[future]
            if source is None:
                try:
                    spider_items = future.result()
                except Exception as e:
                    print('[bridge] spider search failed:', type(e).__name__, flush=True)
                    continue
                for provider_id, provider_name, item in spider_items:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get('vod_name') or '')
                    type_name = str(item.get('type_name') or '')
                    key = (name.lower(), type_name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out = dict(item)
                    out['vod_id'] = encode_id(provider_id, item.get('vod_id', ''))
                    out.setdefault('type_name', '')
                    out['vod_remarks'] = '[' + provider_name + '] ' + str(item.get('vod_remarks') or '')
                    results.append(out)
                continue
            try:
                items = future.result()
            except Exception as e:
                print('[bridge] source failed:', source['name'], type(e).__name__, flush=True)
                record_failure(source['url'], cooldown=cooldown)
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('vod_name') or '')
                type_name = str(item.get('type_name') or '')
                key = (name.lower(), type_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                out = dict(item)
                out['vod_id'] = encode_id(source['id'], item.get('vod_id', ''))
                out.setdefault('type_name', '')
                out['vod_remarks'] = '[' + source['name'] + '] ' + str(item.get('vod_remarks') or '')
                results.append(out)
    except Exception:
        pass  # 到整体截止时间，用已拿到的部分结果
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {
        'code': 1, 'msg': 'duola-bridge', 'page': 1, 'pagecount': 1,
        'limit': len(results), 'total': len(results), 'list': results,
    }


def do_detail(token):
    decoded = decode_id(token)
    if not decoded:
        return {'code': 0, 'msg': 'bad id', 'list': []}
    source_id, vod_id = decoded
    if SPIDER_HOST and source_id in SPIDER_HOST.providers:
        items = SPIDER_HOST.detail(source_id, vod_id)
        for item in items:
            item['vod_id'] = token
        return {'code': 1, 'msg': 'duola-bridge (spider)', 'list': items,
                'total': len(items), 'page': 1, 'pagecount': 1}
    source = next((s for s in SOURCES if s['id'] == source_id), None)
    if not source:
        return {'code': 0, 'msg': 'unknown source', 'list': []}
    try:
        data = fetch_json(build_query(source['url'], {'ac': 'detail', 'ids': vod_id}), float(CFG.get('timeout', 6)))
    except Exception as e:
        return {'code': 0, 'msg': type(e).__name__, 'list': []}
    items = data.get('list') or []
    for item in items:
        item['vod_id'] = token
    return {'code': 1, 'msg': 'duola-bridge', 'list': items, 'total': len(items), 'page': 1, 'pagecount': 1}


def do_play(provider_id, flag, play_id):
    """Spider 两级取址：flag + 剧集 id -> 真实播放 URL 与 header。"""
    if not SPIDER_HOST or provider_id not in SPIDER_HOST.providers:
        return {'code': 0, 'msg': 'unknown spider provider'}
    return {'code': 1, 'result': SPIDER_HOST.play(provider_id, flag, play_id)}


def status_payload():
    health = health_load()
    return {
        'code': 1, 'service': 'duola-bridge', 'version': '2.1.0',
        'sources': [{'id': s['id'], 'name': s['name'], 'url': s['url'],
                     'cooling': is_cooling(s['url'], health, cooldown=int(CFG.get('cooldown', 1800)))}
                    for s in SOURCES],
        'stats': STATS,
        'spider': SPIDER_HOST.status() if SPIDER_HOST else {'enabled': False},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print('[bridge]', fmt % args, flush=True)

    def _reply(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        token = CFG.get('token') or ''
        if token and (query.get('token') or [''])[0] != token:
            return self._reply({'code': 0, 'msg': 'unauthorized'}, 401)
        if 'wd' in query:
            return self._reply(do_search(query['wd'][0]))
        action = (query.get('ac') or [''])[0]
        if action == 'detail':
            return self._reply(do_detail((query.get('ids') or [''])[0]))
        if action == 'list':
            return self._reply({'code': 1, 'class': [{'type_id': '0', 'type_name': '全部'}]})
        if action == 'play':
            return self._reply(do_play((query.get('provider') or [''])[0],
                                       (query.get('flag') or [''])[0],
                                       (query.get('id') or [''])[0]))
        if action == 'spider_test':
            return self._reply(SPIDER_HOST.self_test() if SPIDER_HOST else {'ok': False, 'error': 'not initialized'})
        return self._reply(status_payload())


def main():
    global CFG, SOURCES, STATS, SPIDER_HOST
    parser = argparse.ArgumentParser(description='duola-bridge (FongMi/CMS adapter)')
    parser.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sources.json'))
    args = parser.parse_args()
    with open(args.config, encoding='utf-8') as f:
        CFG = json.loads(strip_jsonc(f.read()))
    SOURCES, STATS = load_sources(CFG)
    host_cfg = dict(CFG)
    host_cfg['spider_host'] = dict(CFG.get('spider_host') or {})
    host_cfg['spider_host']['dex_sites'] = STATS.get('spider_skipped', 0)
    SPIDER_HOST = sh.SpiderHost(host_cfg, base_dir=os.path.dirname(os.path.abspath(__file__)))
    listen = CFG.get('listen', '127.0.0.1')
    port = int(CFG.get('port', 8765))
    server = ThreadingHTTPServer((listen, port), Handler)
    server.daemon_threads = True
    print('[bridge] listening on %s:%d, cms_sources=%d, spider_providers=%d, dex_skipped=%d'
          % (listen, port, len(SOURCES), len(SPIDER_HOST.providers),
             STATS.get('spider_skipped', 0)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
