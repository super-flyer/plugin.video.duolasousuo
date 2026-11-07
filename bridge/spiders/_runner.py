# -*- coding: utf-8 -*-
"""Python Spider 子进程运行器：加载 .py spider，调用 CatVod 契约方法。

输出与 SpiderHostRunner.java 一致：{"ok":bool,"data":"<json字符串>","error":str}
"""
import importlib.util
import json
import sys
import types

sys.modules.setdefault('catvod', types.ModuleType('catvod'))
sys.modules.setdefault('com', types.ModuleType('com'))


def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return {'raw': value}
    return {}


def main():
    try:
        path, action, ext = sys.argv[1], sys.argv[2], sys.argv[3]
        params = sys.argv[4:]
        spec = importlib.util.spec_from_file_location('duola_spider_module', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ctor = getattr(module, 'Spider', None)
        spider = ctor() if ctor else module
        if hasattr(spider, 'init'):
            try:
                spider.init(ext)
            except TypeError:
                spider.init()

        if action == 'home':
            raw = spider.homeVideoContent()
        elif action == 'search':
            raw = spider.searchContent(params[0], True)
        elif action == 'detail':
            raw = spider.detailContent([params[0]])
        elif action == 'play':
            raw = spider.playerContent(params[0], params[1], [])
        else:
            raise ValueError('unknown action: ' + action)
        data = as_dict(raw)
        print(json.dumps({'ok': True, 'data': json.dumps(data, ensure_ascii=False), 'error': ''}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'data': '{}', 'error': str(exc)}))


if __name__ == '__main__':
    main()
