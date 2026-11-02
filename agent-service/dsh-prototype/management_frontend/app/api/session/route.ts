import {cookies} from 'next/headers';
import {sameOrigin} from '../../lib/local-management';
// 本机原型以loopback访问作为管理身份边界，不等于平台管理员认证。
export async function POST(request:Request){
 if(!sameOrigin(request))return Response.json({error:'origin_denied'},{status:403});
 const token=process.env.DSH_MANAGEMENT_TOKEN;if(!token)return Response.json({error:'not_configured'},{status:503});
 (await cookies()).set('local_management',token,{httpOnly:true,sameSite:'strict',path:'/',maxAge:3600});
 return Response.json({scope:'仅本机原型；未来接平台管理员认证'});
}
export async function DELETE(request:Request){if(!sameOrigin(request))return Response.json({error:'origin_denied'},{status:403});(await cookies()).delete('local_management');return Response.json({ok:true});}
