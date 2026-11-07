#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""duola-bridge: FongMi/TVBox 配置 -> MACCMS JSON 适配服务（v2.2）。

在家里的服务器（与 Kodi 同机）只监听 127.0.0.1，把多个 CMS 资源站聚合成
标准 /api.php/provide/vod/ 接口，Kodi 插件的「额外搜索接口」直接填它即可。

- 支持读取 FongMi/TVBox 配置（URL 或本地文件，允许 jsonc 注释），把其中
  type 0/1 的 HTTP CMS 站点自动纳入；type 3 的 csp_*/js/py 需要 Spider Host，
  这里只统计并跳过，不假装能执行。
- 搜索并发聚合 + 去重 + 上游死源冷却；详情按编码后的 vod_id 回源。
- v2.2: 详情回源时补全间接播放地址（playerContent / 播放页抓取），写回 vod_play_url。
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
import play_resolver as pr

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

CFG = {}
SOURCES = []
STATS = {}
SPIDER_HOST = None
PLAY_RESOLVER = None
PEER_INDEX = {}          # 同名影片 -> 其它源的编码 id（跨源线路联邦）
PEER_LOCK = threading.Lock()
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


PEER_TTL = 900
PEER_MAX_PER_NAME = 6


def note_peer(name, token, source_id, source_name, now=None):
    """搜索时登记同名影片在各源的 id，即使被去重丢弃也保留，供详情合并线路。"""
    key = str(name or '').strip().lower()
    if not key or not token:
        return
    now = now or time.time()
    with PEER_LOCK:
        entry = PEER_INDEX.get(key)
        if not entry or now - entry['ts'] > PEER_TTL:
            entry = {'ts': now, 'tokens': []}
            PEER_INDEX[key] = entry
        tokens = entry['tokens']
        if any(t[0] == token for t in tokens) or any(t[1] == source_id for t in tokens):
            return
        if len(tokens) < PEER_MAX_PER_NAME:
            tokens.append((token, source_id, str(source_name or '')))
        if len(PEER_INDEX) > 2000:
            for stale in list(PEER_INDEX)[:500]:
                PEER_INDEX.pop(stale, None)


def find_peers(name, self_token, self_source, max_peers=3, now=None):
    key = str(name or '').strip().lower()
    now = now or time.time()
    with PEER_LOCK:
        entry = PEER_INDEX.get(key)
        if not entry or now - entry['ts'] > PEER_TTL:
            return []
        return [t for t in entry['tokens'] if t[0] != self_token and t[1] != self_source][:max_peers]


def first_line_host(line):
    ep = (line or '').split('#')[0]
    url = ep.split('$', 1)[1] if '$' in ep else ep
    url = url.split('|', 1)[0]
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ''


def merge_peer_lines(primary, peer_items, max_lines=12):
    """把其它源的同名影片线路并入主详情；按播放域名去重，返回新增线路数。

    peer_items: [(source_name, item_dict)]，item 为 CMS 形状的详情条目。
    """
    flags = [f for f in str(primary.get('vod_play_from') or '').split('$$$') if f]
    urls = str(primary.get('vod_play_url') or '').split('$$$')
    if len(flags) < len(urls):
        flags += ['线路%d' % (i + 1) for i in range(len(flags), len(urls))]
    hosts = set(filter(None, (first_line_host(u) for u in urls)))
    added = 0
    for source_name, item in peer_items:
        if not isinstance(item, dict) or len(urls) >= max_lines:
            continue
        p_flags = str(item.get('vod_play_from') or '').split('$$$')
        p_urls = str(item.get('vod_play_url') or '').split('$$$')
        for index, line in enumerate(p_urls):
            if len(urls) >= max_lines:
                break
            host = first_line_host(line)
            if not line or (host and host in hosts):
                continue
            flag = p_flags[index].strip() if index < len(p_flags) else ''
            flags.append('[' + str(source_name) + ']' + (flag or '线路'))
            urls.append(line)
            if host:
                hosts.add(host)
            added += 1
    if added:
        primary['vod_play_from'] = '$$$'.join(flags)
        primary['vod_play_url'] = '$$$'.join(urls)
    return added


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
                    token = encode_id(provider_id, item.get('vod_id', ''))
                    note_peer(name, token, provider_id, provider_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    out = dict(item)
                    out['vod_id'] = token
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
                token = encode_id(source['id'], item.get('vod_id', ''))
                note_peer(name, token, source['id'], source['name'])
                if key in seen:
                    continue
                seen.add(key)
                out = dict(item)
                out['vod_id'] = token
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


def _detail_raw(token, resolve=True):
    """取单个源的原始详情，不做跨源合并（供联邦调用，避免死循环）。

    联邦拉取 peer 时传 resolve=False，跳过耗时的播放地址补全，只取线路。
    """
    decoded = decode_id(token)
    if not decoded:
        return {'code': 0, 'msg': 'bad id', 'list': []}, ''
    source_id, vod_id = decoded
    resolved = 0
    if SPIDER_HOST and source_id in SPIDER_HOST.providers:
        items = SPIDER_HOST.detail(source_id, vod_id)
        base = pr.provider_base(SPIDER_HOST, source_id)
        source_name = getattr(SPIDER_HOST.providers[source_id], 'name', source_id)
        for item in items:
            item['vod_id'] = token
            if PLAY_RESOLVER and resolve:
                resolved += PLAY_RESOLVER.complete(item, source_id, base=base)['resolved']
        return {'code': 1, 'msg': 'duola-bridge (spider)', 'list': items,
                'total': len(items), 'page': 1, 'pagecount': 1, 'resolved': resolved}, source_name
    source = next((s for s in SOURCES if s['id'] == source_id), None)
    if not source:
        return {'code': 0, 'msg': 'unknown source', 'list': []}, ''
    try:
        data = fetch_json(build_query(source['url'], {'ac': 'detail', 'ids': vod_id}), float(CFG.get('detail_timeout', 5)))
    except Exception as e:
        return {'code': 0, 'msg': type(e).__name__, 'list': []}, source['name']
    items = data.get('list') or []
    for item in items:
        item['vod_id'] = token
        if PLAY_RESOLVER and resolve:
            resolved += PLAY_RESOLVER.complete(item, source_id, base=source['url'])['resolved']
    return {'code': 1, 'msg': 'duola-bridge', 'list': items,
            'total': len(items), 'page': 1, 'pagecount': 1, 'resolved': resolved}, source['name']


def federation_config():
    cfg = CFG.get('line_federation') or {}
    return {
        'enabled': bool(cfg.get('enabled', True)),
        'max_peers': max(0, int(cfg.get('max_peers', 3))),
        'max_lines': max(1, int(cfg.get('max_lines', 12))),
        'peer_timeout': float(cfg.get('peer_timeout', 5)),
    }


def do_detail(token):
    """详情 = 主源 + 同名影片在其它源的线路（跨源联邦）。

    好处：某个源/站点（含频繁失效的 Spider 站点）的 CDN 挂了，
    用户仍能在同一个详情里选到其它源的可播线路，无需返回重选。
    """
    payload, source_name = _detail_raw(token)
    if payload.get('code') != 1 or not payload.get('list'):
        return payload
    cfg = federation_config()
    payload['federated'] = 0
    if not cfg['enabled'] or cfg['max_peers'] <= 0:
        return payload
    primary = payload['list'][0]
    decoded = decode_id(token)
    self_source = decoded[0] if decoded else ''
    peers = find_peers(primary.get('vod_name'), token, self_source, max_peers=cfg['max_peers'])
    if not peers:
        return payload

    def fetch_peer(peer):
        try:
            result, _ = _detail_raw(peer[0], resolve=False)
            items = result.get('list') or []
            return (peer[2] or peer[1], items[0] if items else None)
        except Exception:
            return (peer[2] or peer[1], None)

    pool = ThreadPoolExecutor(max_workers=min(cfg['max_peers'], len(peers)))
    peer_items = []
    try:
        futs = [pool.submit(fetch_peer, p) for p in peers]
        # 超预算时保留已完成的部分，不要清空
        for fut in as_completed(futs, timeout=cfg['peer_timeout']):
            try:
                name, item = fut.result()
                if item:
                    peer_items.append((name, item))
            except Exception:
                continue
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # 合并只引入直链线路；不再整体重跑播放补全，避免详情耗时翻倍。
    # 若个别线路是间接地址，插件侧 v2.0 的预检会标 [不可用]。
    payload['federated'] = merge_peer_lines(primary, peer_items, max_lines=cfg['max_lines'])
    payload['peer_sources'] = [n for n, _ in peer_items]
    return payload


def do_resolve(source_id, flag, play_id, base=''):
    """诊断端点：查看单个播放 id 的补全结果。"""
    if not PLAY_RESOLVER:
        return {'code': 0, 'msg': 'resolver not initialized'}
    final, method = PLAY_RESOLVER.resolve_one(source_id, flag, play_id, base)
    return {'code': 1, 'source': source_id, 'flag': flag, 'original': play_id,
            'url': final, 'method': method, 'resolved': bool(final and final != play_id)}


def do_play(provider_id, flag, play_id):
    """Spider 两级取址：flag + 剧集 id -> 真实播放 URL 与 header。"""
    if not SPIDER_HOST or provider_id not in SPIDER_HOST.providers:
        return {'code': 0, 'msg': 'unknown spider provider'}
    return {'code': 1, 'result': SPIDER_HOST.play(provider_id, flag, play_id)}


def status_payload():
    health = health_load()
    return {
        'code': 1, 'service': 'duola-bridge', 'version': '2.3.0',
        'sources': [{'id': s['id'], 'name': s['name'], 'url': s['url'],
                     'cooling': is_cooling(s['url'], health, cooldown=int(CFG.get('cooldown', 1800)))}
                    for s in SOURCES],
        'stats': STATS,
        'spider': SPIDER_HOST.status() if SPIDER_HOST else {'enabled': False},
        'play_resolver': PLAY_RESOLVER.status() if PLAY_RESOLVER else {'enabled': False},
        'line_federation': dict(federation_config(), indexed_titles=len(PEER_INDEX)),
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
        if action == 'resolve':
            return self._reply(do_resolve((query.get('provider') or [''])[0],
                                          (query.get('flag') or [''])[0],
                                          (query.get('id') or [''])[0],
                                          (query.get('base') or [''])[0]))
        return self._reply(status_payload())


def main():
    global CFG, SOURCES, STATS, SPIDER_HOST, PLAY_RESOLVER
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
    PLAY_RESOLVER = pr.PlayResolver(CFG.get('play_resolve'), spider_host=SPIDER_HOST)
    listen = CFG.get('listen', '127.0.0.1')
    port = int(CFG.get('port', 8765))
    server = ThreadingHTTPServer((listen, port), Handler)
    server.daemon_threads = True
    print('[bridge] listening on %s:%d, cms_sources=%d, spider_providers=%d, '
          'dex_skipped=%d, play_resolve=%s'
          % (listen, port, len(SOURCES), len(SPIDER_HOST.providers),
             STATS.get('spider_skipped', 0), PLAY_RESOLVER.enabled), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
