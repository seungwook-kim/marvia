// Next.js 설정. 글꼴·지도 자료를 로컬로 번들해 외부 서비스 설정이 없다.
import type { NextConfig } from 'next';
const config: NextConfig = { reactStrictMode: true, agentRules: false, devIndicators: false };
export default config;
