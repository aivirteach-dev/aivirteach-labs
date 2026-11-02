// 仅从只读秘密文件加载本机管理能力；不输出、不写入浏览器构建变量。
const fs=require('node:fs');
const token=fs.readFileSync('/run/secrets/management_token','utf8').trim();
if(!/^[A-Za-z0-9_-]{32,128}$/.test(token))throw new Error('management_secret_rejected');
process.env.DSH_MANAGEMENT_TOKEN=token;
require('./server.js');
