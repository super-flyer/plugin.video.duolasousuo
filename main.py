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
import time

# plugin info
ADDON_name = '哆啦搜索'
ADDON_object = xbmcaddon.Addon()
ADDON_handle = int(sys.argv[1])  # 当前插件句柄
ADDON_address = sys.argv[0]  # 当前插件地址
ADDON_parm = sys.argv[2]  # 问号以后的内容
ADDON_dialog = xbmcgui.Dialog()
ADDON_PlayerStyle = int(ADDON_object.getSetting('Duola_play_style'))
ADDON_PlayerMimes = ['.m3u8', '.mp4', '.flv', '.ts', '.ogg', '.mp3']

# Get addon base path
ADDON_path = xbmcvfs.translatePath(ADDON_object.getAddonInfo('path'))
ICONS_dir = os.path.join(ADDON_path, 'resources', 'images', 'icons')
FANART_dir = os.path.join(ADDON_path, 'resources', 'images', 'fanart')
ADDON_TempDir = os.path.join(xbmcvfs.translatePath('special://home/temp'), ADDON_object.getAddonInfo('id'), '')
ADDON_CloudCacheTxt = os.path.join(ADDON_TempDir, 'Duola_Local_Search_Engine.txt')

ADDON_api = 'https://raw.githubusercontent.com/super-flyer/plugin.video.duolasousuo/refs/heads/main/v1.json'

# bot config
UA_timeout = 30
UA_head = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

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


def check_url_mime(url):
    hz = '.' + url.split('.')[-1]
    if hz in ADDON_PlayerMimes:
        return True
    else:
        return False


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


def resolve_config_sites(base_url):
    """Probe only a fixed set of seed URLs for FongMi-style config JSON.
    Never recurse into discovered HTML links to avoid unbounded crawling."""
    if not base_url:
        return []

    normalized_base = normalize_url(base_url)
    # Fixed seed candidates only — do NOT grow this list from page content
    seed_candidates = []
    for candidate in [normalized_base, normalized_base + '/api.php/provide/vod/', normalized_base + '/api.php/provide/vod']:
        if candidate not in seed_candidates:
            seed_candidates.append(candidate)

    probe_timeout = min(8, UA_timeout)
    api_candidates_from_page = []  # collect page-level config URLs to try ONCE, never recurse

    for candidate in seed_candidates:
        print('duola_debug:resolve_config_sites probe=' + candidate)
        try:
            res = requests.get(url=candidate, headers=UA_head, timeout=probe_timeout)
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
    for config_url in api_candidates_from_page[:5]:
        config_url = normalize_url(config_url)
        print('duola_debug:resolve_config_sites try page config url=' + config_url)
        try:
            res2 = requests.get(url=config_url, headers=UA_head, timeout=probe_timeout)
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


def request_engine_json_candidates(base_url, suffix=''):
    candidate_urls = []
    for candidate in build_engine_candidates(base_url):
        candidate_urls.append(build_request_url(candidate, suffix))

    print('duola_debug:request_engine_json_candidates base_url=' + str(base_url) + ' suffix=' + str(suffix))
    print('duola_debug:candidate_urls initial count=' + str(len(candidate_urls)))

    # Only resolve config sites for non-API URLs (config/navigation pages).
    # Direct VOD API URLs have no config to resolve.
    if not is_direct_vod_api(base_url):
        config_sites = resolve_config_sites(base_url)
        if config_sites:
            print('duola_debug:config_sites count=' + str(len(config_sites)))
            for site_entry in config_sites:
                for api_candidate in build_site_api_candidates(site_entry):
                    url_with_suffix = build_request_url(api_candidate, suffix)
                    if url_with_suffix not in candidate_urls:
                        candidate_urls.append(url_with_suffix)

    seen = set()
    results = []
    for candidate in candidate_urls:
        if candidate in seen:
            continue
        seen.add(candidate)
        print('duola_debug:try candidate=' + candidate)
        try:
            res = requests.get(url=candidate, headers=UA_head, timeout=UA_timeout)
            res_text = res.text
            print('duola_debug:response candidate=' + candidate + ' status=' + str(res.status_code) + ' len=' + str(len(res_text)))
        except requests.exceptions.RequestException as e:
            print('duola_debug:request failed=' + str(e) + ' candidate=' + candidate)
            continue

        if not check_json(res_text):
            print('duola_debug:invalid json from candidate=' + candidate)
            continue

        try:
            res_json = json.loads(res_text)
        except ValueError:
            continue

        if res_json.get('code') == 1:
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


def Web_load_search_multi(engine_list, keyword):
    """并发搜索所有引擎，实时显示结果"""
    merged_results = []
    seen_results = set()
    result_lock = threading.Lock()
    
    def search_engine_worker(engine, thread_id):
        """单个引擎搜索线程"""
        try:
            if not engine:
                return
            engine_url = engine['url']
            source_name = engine.get('name', '')
            print('duola_debug: [Thread-' + str(thread_id) + '] 开始搜索 ' + source_name + ' -> ' + engine_url)
            
            candidates = request_engine_json_candidates(engine_url, '?wd=' + keyword)
            if not candidates:
                print('duola_debug: [Thread-' + str(thread_id) + '] ' + source_name + ' 无可用候选地址')
                return
            
            thread_results = []
            for resolved_url, _, res_json in candidates:
                search_results = res_json.get('list', [])
                base_engine_url = resolved_url.split('?')[0].rstrip('/')
                for video in search_results:
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
            
            # 将该引擎的结果添加到全局列表
            with result_lock:
                merged_results.extend(thread_results)
            
            if thread_results:
                print('duola_debug: [Thread-' + str(thread_id) + '] ' + source_name + ' 返回 ' + str(len(thread_results)) + ' 条结果')
        except Exception as e:
            print('duola_debug: [Thread-' + str(thread_id) + '] 搜索异常: ' + str(e))
    
    # 为每个引擎创建线程
    threads = []
    for idx, engine in enumerate(engine_list):
        t = threading.Thread(target=search_engine_worker, args=(engine, idx + 1))
        t.daemon = True
        t.start()
        threads.append(t)
    
    # 等待所有线程完成（最多等待 60 秒）
    for t in threads:
        t.join(timeout=60)
    
    if len(merged_results) == 0:
        ADDON_dialog.notification(heading=ADDON_name, message='抱歉，当前所有服务器都没有找到相关资源', time=3000)
        return

    # 添加所有结果到 Kodi 列表
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


# request: https://123.com/api/provide/vod/?ac=detail&ids={detail_id}
# return: play list
def Web_load_detail_one(url, detail_id):
    detail_candidates = request_engine_json_candidates(url, '?ac=detail&ids=' + detail_id)
    if not detail_candidates:
        ADDON_dialog.notification(heading=ADDON_name, message='内容获取失败，暂不可用', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
        return

    resolved_url = None
    res_json = None
    for candidate_url, _, candidate_json in detail_candidates:
        if candidate_json and candidate_json.get('code') == 1 and candidate_json.get('list'):
            resolved_url = candidate_url
            res_json = candidate_json
            break

    if resolved_url is None or res_json is None:
        ADDON_dialog.notification(heading=ADDON_name, message='内容获取失败，暂不可用', icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
        return

    if res_json['code'] == 1:
        if len(res_json['list']) > 0:
            video = res_json['list'][0]  # 仅提取一个
            v_id = str(video['vod_id'])
            v_name = video['vod_name']
            v_typename = video['type_name']
            v_picture = video['vod_pic']
            v_list_text = video['vod_play_url']  # 多地址合集
            v_infos = {}
            try:
                v_infos['title'] = video['vod_name']
                v_infos['originaltitle'] = video['vod_sub']
                v_infos['sorttitle'] = video['vod_en']
                v_infos['country'] = video['vod_area'].split(",")
                v_infos['year'] = video['vod_year']
                v_infos['director'] = video['vod_director']
                v_infos['writer'] = video['vod_writer'].split(",")
                v_infos['cast'] = video['vod_actor'].split(',')
                v_infos['plotoutline'] = video['vod_blurb']
                v_infos['plot'] = remove_html_tags(video['vod_content'])
                v_infos['rating'] = float(video['vod_douban_score'])
            except IndexError as e:
                pass
            # dialog.select
            V_name_list = []
            V_m3u8_list = []
            playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            # 按$$$分隔不同的[播放来源]数据
            # 01集$http://a.com/1.mp4#02集$http://a.com/2.mp4$$$01集$http://a.com/1.flv#02集$http://a.com/2.flv
            V_class_data = v_list_text.split('$$$')
            if len(V_class_data) > 0:
                select_title = '请选择播放源开始播放【' + v_name + '】'
                for V_list in V_class_data:
                    # 按#分隔相同的[播放来源]数据中的不同[播放地址/剧集]
                    # 第01集$http://abc.com/1.flv#第02集$http://abc.com/2.flv
                    V_playlist = V_list.split('#')
                    V_i = 0
                    for V_play in V_playlist:
                        # 按#分隔，将vod_title与vod_url区分
                        # 第01集$http://abc.com/1.flv
                        V = V_play.split('$')
                        if len(V) == 2 and check_url_mime(V[1].strip()):
                            _v_play_label = V[0].strip()  # 去除首尾空格
                            _v_play_url = V[1].strip()  # 去除首尾空格
                            # Build play URL with HTTP headers for CDN access
                            if '|' not in _v_play_url:
                                try:
                                    _play_parsed = urllib.parse.urlparse(_v_play_url)
                                    _referer = _play_parsed.scheme + '://' + _play_parsed.netloc + '/'
                                except Exception:
                                    _referer = ''
                                if _referer:
                                    _ua = UA_head.get('User-Agent', '')
                                    _v_play_url = _v_play_url + '|Referer=' + _referer + '&User-Agent=' + urllib.parse.quote(_ua)
                            V_name_list.append(_v_play_label)  # 播放标签
                            V_m3u8_list.append(_v_play_url)  # 播放地址
                            # listitem for player_style 2
                            list_item = xbmcgui.ListItem(v_name + ': ' + _v_play_label, v_typename)
                            list_item.setContentLookup(False)
                            list_item.setMimeType('application/x-mpegURL')
                            list_item.setArt({'thumb': v_picture, 'poster': v_picture})
                            list_item.setInfo('video', v_infos)
                            playlist.add(url=_v_play_url, listitem=list_item, index=V_i)
                            V_i = V_i + 1
                        else:
                            pass  # 不符合条件的播放地址跳过
            else:
                select_title = '此视频暂时没有播放源'
            # 播放方式
            print('duola_debug: ADDON_PlayerStyle@' + str(ADDON_PlayerStyle))
            # player_style -------------------------------------------------
            if ADDON_PlayerStyle == 0:
                a = -1
                for x in V_name_list:
                    a = a + 1
                    list_item = xbmcgui.ListItem('[COLOR blue]【播放】[/COLOR]' + v_name + ' (' + x + ')')
                    list_item.setContentLookup(False)
                    list_item.setMimeType('application/x-mpegURL')
                    list_item.setArt({'icon': os.path.join(ICONS_dir, 'play.png'), 'poster': v_picture})
                    list_item.setInfo('video', v_infos)
                    xbmcplugin.addDirectoryItem(ADDON_handle, V_m3u8_list[a], list_item, False)
                xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
            # player_style -------------------------------------------------
            if ADDON_PlayerStyle == 1:
                select_i = ADDON_dialog.select(select_title, V_name_list)
                print('duola_debug: select_i ' + str(select_i))
                if select_i >= 0:
                    list_item = xbmcgui.ListItem(v_name, v_typename, V_m3u8_list[select_i], offscreen=False)
                    list_item.setContentLookup(False)
                    list_item.setMimeType('application/x-mpegURL')
                    list_item.setArt({'thumb': v_picture, 'poster': v_picture})
                    list_item.setInfo('video', v_infos)
                    # ADDON_dialog.info(list_item)  # 弹出视频
                    xbmc.Player().play(item=V_m3u8_list[select_i], listitem=list_item)
                    ADDON_dialog.notification(heading=ADDON_name, message='视频即将播放，请耐心稍候一会', time=6000, sound=False)
                xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
            # player_style -------------------------------------------------
            if ADDON_PlayerStyle == 2:
                # dialog = xbmcgui.Dialog()
                select_i = ADDON_dialog.select(select_title, V_name_list)
                print('duola_debug: select_i ' + str(select_i))
                if select_i >= 0:
                    list_item = xbmcgui.ListItem(v_name, v_typename, V_m3u8_list[select_i], offscreen=False)
                    list_item.setContentLookup(False)
                    list_item.setMimeType('application/x-mpegURL')
                    list_item.setArt({'thumb': v_picture, 'poster': v_picture})
                    list_item.setInfo('video', v_infos)
                    xbmc.Player().play(item=playlist, listitem=list_item, windowed=False, startpos=select_i)
                    ADDON_dialog.notification(heading=ADDON_name, message='视频即将播放，请耐心稍候一会', time=6000, sound=False)
                xbmcplugin.endOfDirectory(handle=ADDON_handle, succeeded=True, updateListing=False, cacheToDisc=True)
        else:
            print('duola_debug:没有数据')
            ADDON_dialog.notification(heading=ADDON_name, message='抱歉，找不到播放列表', time=3000)
    else:
        print('duola_debug:无法解析json')
        ADDON_dialog.notification(
            heading=ADDON_name,
            message='抱歉，由于无法解析返回的数据，服务暂时不可用，请稍后重试',
            icon=xbmcgui.NOTIFICATION_WARNING,
            time=3000
        )


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
        res = requests.get(url=ADDON_api, headers=UA_head, timeout=UA_timeout)
        cloud_engine_text = res.text
        # 写入缓存，降低服务器请求数
        expires_in = 3600  # 初始有效时间为1小时
        if check_json(cloud_engine_text):
            api_json = json.loads(cloud_engine_text)
            notice = base64.b64decode(api_json['notice'])
            notice_title = ADDON_name
            # version format: x.x.x
            client = str(ADDON_object.getAddonInfo('version'))
            if client != str(api_json['client']):
                notice = '当前插件可以下载新版本，如果需要升级需卸载旧版本后才可以安装新版本'
                notice_title = '发现新版本: ' + str(api_json['client'])
            if notice != "":
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
    help_text += '\n插件声明：\n本插件是基于学习和研究目的通过爬虫技术所抓取的所有内容均来自于第三方网站的数据，插件不涉及资源存储或分发。'
    help_text += '作者不对其内容承担任何责任！仅供个人学习交流之用，24小时内请自觉卸载，勿作商业用途。'
    ADDON_dialog.ok(ADDON_name + ' 帮助&声明', help_text)

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
        search_engines = []
        # 聚合搜索仅使用云端接口，本机接口跳过（本机接口请通过单独引擎入口使用）
        if ADDON_object.getSetting('Duola_Cloud_Search_Engine') == 'true':
            print('duola_debug:cloud search enabled')
            search_engines.extend(API_get_Cloud_Engine_list())
        if len(search_engines) > 0:
            Web_load_search_multi(search_engines, keyword)
        else:
            ADDON_dialog.notification(heading=ADDON_name, message='当前没有可用的云端搜索服务器，请在设置中启用云端搜索', time=4000)

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
    if xbmcvfs.exists(ADDON_TempDir):
        if xbmcvfs.rmdir(ADDON_TempDir, True):
            ADDON_dialog.notification(heading=ADDON_name, message="清理成功", time=3000)
        else:
            ADDON_dialog.notification(heading=ADDON_name, message="清理失败", icon=xbmcgui.NOTIFICATION_WARNING, time=3000)
    else:
        ADDON_dialog.notification(heading=ADDON_name, message="无需清理", time=3000)

