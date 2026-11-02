import type {NextConfig} from 'next';
// 独立容器只携带构建产物；本机开发和现有端口保持可用。
const config:NextConfig={output:'standalone'};
export default config;
