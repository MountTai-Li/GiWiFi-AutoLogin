/* aes-core.js —— GiWiFi 门户认证加密核心（Web 版）
 * 从桌面版 aes128.py 1:1 移植：AES-128-CBC + ZeroPadding → Base64
 * 对应门户登录页 CryptoJS 行为：
 *   CryptoJS.AES.encrypt(data, Utf8.parse('1234567887654321'),
 *                        {iv: Utf8.parse(iv), mode: CBC, padding: ZeroPadding}).toString()
 * 说明：WebCrypto 无法关闭 PKCS#7 填充，ZeroPadding 语义必须自己实现，
 *       因此这里内嵌一份精简 AES 核心（与桌面版逐行同构）。
 * 零第三方依赖；可在浏览器与 Node.js 双端运行。
 */
var GiAES = (function () {
  'use strict';

  var SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
  ];
  var RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36];

  function xt(a) { a <<= 1; if (a & 0x100) a = (a ^ 0x1b) & 0xff; return a; }

  function gmul(a, b) {           // GF(2^8) 乘法
    var p = 0, i;
    for (i = 0; i < 8; i++) {
      if (b & 1) p ^= a;
      b >>= 1;
      a = xt(a);
    }
    return p & 0xff;
  }

  function expandKey(key) {       // key: 16 字节 → 11 组轮密钥（每组 16 字节）
    var w = [], i, j, t, nw, r, c, rks, rk;
    for (i = 0; i < 4; i++) w.push([key[i*4], key[i*4+1], key[i*4+2], key[i*4+3]]);
    for (i = 4; i < 44; i++) {
      t = w[i-1].slice();
      if (i % 4 === 0) {
        t = t.slice(1).concat(t.slice(0, 1));            // RotWord
        for (j = 0; j < 4; j++) t[j] = SBOX[t[j]];        // SubWord
        t[0] ^= RCON[i/4 - 1];
      }
      nw = [];
      for (j = 0; j < 4; j++) nw.push(w[i-4][j] ^ t[j]);
      w.push(nw);
    }
    rks = [];
    for (r = 0; r < 11; r++) {
      rk = [];
      for (c = 0; c < 4; c++) rk = rk.concat(w[r*4 + c]);
      rks.push(rk);
    }
    return rks;
  }

  function encBlock(s, rks) {     // 加密单个 16 字节块（状态按列优先：s[c*4+r]）
    var o = [], r, c, a, rnd, i;
    for (i = 0; i < 16; i++) s[i] ^= rks[0][i];            // AddRoundKey
    for (rnd = 1; rnd < 10; rnd++) {
      for (i = 0; i < 16; i++) s[i] = SBOX[s[i]];           // SubBytes
      o = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0];
      for (r = 0; r < 4; r++) for (c = 0; c < 4; c++) o[c*4+r] = s[((c+r)%4)*4+r];  // ShiftRows
      s = o;
      o = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0];
      for (c = 0; c < 4; c++) {                              // MixColumns
        a = s.slice(c*4, c*4+4);
        o[c*4+0] = gmul(a[0],2) ^ gmul(a[1],3) ^ a[2] ^ a[3];
        o[c*4+1] = a[0] ^ gmul(a[1],2) ^ gmul(a[2],3) ^ a[3];
        o[c*4+2] = a[0] ^ a[1] ^ gmul(a[2],2) ^ gmul(a[3],3);
        o[c*4+3] = gmul(a[0],3) ^ a[1] ^ a[2] ^ gmul(a[3],2);
      }
      s = o;
      for (i = 0; i < 16; i++) s[i] ^= rks[rnd][i];         // AddRoundKey
    }
    for (i = 0; i < 16; i++) s[i] = SBOX[s[i]];
    o = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0];
    for (r = 0; r < 4; r++) for (c = 0; c < 4; c++) o[c*4+r] = s[((c+r)%4)*4+r];
    for (i = 0; i < 16; i++) o[i] ^= rks[10][i];
    return o;
  }

  function utf8(str) {            // str → UTF-8 字节数组
    return Array.prototype.slice.call(new TextEncoder().encode(str));
  }

  function zeroPad(bytes) {       // ZeroPadding：不对齐才补 0x00（与 CryptoJS 一致）
    var rem = bytes.length % 16;
    if (!rem) return bytes;
    var o = new Array(bytes.length + 16 - rem);
    for (var i = 0; i < bytes.length; i++) o[i] = bytes[i];
    for (var j = bytes.length; j < o.length; j++) o[j] = 0;
    return o;
  }

  function encryptCbcZeroPad(plain, keyStr, ivStr) {
    var raw = zeroPad(utf8(plain));
    var rks = expandKey(utf8(keyStr));
    var iv = utf8(ivStr);
    var out = [], off, i, blk, c;
    for (off = 0; off < raw.length; off += 16) {
      blk = [];
      for (i = 0; i < 16; i++) blk.push(raw[off+i] ^ iv[i]);
      c = encBlock(blk, rks);
      out = out.concat(c);
      iv = c;
    }
    return out;
  }

  function b64(bytes) {           // 字节数组 → Base64
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function bytesToHex(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += ('0' + bytes[i].toString(16)).slice(-2);
    return s;
  }

  return {
    SBOX: SBOX, RCON: RCON,
    xt: xt, gmul: gmul, expandKey: expandKey, encBlock: encBlock,
    utf8: utf8, zeroPad: zeroPad, encryptCbcZeroPad: encryptCbcZeroPad,
    b64: b64, bytesToHex: bytesToHex,
    KEY: '1234567887654321'
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = GiAES;
