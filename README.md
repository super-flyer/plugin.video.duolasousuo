# plugin.video.duolasousuo

哆啦搜索（Kodi视频插件）

> 声明：本插件是基于学习和研究目的通过爬虫技术所抓取的所有内容均来自于第三方网站的数据，插件不涉及资源存储或分发。作者不对其内容承担任何责任！仅供个人学习交流之用，24小时内请自觉卸载，勿作商业用途。


### 如何下载

插件可以從 releases 選項下載。 請勿嘗試安裝GitHub自動生成的 .zip


### 如何更換其他資源站數據接口？

插件在設置面板中提供了一個設定項目，您可以在修改【內置搜索引擎接口】為你所需要的資源站數據接口，那麽程序就會按照該預設的地址去獲取資源。


### 如何添加額外搜索接口？（v1.8）

聚合搜索會同時使用「内置接口 + 雲端接口（如已啓用） + 額外接口」。在設置的【額外搜索接口】中填寫地址，多個用 `|` 分隔：

```
名稱=https://api.example.com/api.php/provide/vod/|https://another.com/api.php/provide/vod/
```

- 可用 `名稱=地址` 給接口起名字，不寫名稱時列表顯示域名。
- 自定義路徑（非 `api.php/provide/vod`）請加 `direct:` 前綴，插件會直接請求該地址並跳過接口探測，例如 `家裡=direct:http://10.0.0.2:8080/vod/`。
- 重複地址會自動去重；連續失敗的接口冷卻 30 分鐘，可在【清理緩存】中立即恢復。
- 聚合搜索最多等待 12 秒並使用已返回的結果；慢接口仍可通過單接口入口單獨搜索。

### v2.3 跨源線路聯邦（不依賴單一站点）

對頻繁換域名 / 地區封禁的站點（含 `csp_*Guard` 的熱播、茉莉、荐片、瓜子），
更穩的辦法不是逆向某個 Spider，而是**讓同一部片的播放線路來自多個源**：

- `bridge/duola_bridge.py`：搜索時用 `note_peer()` 記住同名影片在各源的編碼 id
  （即使被去重丟掉的條目也保留，TTL 15 分鐘、每源一條）；詳情時 `do_detail()`
  並發取最多 3 個同源影片的詳情，`merge_peer_lines()` 按**播放域名去重**後把線路
  併入主詳情，線路名帶 `[源名]` 前綴。某個 CDN 403/404 時仍可在同一個詳情裡
  改選其它源的線路，不必返回重選源。
- `sources.json` 的 `line_federation` 可關（`enabled:false`），`max_peers` /
  `max_lines` 可調；狀態接口上報 `indexed_titles`，詳情響應帶
  `federated` 與 `peer_sources`。
- 源池同時補入實測可用的 `如意 / 量子 / 百度` 三個 CMS 接口，與既有源、
  FongMi 配置提取的 CMS 源、IkBot Spider 一起參與聚合。
- 插件 `main.py` 的線路預檢修正：實測有 CDN（`svip.ryiplay18.com`）
  **HEAD 返回 404 但 GET 200 且內容是 `#EXTM3U`**，只信 HEAD 會把可播線路
  誤標 `[不可用]`。現在 HEAD 遇 400/403/404 會回退 `GET Range: bytes=0-1023`，
  並排除 HTML 錯誤頁。

補充調研結論：熱播 / 荐片在公開 DRPY2 規則裡有明文 JS 版本
（`荐片.js` 的 `api2.rinhome.com`、`热播库.js` 的 `reboku.cc`），
但實測這兩個上游目前也已失效（停放頁 / 空回覆），
故未把 JS 規則直譯作為落地方案；聯邦冗餘對站點失效更健壯。

### v2.2 播放地址補全（服務器側為主）

`bridge/play_resolver.py` 會在**詳情回源時**把間接播放地址改寫成 Kodi 可直接播放的 URL，插件側只看到標準 `vod_play_url`：

- `playerContent` 優先：對不含協定/路徑的播放 id 調用 Spider 二級取址，統一處理 `url / header / parse / jx`，header 轉成 Kodi `|Key=Value&...` 語法並做 URL 編碼。
- 播放頁抓取兜底：`.html` / `/vodplay/...` 之類地址按 `base_url`（`sources.json` 的 `base_url` 或 `ext.site`）補全為絕對 URL，抓頁面裡的 `player_aaaa` / `player_data` JSON 或媒體直鏈，並回填 Referer/UA 防盜鏈頭。實測 `Zxzj` 的 Java `playerContent` 因站點改版失效，但頁面解析仍能取到真實地址。
- 已是 `m3u8/mp4` 等直鏈的分集**完全不改写**；無後綴的 http 地址保持原樣（v1.9 已放行）。解析並發上限 8、單次 3 秒、整體 6 秒預算、最多 40 集，結果按 (源, 線路, id) 快取 10 分鐘。
- 插件側補了兩處：`prepare_play_item()` 在 `inputstream.adaptive` 分支不再丟掉 `|` 自定義 header；線路預檢 HEAD 請求也會帶上補全帶回的 Referer/UA。
- 新端點：`?ac=resolve&provider=&flag=&id=&base=`（單個 id 補全診斷），狀態接口新增 `play_resolver` 統計；詳情響應帶 `resolved` 計數。
- 若 `playerContent` 返回 `parse=1`，需自行在 `play_resolve.parse_url` 配置解析接口前綴；未配置時該分集標記為不可播。

白名單同步調整為 `IkBot`（實測直接給出 31 條線路的可播 m3u8）；移除 `Wogg`（詳情無播放列表）、`SeedHub`（詳情異常）與 `Zxzj`（補全後的上游媒體對當前出口固定 403，且詳情耗時 13 秒），避免搜索結果裡混入必然打不開的條目。播放頁補全能力仍保留，給 CMS 間接地址和今後啟用的 Spider 使用，可用 `?ac=resolve&id=/vodplay/1.html&base=https://站點/` 單獨驗證。

### v2.1 Spider Host（服務器側，插件無需改動）

`bridge/` 現在能真正執行 FongMi/CatVod 的 Spider 契約，並把結果轉成 CMS JSON：

- **Java**：`SpiderHostRunner.java` 用 JVM 反射調用 `com.github.catvod.spider.<Class>` 的 `init / homeVideoContent / searchContent / detailContent / playerContent`。標準 `.class` 的 spider jar 放進 `bridge/jars/`；白名單類需在 `sources.json` 顯式登記。Spider 與 CMS 源**並行**搜索，避免 JVM 啟動時間累加把插件請求拖超時；插件對 `127.0.0.1` / 內網 bridge 的單請求與引擎預算放寬到 10 秒。
- **播放邊界**：二級取址與播放地址補全已由 v2.2 實作；上游站點本身已失效或需要登入態的 Spider（如 `Bili` 需 cookie、`Wogg`/`SeedHub` 詳情異常）仍無法播放，建議從白名單移除。
- **Python**：`.py` spider 由 `spiders/_runner.py` 在子進程執行，附最小 `catvod` shim；內置 `spiders/demo.py` 只作自檢，不污染正常搜索。
- **JS**：`_js_runner.js` 已備好，但伺服器沒有 Node/QuickJS，狀態會標記為不可用；裝了 Node 後在 `sources.json` 登記即可啓用。
- **Android dex**：`csp_*Guard` 這類 dex 格式 jar 需要 `DexClassLoader` 與 Android API，Linux JVM 跑不了，目前**只統計、不執行**（菜妮絲配置的 36 個即屬此類）；`remote_spider_url` 預留給日後對接 Android 上的 FongMi TV。

統一 `home / search / detail / play` 契約，支持 `flag + id` 兩級取址；新端點 `?ac=spider_test`（自檢）、`?ac=play&provider=&flag=&id=`。所有第三方 Spider 都是**顯式白名單 + 子進程隔離 + 超時**，可在 `sources.json` 把 `spider_host.enabled` 或 `java.enabled` 設為 `false` 一鍵關閉。

### v2.0 播放容錯與家庭服務器橋接

- 播放域名也納入健康緩存：線路載入前會並發探測首集地址，403/超時的域名冷卻 30 分鐘，線路標記為不可用；「清理緩存」可立即重置。
- `/share/xxx` 這類需要二級解析的間接地址不再混進可播列表。
- 新增常駐 service：播放失敗時自動切換到其它線路（最多 3 次、優先不同域名），並記住失敗域名，下次自動跳過。
- `bridge/duola_bridge.py` 是給家庭服務器用的適配服務，只監聽 `127.0.0.1:8765`，把多個 CMS 資源站聚合成標準 `/api.php/provide/vod/` JSON；也支援讀取 FongMi/TVBox 配置，自動納入其中的 type 0/1 CMS 站點，type 3 spider 站點只統計、跳過（需要 Spider Host，本輪未實現）。
- 在插件【額外搜索接口】中填 `家里桥接=direct:http://127.0.0.1:8765/api.php/provide/vod/` 即可接入。不要把無鑒權的適配服務暴露到公網；外網需求請走 SSH 隧道。

### v1.9 播放改動

- 多線路影片會**先選播放源（線路），再選劇集**；單線路自動跳過選擇。
- 播放列表只含當前線路，連續播放不會跳到另一條線路。
- `.m3u8?token=...`、`.mpd`、無後綴播放地址都能識別；mp4/flv 不再被強制當成 HLS。
- 若已安裝 `inputstream.adaptive`，HLS/DASH 會走 adaptive 並攜帶 UA / Referer / Origin；未安裝則自動回退直鏈播放（在設定中可選，插件已聲明為可選依賴）。

### 如何啟用雲端引擎接口？

爲了方便测试练习，插件默認的雲端引擎并沒有開啓，開啓后它將支持獲取基於github上搜索引擎接口列表，使插件擁有更多的搜索渠道。

>  云端引擎的接口数据格式如下（即：代碼中[ADDON_api]變量對應返回的内容）：

```json

{
  "code": 1, // API状态：1为可用
  "message": "success", // API状态描述
  "expires_in": 3600, // API有效期：单位为秒
  "readme": "hello", // API简介：[1.7]版本起开始弃用!
  "notice": "5qyi6L+O5YWJ5Li05Li76aG177yaaHR0cHM6Ly9tYWxpbWFsaWFvLmdpdGh1Yi5pby9rb2RpLw==", // API通知消息：要求内容采用base64编码
  "client": "1.7.0", // 版本号
  "data": {
    "time": "2024-06-07 17:09:02", // 数据更新时间
    "note": "", // 数据更新备注
    "list": [ // 数据源列表
      {
        "name": "1.飞速资源", // 数据源名称
        "status": 1, // 数据源状态：1为可用
        "api_url": "aHR0cHM6Ly93d3cuZmVpc3V6eWFwaS5jb20vYXBpLnBocC9wcm92aWRlL3ZvZC8=" // 数据源接口地址：要求内容采用base64编码
      },
      {
        "name": "2.无极网络", // 数据源名称
        "status": 1, // 数据源状态：1为可用
        "api_url": "aHR0cHM6Ly9hcGkud3VqaW5hcGkubWUvYXBpLnBocC9wcm92aWRlL3ZvZC8=" // 数据源接口地址：要求内容采用base64编码
      }
    ]
  }
}

```


### 什麽是資源站數據接口？

> 當前程序是完全基於目前互聯網第三方的資源站的Json數據接口進行獲取内容並構造到kodi中的，那麽什麽是接口呢，又如何尋找它？

請您Google下面这些关键词，并寻找这些第三方站点中的json api接口。資源站json接口通常非常类似，
例如我们找到了他们的json接口是这样举例的：https://api.123.com/api.php/provide/vod/?ac=list
那么其实它的接口就是：<u>https://api.123.com/api.php/provide/vod/</u>

其中问好后面的都是参数，例如?ac=list是一个参数。

```

資源站、采集站、资源帮助中心、
/api.php/provide/vod/
/cjapi/mc10/vod/json.html
/inc/api_mac10.php

```

通過搜索以上關鍵詞，應該可以找到他們。
