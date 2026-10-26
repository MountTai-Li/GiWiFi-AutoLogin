/* app-core.js —— 认证工具箱的应用逻辑（页面与测试脚本共用，保证测试即所发布）
 * 依赖：aes-core.js（全局 GiAES）
 */
var GiApp = (function (GiAES) {
  'use strict';

  /* 与页面运行中的实现完全同源：序列化同一段函数源码，用于生成自包含脚本 */
  function coreSource() {
    return 'var SBOX=' + JSON.stringify(GiAES.SBOX) + ';\n'
         + 'var RCON=' + JSON.stringify(GiAES.RCON) + ';\n'
         + [GiAES.xt, GiAES.gmul, GiAES.expandKey, GiAES.encBlock,
            GiAES.utf8, GiAES.zeroPad, GiAES.encryptCbcZeroPad, GiAES.b64]
           .map(function (f) { return f.toString(); }).join('\n\n')
         + '\nvar KEY=' + JSON.stringify(GiAES.KEY) + ';';
  }

  /* 协议直连主流程：在门户页上下文里运行（location/fetch/DOMParser 由页面提供） */
  function protocolMain(USER, PASS) {
    var origin = location.origin;
    fetch(origin + '/gportal/web/login', { credentials: 'include' })
      .then(function (r) {
        if (!r.ok) throw new Error('登录页 HTTP ' + r.status + '（请确认当前就打开在门户页上）');
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var form = doc.getElementById('frmLogin');
        if (!form) throw new Error('登录页里没有 frmLogin 表单（可能已经在线，或门户改版）');
        var fields = [], iv = '';
        var els = form.querySelectorAll('input,select,textarea');
        for (var i = 0; i < els.length; i++) {
          var el = els[i];
          var t = (el.getAttribute('type') || 'text').toLowerCase();
          if (['button', 'submit', 'reset', 'image', 'file'].indexOf(t) >= 0) continue;
          if ((t === 'checkbox' || t === 'radio') && !el.hasAttribute('checked')) continue;
          var n = el.getAttribute('name');
          if (!n) continue;
          var v = el.getAttribute('value') || '';
          if (n === 'name') v = USER;
          if (n === 'password') v = PASS;
          if (n === 'iv') iv = v;
          fields.push([n, v]);
        }
        if (!iv || iv.length !== 16) throw new Error('iv 异常（' + iv + '）：你可能已经在线，无需认证');
        var plain = fields.map(function (kv) {
          return encodeURIComponent(kv[0]) + '=' + encodeURIComponent(kv[1]);
        }).join('&');
        var data = b64(encryptCbcZeroPad(plain, KEY, iv));
        return fetch(origin + '/gportal/web/authLogin?round=' + Math.floor(Math.random() * 1001), {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest'
          },
          body: 'data=' + encodeURIComponent(data) + '&iv=' + encodeURIComponent(iv)
        }).then(function (r) { return r.text(); });
      })
      .then(function (t) {
        var j = null;
        try { j = JSON.parse(t); } catch (e) { }
        if (j && (String(j.status) === '1' || String(j.status) === '200' || j.success === true)) {
          alert('✅ 认证成功' + ((j.info || j.msg) ? '：' + (j.info || j.msg) : ''));
        } else {
          alert('❌ ' + (j && (j.info || j.msg) ? (j.info || j.msg) : '服务端响应：' + String(t).slice(0, 200)));
        }
      })
      .catch(function (e) { alert('❌ ' + (e && e.message ? e.message : String(e))); });
  }

  /* 表单填充兼容模式 */
  function formMain(USER, PASS) {
    var f = document.getElementById('frmLogin') || document.querySelector('form');
    if (!f) { alert('❌ 未找到登录表单（请确认当前就打开在门户登录页）'); return; }
    var u = f.querySelector('[name=name]'), p = f.querySelector('[name=password]');
    if (!u || !p) { alert('❌ 表单里没有账号/密码输入框'); return; }
    u.value = USER;
    p.value = PASS;
    if (f.requestSubmit) { f.requestSubmit(); } else { f.submit(); }
  }

  /* 生成自包含脚本（protocol / form 两种模式） */
  function buildScript(mode, user, pass) {
    var head = '/* 由「GiWiFi 校园网认证工具箱」生成 · 仅用于本人账号 · 凭据不经过任何服务器 */\n'
             + '/* 用法：在校园网内打开门户登录页 → F12 → Console → 粘贴回车 */\n'
             + '(function(){\n'
             + 'var USER = ' + JSON.stringify(user) + ';\n'
             + 'var PASS = ' + JSON.stringify(pass) + ';\n';
    if (mode === 'protocol') {
      return head + coreSource() + '\n' + protocolMain.toString() + '\nprotocolMain(USER, PASS);\n})();';
    }
    return head + formMain.toString() + '\nformMain(USER, PASS);\n})();';
  }

  /* 本地 CBC 演示（含中间步骤，供协议可视化） */
  function demoCbc(plain, keyStr, ivStr) {
    var raw = GiAES.zeroPad(GiAES.utf8(plain));
    var rks = GiAES.expandKey(GiAES.utf8(keyStr));
    var iv = GiAES.utf8(ivStr);
    var rows = [], out = [];
    for (var off = 0; off < raw.length; off += 16) {
      var x = [], i, c;
      for (i = 0; i < 16; i++) x.push(raw[off + i] ^ iv[i]);
      c = GiAES.encBlock(x.slice(), rks);
      rows.push({ plain: GiAES.bytesToHex(raw.slice(off, off + 16)),
                  xor: GiAES.bytesToHex(x),
                  cipher: GiAES.bytesToHex(c) });
      out = out.concat(c);
      iv = c;
    }
    return { rows: rows, b64: GiAES.b64(out), padded: raw.length };
  }

  return {
    coreSource: coreSource, protocolMain: protocolMain, formMain: formMain,
    buildScript: buildScript, demoCbc: demoCbc
  };
})(typeof GiAES !== 'undefined' ? GiAES : require('./aes-core.js'));

if (typeof module !== 'undefined' && module.exports) module.exports = GiApp;
