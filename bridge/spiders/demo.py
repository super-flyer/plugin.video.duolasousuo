# -*- coding: utf-8 -*-
"""本地自检用 Spider（不联网、不抓第三方站点），用于验证 Spider Host 契约链路。

真实 spider 按 CatVodSpider 的 Python 契约放进 spiders/ 目录，
再在 sources.json 的 spider_host.python.spiders 里登记即可。
"""


class Spider(object):
    def init(self, ext):
        self.ext = ext or '{}'

    def homeContent(self, filter=False):
        return {'class': [{'type_id': '1', 'type_name': '演示'}], 'list': self._vods()}

    def homeVideoContent(self):
        return {'list': self._vods()}

    def categoryContent(self, tid, pg, filter, extend):
        return {'list': self._vods(), 'page': 1, 'pagecount': 1}

    def searchContent(self, key, quick=False, pg='1'):
        # 自检 spider 不污染正常搜索：只有关键词命中自检才返回结果
        text = str(key or '').lower()
        if 'demo' not in text and '自检' not in text:
            return {'list': []}
        return {'list': self._vods(key)}

    def detailContent(self, ids):
        vid = str(ids[0]) if ids else '1'
        return {'list': [{
            'vod_id': vid,
            'vod_name': 'Spider Host 自检影片',
            'type_name': '演示',
            'vod_remarks': '本地示例',
            'vod_pic': '',
            'vod_play_from': 'demo',
            'vod_play_url': '第01集$https://example.com/1.m3u8',
            'vod_content': 'This item is generated locally by the demo spider.',
        }]}

    def playerContent(self, flag, play_id, vipFlags=None):
        return {
            'parse': 0,
            'url': 'https://example.com/1.m3u8',
            'header': {'User-Agent': 'duola-bridge/2.1'},
        }

    def _vods(self, key='demo'):
        return [{
            'vod_id': '1',
            'vod_name': 'Spider Host 自检影片',
            'type_name': '演示',
            'vod_remarks': key,
            'vod_pic': '',
        }]
