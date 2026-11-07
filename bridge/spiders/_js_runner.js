// QuickJS/Node Spider 运行器（检测到 Node 时才会启用）。
// 输出与 Java/Python runner 一致：{"ok":bool,"data":"<json字符串>","error":str}
const path = require('path');

function emit(ok, data, error) {
  process.stdout.write(JSON.stringify({
    ok: ok,
    data: JSON.stringify(data || {}),
    error: error || ''
  }) + '\n');
}

try {
  const [, , file, action, ext, ...params] = process.argv;
  const mod = require(path.resolve(file));
  const Ctor = mod.default || mod.Spider || mod;
  const spider = typeof Ctor === 'function' ? new Ctor() : Ctor;
  if (typeof spider.init === 'function') spider.init(ext);

  let raw;
  if (action === 'home') raw = spider.homeVideoContent();
  else if (action === 'search') raw = spider.searchContent(params[0], true);
  else if (action === 'detail') raw = spider.detailContent([params[0]]);
  else if (action === 'play') raw = spider.playerContent(params[0], params[1], []);
  else throw new Error('unknown action: ' + action);

  const data = typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {});
  emit(true, data, '');
} catch (e) {
  emit(false, {}, String(e));
}
