/** 루트 레이아웃. 글꼴을 번들해서 폰트 CDN 없이 동작한다.
 * 본문은 Noto Sans KR, 고정폭은 B/L 번호용 IBM Plex Mono만 쓴다. */
import type {Metadata} from 'next';
import '@fontsource-variable/noto-sans-kr';
import '@fontsource/ibm-plex-mono/400.css';
import 'leaflet/dist/leaflet.css';
// 지도 바탕(MapLibre) 스타일. 한 번만 불러온다 (lib/basemap.ts)
import 'maplibre-gl/dist/maplibre-gl.css';
import './globals.css';
// 화면 영역별 CSS는 globals.css 뒤에 이 순서로 불러온다 (뒤에 오는 파일이 앞의 같은 선택자를 덮는다)
import './styles/chat.css';
import './styles/detail.css';
import './styles/compare-vessel.css';
import './styles/cargo-sheet.css';
import './styles/watch.css';
import './styles/settings.css';
import './styles/admin.css';
import './styles/entry.css';
import './styles/landing.css';
import './styles/features.css';
import './styles/enterprise.css';
import './styles/today.css';
import {AppProvider} from '@/components/AppProvider';
import {Shell} from '@/components/Shell';
import {ThemeSync} from '@/components/ThemeSync';
import {TourGate} from '@/components/TourGate';
import {THEME_BOOT_SCRIPT} from '@/lib/theme';
export const metadata:Metadata={title:'Marvia',description:'B/L 하나로 내 화물이 제때 오는지 판정하고, 왜 그런지 근거까지 보여 드려요. 예시 데이터로 체험해 볼 수 있어요.'};
// 화면 색(data-theme)은 그리기 전에 작은 스크립트가 먼저 넣는다(깜빡임 방지). 서버가 그린 html에는 없는 속성이라 suppressHydrationWarning
// data-scroll-behavior: globals.css가 html에 scroll-behavior:smooth를 둔다. Next.js 16은 이 표시가 있어야 화면을 옮길 때 부드러운 스크롤을 잠시 끄고 경고도 내지 않는다 (10-08 QA G13)
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="ko" data-scroll-behavior="smooth" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{__html:THEME_BOOT_SCRIPT}}/></head><body><ThemeSync/><TourGate><AppProvider><Shell>{children}</Shell></AppProvider></TourGate></body></html>;}
