import {cookies} from 'next/headers';
import {timingSafeEqual} from 'node:crypto';
export const dynamic='force-dynamic';
export async function GET(request:Request,{params}:{params:Promise<{path:string[]}>}){
 const token=process.env.DSH_MANAGEMENT_TOKEN;const cookie=(await cookies()).get('local_management')?.value;
 if(!token||!cookie||Buffer.byteLength(cookie)!==Buffer.byteLength(token)||!timingSafeEqual(Buffer.from(cookie),Buffer.from(token)))return Response.json({error:'unauthorized'},{status:401});
 const host=request.headers.get('host');if(!['127.0.0.1:3002','localhost:3002'].includes(host||''))return Response.json({error:'host_denied'},{status:403});
 const path=(await params).path.join('/');
 if(!/^(overview|config|events(?:\/[1-9][0-9]{0,18})?)$/.test(path))return Response.json({error:'not_found'},{status:404});
 try{
  // 地址固定；浏览器不能指定上游URL。超时不重试、不触发模型调用。
  const result=await fetch('http://127.0.0.1:8788/v1/management/'+path+new URL(request.url).search,{headers:{Authorization:'Bearer '+token},cache:'no-store',signal:AbortSignal.timeout(2000)});
  return new Response(await result.text(),{status:result.status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
 }catch{return Response.json({error:'management_unavailable'},{status:503});}
}
