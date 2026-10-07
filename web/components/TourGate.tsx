'use client';
/** 데모 둘러보기 켜기 (lib/tour.ts). 서버가 그린 첫 화면은 늘 보통 모드라, 브라우저에서 켜졌는지 확인한 뒤
 * 데모로 바꾸고 아래 앱 전체를 한 번 다시 그린다(key). 그래야 서버 그림과 첫 그림이 어긋나지 않는다(hydration).
 * 데모에서는 서버 호출·로그인을 끈다: isLive=false, authEnabled=false */
import {useEffect, useState, type ReactNode} from 'react';
import {startTour} from '@/lib/api';
import {disableAuth} from '@/lib/auth';
import {tourRequested} from '@/lib/tour';

export function TourGate({children}: {children: ReactNode}) {
  const [key, setKey] = useState('live');
  useEffect(() => {
    if (!tourRequested()) return;
    startTour(); disableAuth();
    document.documentElement.dataset.tour = 'on';
    setKey('tour');
  }, []);
  return <div key={key} style={{display: 'contents'}}>{children}</div>;
}
