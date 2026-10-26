/* e2e.test.js —— 端到端验证：模拟门户页，真实执行「生成的认证脚本」
 * 运行：node web/test/e2e.test.js
 * 1) 协议直连模式：GET 登录页 → 解析表单 → AES 加密 → POST，核对提交密文
 * 2) 表单填充模式：核对表单赋值与提交调用
 */
'use strict';
const path = require('path');
const GiAES = require(path.join(__dirname, '..', 'aes-core.js'));
const GiApp = require(path.join(__dirname, '..', 'app-core.js'));

const PORTAL = 'http://192.0.2.1';
const IV = '4e73a7dcdd4afd3b';
const USER = '13800138000';
const PASS = 'p@ss w0rd 空格测试';

const LOGIN_HTML = '<html><body><form id="frmLogin">'
  + '<input type="hidden" name="sign" value="SIGNVALUE">'
  + '<input type="hidden" name="iv" value="' + IV + '">'
  + '<input type="hidden" name="nasName" value="NASNAME">'
  + '<input type="hidden" name="userIp" value="10.0.0.9">'
  + '<input type="text" name="name" value="">'
  + '<input type="password" name="password" value="">'
  + '<button type="submit">login</button>'
  + '</form></body></html>';

let posted = null;
let alerts = [];

global.location = { origin: PORTAL };
global.alert = function (m) { alerts.push(String(m)); };
global.DOMParser = function () {
  this.parseFromString = function (html) {
    const els = [];
    const re = /<(input|select|textarea)\b([^>]*)>/gi;
    let m;
    while ((m = re.exec(html))) {
      const attrs = m[2];
      const attr = (k) => { const r = new RegExp(k + '="([^"]*)"', 'i').exec(attrs); return r ? r[1] : null; };
      const t = (attr('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image', 'file'].indexOf(t) >= 0) continue;
      if ((t === 'checkbox' || t === 'radio') && !/\bchecked\b/i.test(attrs)) continue;
      const name = attr('name');
      if (!name) continue;
      els.push({ getAttribute: (k) => (k === 'type' ? t : (k === 'name' ? name : (k === 'value' ? (attr('value') || '') : null))),
                 hasAttribute: (k) => (k === 'checked' ? /\bchecked\b/i.test(attrs) : false) });
    }
    return { getElementById: (id) => (id === 'frmLogin' ? { querySelectorAll: () => els } : null) };
  };
};

let loginGets = 0;
global.fetch = function (url, opts) {
  url = String(url);
  if (url.indexOf('/gportal/web/login') >= 0) {
    loginGets++;
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(LOGIN_HTML) });
  }
  if (url.indexOf('/gportal/web/authLogin') >= 0) {
    posted = { url: url, body: opts.body };
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{"status":"1","info":"认证成功"}') });
  }
  return Promise.reject(new Error('unexpected url: ' + url));
};

function expect(name, cond) {
  console.log((cond ? 'PASS' : 'FAIL') + '  ' + name);
  if (!cond) process.exitCode = 1;
}

const script = GiApp.buildScript('protocol', USER, PASS);
expect('生成脚本包含内嵌 AES 核心（SBOX）', script.indexOf('[99,124') >= 0);
expect('生成脚本包含账号（JSON 转义）', script.indexOf(JSON.stringify(USER)) >= 0);
eval(script);
setTimeout(function () {
  expect('登录页被拉取', loginGets === 1);
  expect('authLogin 被提交', !!posted);
  if (posted) {
    const params = new URLSearchParams(posted.body);
    const data = params.get('data');
    const iv = params.get('iv');
    expect('提交的 iv 与登录页一致', iv === IV);
    const plain = 'sign=SIGNVALUE&iv=' + IV + '&nasName=NASNAME&userIp=10.0.0.9'
                + '&name=' + encodeURIComponent(USER) + '&password=' + encodeURIComponent(PASS);
    const want = GiAES.b64(GiAES.encryptCbcZeroPad(plain, '1234567887654321', IV));
    expect('提交密文与参考实现一致', data === want);
    if (data !== want) { console.log('  实际:', data); console.log('  期望:', want); }
  }
  expect('弹窗提示认证成功', alerts.some(a => a.indexOf('认证成功') >= 0));

  let filled = {}, submitted = false;
  global.document = {
    getElementById: (id) => (id === 'frmLogin' ? {
      querySelector: (sel) => ({
        set value(v) { filled[sel] = v; },
        get value() { return filled[sel]; }
      }),
      requestSubmit: () => { submitted = true; },
      submit: () => { submitted = true; }
    } : null),
    querySelector: () => null
  };
  alerts.length = 0;
  const script2 = GiApp.buildScript('form', USER, PASS);
  eval(script2);
  setTimeout(function () {
    expect('表单填充：账号已填', filled['[name=name]'] === USER);
    expect('表单填充：密码已填', filled['[name=password]'] === PASS);
    expect('表单填充：表单已提交', submitted === true);
    console.log(process.exitCode ? '== 存在失败 ==' : '== 端到端全部通过 ==');
  }, 80);
}, 120);
