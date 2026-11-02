// 本机测试管理边界；平台认证另接。环境值只由可信部署者配置，浏览器不能指定上游。
export function allowedHost(host:string|null):boolean{
 const port=process.env.DSH_MANAGEMENT_PORT||'3002';
 if(!['3002','13002'].includes(port))return false;
 return [`127.0.0.1:${port}`,`localhost:${port}`].includes(host||'');
}
export function sameOrigin(request:Request):boolean{
 const host=request.headers.get('host'),origin=request.headers.get('origin');
 if(!allowedHost(host)||!origin)return false;
 try{return new URL(origin).host===host;}catch{return false;}
}
export function backendBase():string{
 const value=process.env.DSH_MANAGEMENT_BASE_URL||'http://127.0.0.1:8788';
 if(!['http://127.0.0.1:8788','http://agent:18788'].includes(value))throw new Error('invalid_management_backend');
 return value;
}
