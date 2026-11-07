# -*- coding: utf-8 -*-
"""Spider Host: 在服务器侧执行 FongMi/CatVod Spider 契约并输出 CMS 形状数据。

支持的运行时：
- java  : 标准 JVM .class Spider（如 pc-spider.jar），经 SpiderHostRunner 反射调用；
          Android dex 格式的 csp_*Guard jar 需要 DexClassLoader，本层只登记不执行。
- python: 本地 .py Spider（CatVodSpider Python 契约），子进程隔离执行。
- js    : .js Spider（QuickJS/Node 契约），仅在检测到 Node 时启用。

统一接口: home / search / detail / play(两级取址)，结果交给 duola_bridge 聚合。
执行第三方 Spider 字节码有风险，默认由 sources.json 的 spider_host.enabled 控制，
并且只加载配置里显式白名单的类/文件；每次调用都有超时与子进程隔离。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


class SpiderError(Exception):
    pass


def build_classpath(base_dir, jar_dir='jars', runner_dir=None):
    root = os.path.abspath(base_dir)
    parts = [os.path.abspath(runner_dir) if runner_dir else root]
    full = os.path.join(root, jar_dir)
    if os.path.isdir(full):
        parts += [os.path.join(full, name) for name in sorted(os.listdir(full)) if name.endswith('.jar')]
    return os.pathsep.join(parts)


class BaseProvider(object):
    kind = 'base'

    def __init__(self, provider_id, name, ext='', timeout=8, cwd=None):
        self.id = provider_id
        self.name = name
        self.ext = ext or '{}'
        self.timeout = timeout
        self.cwd = cwd or os.getcwd()
        self.available = True
        self.last_error = ''

    def status(self):
        return {'id': self.id, 'name': self.name, 'kind': self.kind,
                'available': self.available, 'last_error': self.last_error}

    def command(self, action, *params):
        raise NotImplementedError

    def call(self, action, *params):
        if not self.available:
            raise SpiderError('%s runtime unavailable' % self.kind)
        proc = subprocess.run(
            self.command(action, *params),
            capture_output=True, text=True, timeout=self.timeout, cwd=self.cwd,
        )
        lines = [line.strip() for line in (proc.stdout or '').splitlines() if line.strip()]
        line = next((line for line in reversed(lines) if line.startswith('{')), '')
        if not line:
            raise SpiderError('%s produced no JSON: %s' % (self.kind, (proc.stderr or '')[:160]))
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise SpiderError('invalid runner output: %s' % exc)
        if not payload.get('ok'):
            raise SpiderError(payload.get('error') or 'spider call failed')
        raw = payload.get('data') or '{}'
        try:
            return json.loads(raw)
        except ValueError:
            return {'raw': raw}

    def home(self):
        return self.call('home').get('list') or []

    def search(self, keyword):
        return self.call('search', keyword).get('list') or []

    def detail(self, vod_id):
        return self.call('detail', vod_id).get('list') or []

    def play(self, flag, play_id):
        return self.call('play', flag, play_id)


class JavaSpiderProvider(BaseProvider):
    kind = 'java'

    def __init__(self, provider_id, name, class_name, ext, classpath, java_bin='java', timeout=8, cwd=None):
        super(JavaSpiderProvider, self).__init__(provider_id, name, ext, timeout, cwd)
        self.class_name = class_name
        self.classpath = classpath
        self.java_bin = java_bin or 'java'
        self.available = bool(shutil.which(self.java_bin)) and bool(classpath)

    def command(self, action, *params):
        return [self.java_bin, '-cp', self.classpath, 'SpiderHostRunner',
                action, self.class_name, self.ext] + [str(p) for p in params]


class PythonSpiderProvider(BaseProvider):
    kind = 'python'

    def __init__(self, provider_id, name, spider_file, ext, runner_file=None, timeout=8, cwd=None):
        super(PythonSpiderProvider, self).__init__(provider_id, name, ext, timeout, cwd)
        self.spider_file = spider_file
        self.runner_file = runner_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       'spiders', '_runner.py')
        self.available = os.path.exists(self.spider_file) and os.path.exists(self.runner_file)

    def command(self, action, *params):
        return [sys.executable, self.runner_file, self.spider_file,
                action, self.ext] + [str(p) for p in params]


class JsSpiderProvider(BaseProvider):
    kind = 'js'

    def __init__(self, provider_id, name, spider_file, ext, node_bin=None,
                 runner_file=None, timeout=8, cwd=None):
        super(JsSpiderProvider, self).__init__(provider_id, name, ext, timeout, cwd)
        self.node_bin = node_bin or shutil.which('node') or shutil.which('nodejs') or ''
        self.spider_file = spider_file
        self.runner_file = runner_file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       'spiders', '_js_runner.js')
        self.available = bool(self.node_bin) and os.path.exists(self.spider_file) and os.path.exists(self.runner_file)
        if not self.node_bin:
            self.last_error = 'node/quickjs runtime not found'

    def command(self, action, *params):
        return [self.node_bin, self.runner_file, self.spider_file,
                action, self.ext] + [str(p) for p in params]


class SpiderHost(object):
    def __init__(self, config=None, base_dir=None):
        self.config = config or {}
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.providers = {}
        self.notes = {'dex_skipped': 0, 'java_runtime': False, 'js_runtime': False,
                      'python_runtime': True, 'remote_spider_url': ''}
        self.enabled = False
        self._load()

    def _path(self, value):
        if not value:
            return ''
        return value if os.path.isabs(value) else os.path.abspath(os.path.join(self.base_dir, value))

    def _load(self):
        cfg = self.config.get('spider_host') or {}
        self.enabled = bool(cfg.get('enabled'))
        self.notes['remote_spider_url'] = cfg.get('remote_spider_url') or ''
        if not self.enabled:
            return

        py_cfg = cfg.get('python') or {}
        if py_cfg.get('enabled', True):
            for index, item in enumerate(py_cfg.get('spiders') or []):
                provider = PythonSpiderProvider(
                    'p%d' % index, item.get('name') or item.get('file', 'py'),
                    self._path(item.get('file', '')), item.get('ext', '{}'),
                    timeout=cfg.get('timeout', 8), cwd=self.base_dir)
                self.providers[provider.id] = provider

        js_cfg = cfg.get('js') or {}
        self.notes['js_runtime'] = bool(shutil.which('node') or shutil.which('nodejs'))
        for index, item in enumerate(js_cfg.get('spiders') or []):
            provider = JsSpiderProvider(
                'k%d' % index, item.get('name') or item.get('file', 'js'),
                self._path(item.get('file', '')), item.get('ext', '{}'),
                node_bin=js_cfg.get('node_bin'), timeout=cfg.get('timeout', 8), cwd=self.base_dir)
            self.providers[provider.id] = provider

        java_cfg = cfg.get('java') or {}
        self.notes['java_runtime'] = bool(shutil.which('java'))
        classpath = build_classpath(self.base_dir, java_cfg.get('jar_dir', 'jars'))
        if java_cfg.get('enabled') and self.notes['java_runtime'] and classpath:
            for index, item in enumerate(java_cfg.get('classes') or []):
                provider = JavaSpiderProvider(
                    'j%d' % index, item.get('name') or item.get('class', 'java'),
                    item.get('class', ''), item.get('ext', '{}'), classpath,
                    java_bin=java_cfg.get('java_bin') or 'java',
                    timeout=cfg.get('timeout', 8), cwd=self.base_dir)
                self.providers[provider.id] = provider

        # FongMi 配置里 csp_* 的 Android dex jar 需要 DexClassLoader，只登记
        self.notes['dex_skipped'] = int(cfg.get('dex_sites') or 0)

    def status(self):
        return {
            'enabled': self.enabled,
            'providers': [p.status() for p in self.providers.values()],
            'notes': self.notes,
        }

    def search(self, keyword, budget=8):
        """各 Provider 并行搜索；顺序执行会让多个 JVM 启动时间累加而超时。"""
        out = []
        providers = [p for p in self.providers.values() if p.available]
        if not providers:
            return out
        pool = ThreadPoolExecutor(max_workers=len(providers))
        futures = {pool.submit(p.search, keyword): p for p in providers}
        try:
            for future in as_completed(futures, timeout=budget):
                provider = futures[future]
                try:
                    for item in future.result():
                        if isinstance(item, dict):
                            out.append((provider.id, provider.name, item))
                except Exception as exc:
                    provider.last_error = str(exc)[:160]
        except Exception:
            pass  # 到预算就返回已完成的部分
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out

    def detail(self, provider_id, vod_id):
        provider = self.providers.get(provider_id)
        if not provider:
            return []
        try:
            return provider.detail(vod_id)
        except Exception as exc:
            provider.last_error = str(exc)[:160]
            return []

    def play(self, provider_id, flag, play_id):
        provider = self.providers.get(provider_id)
        if not provider:
            return {}
        try:
            return provider.play(flag, play_id)
        except Exception as exc:
            provider.last_error = str(exc)[:160]
            return {}

    def self_test(self):
        """用本地 demo spider 跑一遍完整契约，验证 Host 链路。"""
        result = {'ok': True, 'steps': {}}
        demo = next((p for p in self.providers.values() if p.kind == 'python'), None)
        if not demo:
            return {'ok': False, 'error': 'no python demo spider'}
        try:
            home = demo.home()
            result['steps']['home'] = len(home)
            found = demo.search('demo')
            result['steps']['search'] = len(found)
            vid = str((found[0] if found else {}).get('vod_id', '1'))
            detail = demo.detail(vid)
            result['steps']['detail'] = len(detail)
            play = demo.play('demo', vid)
            result['steps']['play'] = bool(play.get('url') or play.get('data'))
        except Exception as exc:
            result['ok'] = False
            result['error'] = str(exc)
        return result
