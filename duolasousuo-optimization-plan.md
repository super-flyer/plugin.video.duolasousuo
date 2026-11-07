# duolasousuo 最小优化意见（v1.8）

> 目标插件：`plugin.video.duolasousuo` v1.7.0  
> 代码基线：`18ca4f7`（main 分支）  
> 优化原则：**只解决主要问题，最小调整，不重构整体架构**  
> 文档日期：2026-11-07

---

## 1. 背景与目标

duolasousuo 当前已经可用，主要问题集中在三处：

| 问题 | 具体表现 |
|---|---|
| 可用源太少 | 聚合搜索只使用云端 CMS 列表；本机接口被排除；没有额外接口入口 |
| 搜索太慢 | 每个引擎多次探测 URL，单请求 30 秒；线程最多等待 60 秒；死源反复重试 |
| 播放卡顿 | 所有线路剧集混排；MIME 固定为 HLS；没有 `inputstream.adaptive`；header 不完整 |

本轮目标：

1. **增源**：聚合搜索纳入本机 + 云端 + 可配置额外接口；
2. **提速**：最坏 60 秒 → 最多 10–12 秒返回，死源冷却；
3. **稳播**：先选线路再选集；HLS 走 adaptive；MIME/header 正确。

---

## 2. 优化范围

### 本轮做

- 设置页增加“额外搜索接口”；
- 聚合搜索加入本机接口和额外接口；
- 搜索请求限时、整体截止、失败冷却；
- 详情页按线路分组；
- 播放 MIME、`inputstream.adaptive`、header 最小修复。

### 本轮不做

- FongMi type 3 Guard/PC/JS/Python Spider Host；
- PVR 直播导出；
- 本地代理、多线程播放缓存；
- service 自动换源/进度恢复；
- 整体架构重构或模块拆分。

---

## 3. 改动 1：聚合搜索纳入本机 + 额外接口

### 现状

[`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) 的 `Bot_search_all` 分支：

```python
if ADDON_object.getSetting('Duola_Cloud_Search_Engine') == 'true':
    search_engines.extend(API_get_Cloud_Engine_list())
# 本机接口被跳过
```

### 改法

1. 设置页新增：

```xml
<setting id="Duola_Extra_Search_Engines" type="text"
         label="额外搜索接口（多个用 | 分隔）" default="" />
```

2. 聚合搜索列表改为：

```text
本机接口 + 云端接口 + 额外接口
```

3. 额外接口按 `|` 拆分，做 `normalize_url`、`is_direct_vod_api` 过滤和去重。

4. 每个接口仍保持现有结构：

```python
{"name": "本机接口", "url": "https://.../api.php/provide/vod/"}
```

### 验收

- 聚合搜索不再排除本机接口；
- 额外接口可配置、可去重；
- 源数量不再受云端 8 个接口限制。

> 注意：本轮只能增加 CMS/type 1 类型的源；六个 Guard 源仍要等 Host。

---

## 4. 改动 2：搜索限时 + 健康缓存

### 现状

- [`request_engine_json_candidates()`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) 的 `requests.get` 使用 `timeout=30`；
- [`Web_load_search_multi()`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) 对每个线程 `join(timeout=60)`；
- 没有健康检查，死源每次搜索都重试。

### 改法

新增常量：

```python
SEARCH_TIMEOUT = 8       # 单请求超时
SEARCH_DEADLINE = 10     # 整体截止时间
ENGINE_COOLDOWN = 1800   # 死源冷却 30 分钟
```

1. `request_engine_json_candidates()` 中：

```python
requests.get(..., timeout=SEARCH_TIMEOUT)
```

2. `Web_load_search_multi()` 改为总截止等待：

```python
deadline = time.time() + SEARCH_DEADLINE
for t in threads:
    t.join(max(0, deadline - time.time()))
```

到点后直接使用已拿到的部分结果，不再等 60 秒。

3. 增加轻量健康缓存：

- 文件：`addon_data/engine_health.json`
- 结构：

```json
{
  "https://example.com/api.php/provide/vod/": {
    "fail_until": 1760000000,
    "last_ok": 1759990000
  }
}
```

- 搜索前跳过 `fail_until > now` 的引擎；
- 请求失败写入 `now + ENGINE_COOLDOWN`；
- 成功清除失败标记。

### 验收

- 搜索最坏 ≤ 10–12 秒返回；
- 死源失败一次后 30 分钟内不再参与搜索；
- 现有单接口搜索不受影响。

---

## 5. 改动 3：详情页按线路分组，不再混排

### 现状

[`Web_load_detail_one()`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) 把 `vod_play_url` 中所有 `$$$` 线路的所有剧集，塞进同一个 `V_name_list` / `playlist`：

```python
V_class_data = v_list_text.split('$$$')
for V_list in V_class_data:
    for V_play in V_list.split('#'):
        V_name_list.append(...)
        V_m3u8_list.append(...)
        playlist.add(...)
```

结果：

- 无法选择线路；
- 播放列表跨线路混排；
- 自动连播可能跳到另一条线路。

### 改法

只改 `Web_load_detail_one()`：

1. 同时解析 `vod_play_from` 和 `vod_play_url`：

```python
flags = video.get('vod_play_from', '').split('$$$')
urls = video.get('vod_play_url', '').split('$$$')
```

2. 构建线路模型：

```python
lines = []
for i, line in enumerate(urls):
    episodes = []
    for ep in line.split('#'):
        if '$' in ep:
            name, url = ep.split('$', 1)
            episodes.append({"name": name.strip(), "url": url.strip()})
    lines.append({
        "name": flags[i] if i < len(flags) and flags[i] else f"线路{i + 1}",
        "episodes": episodes
    })
```

3. 先选线路，再选集：

```python
line_i = 0 if len(lines) == 1 else dialog.select(
    "选择播放源", [line["name"] for line in lines]
)
ep_i = dialog.select(
    "选择剧集", [ep["name"] for ep in lines[line_i]["episodes"]]
)
```

4. `player_style=2` 时，播放列表只放当前线路：

```python
playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
for ep in lines[line_i]["episodes"][ep_i:]:
    playlist.add(url=ep["url"], listitem=...)

xbmc.Player().play(playlist, startpos=0)
```

5. `player_style=0/1` 时，用当前线路的选中集播放。

### 验收

- 多线路剧集先选线路，再选集；
- 播放列表只包含当前线路；
- 连续播放保留，但只在当前线路内推进；
- 不再出现跨线路错集。

---

## 6. 改动 4：播放 MIME、adaptive、header 最小修复

### 现状问题

1. `check_url_mime()` 使用：

```python
hz = '.' + url.split('.')[-1]
```

`https://x/a.m3u8?token=...` 会判断失败。

2. 所有地址都强制：

```python
list_item.setMimeType('application/x-mpegURL')
```

mp4/flv 也被当成 HLS。

3. 没有 `inputstream.adaptive`。

4. header 只拼 Referer/UA，编码不完整，缺少 Origin。

### 改法

1. 修正 MIME 判断：

```python
from urllib.parse import urlparse

path = urlparse(url).path.lower()
is_hls = path.endswith('.m3u8')
is_dash = path.endswith('.mpd')
is_mp4 = path.endswith(('.mp4', '.flv', '.mkv', '.ts'))
```

2. 新增 `prepare_play_item(list_item, url, v_infos, v_picture)`：

`.m3u8`：

```python
list_item.setProperty('inputstream', 'inputstream.adaptive')
list_item.setProperty('inputstream.adaptive.manifest_type', 'hls')
list_item.setProperty('inputstream.adaptive.stream_headers', header_text)
list_item.setMimeType('application/vnd.apple.mpegurl')
```

`.mpd`：

```python
list_item.setProperty('inputstream', 'inputstream.adaptive')
list_item.setProperty('inputstream.adaptive.manifest_type', 'dash')
list_item.setMimeType('application/dash+xml')
```

`.mp4/.flv`：

- 不强制 HLS MIME；
- 按实际类型设置或留空。

3. header 统一构造：

```text
User-Agent=...&Referer=...&Origin=...
```

- adaptive 使用 `stream_headers`；
- 直链使用 `|User-Agent=...&Referer=...`；
- `Referer` 默认使用播放地址的 `scheme://host/`。

4. 保留 `setContentLookup(False)`，每个播放 item 统一调用 helper。

### 验收

- `https://x/a.m3u8?token=...` 能识别；
- HLS 日志显示 `inputstream.adaptive`；
- mp4/flv 不被强制成 HLS；
- CDN 需要 Referer/UA/Origin 时能正常起播。

---

## 7. 验收标准

| 项目 | 标准 |
|---|---|
| 源数量 | 聚合搜索包含本机 + 云端 + 额外接口，去重后数量可配置 |
| 搜索时间 | 最坏 ≤ 10–12 秒返回；不再出现 60 秒等待 |
| 死源 | 失败一次后 30 分钟内不再参与搜索 |
| 线路 | 多线路剧集先选线路，再选集；播放列表只含当前线路 |
| MIME | `.m3u8?token=` 能识别；mp4/flv 不强制 HLS |
| 播放器 | HLS 使用 `inputstream.adaptive` |
| 回归 | 原有单接口搜索、详情、播放、云端菜单保持可用；现有测试通过 |

---

## 8. 实施顺序与版本

| 顺序 | 内容 | 风险 | 建议版本 |
|---|---|---|---|
| 1 | 搜索限时 + 健康缓存 | 低 | v1.8 |
| 2 | 本机 + 额外接口 | 低 | v1.8 |
| 3 | MIME/adaptive/header | 中低 | v1.9 |
| 4 | 线路分组 | 中 | v1.9 |

建议：

- **v1.8 先解决“慢”和“源少”**；
- **v1.9 再解决“线路混排/卡顿”**；
- 每个版本独立提交，便于回滚。

---

## 9. 风险与回滚

| 风险 | 应对 |
|---|---|
| 额外接口大量失效 | 健康缓存 + 30 分钟冷却；用户可清空额外接口 |
| 10 秒截止漏掉慢但可用的源 | 保留单接口搜索入口；慢源可单独搜索 |
| `inputstream.adaptive` 未安装 | 先检测，未安装则回退直链播放 |
| 线路分组改变原有操作习惯 | 单线路自动跳过线路选择；多线路才弹两次 |
| 播放 header 影响部分源 | header 仅在需要时注入；保留原 URL 回退 |
| 改动破坏现有功能 | 每个改动独立 commit；保留 v1.7.0 tag |

---

## 10. 后续（不在本轮）

- FongMi type 3 Host：Guard/PC/JS/Python；
- service 自动连播/失败换源/进度恢复；
- PVR 直播导出；
- 本地 HLS 分片缓存/预取代理；
- MP4 Range 多线程代理。

---

## 11. 附录：涉及代码位置

| 位置 | 用途 |
|---|---|
| [`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) `check_url_mime()` | MIME 判断 |
| [`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) `request_engine_json_candidates()` | CMS 请求/候选探测 |
| [`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) `Web_load_search_multi()` | 聚合搜索 |
| [`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) `Web_load_detail_one()` | 详情/线路/播放 |
| [`main.py`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/main.py) `Bot_search_all` | 聚合搜索入口 |
| [`resources/settings.xml`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/resources/settings.xml) | 新增额外接口设置 |
| [`v1.json`](https://github.com/super-flyer/plugin.video.duolasousuo/blob/main/v1.json) | 云端 CMS 列表（数据，可选更新） |
