'use client';
/** 운영자 전용 화면의 문 (docs/specs/operator-admin.md). 사이드바는 운영자에게만 탭을 보여 주지만,
 * 주소로 바로 들어오는 경우를 여기서 막는다. 데이터 권한은 서버가 따로 확인한다 (화면 숨김은 편의).
 * 체험 모드(서버 없음)는 운영자가 아니라 메뉴에 안 보이지만, 주소를 직접 열면 가공 예시로 화면을 미리 볼 수 있다(previewOk).
 * 서버를 부르지 않고 바꿀 수 있는 것도 없어서다. 화면 접근성 검사(scripts/verify.mjs)도 이 길로 관리·지표 화면을 본다 (10-08 QA G10).
 * 데모 둘러보기(tour)는 일반 사용자 체험이라 미리보기도 막는다.
 */
import type {ReactNode} from 'react';
import {ShieldUser} from 'lucide-react';
import {useApp} from './AppProvider';
import {isLive, tour} from '@/lib/api';

/** 체험 모드에서 운영자 화면을 가공 예시로 미리 볼 수 있는지. 서버가 있는 실제 모드와 둘러보기에서는 false */
export function previewOk() { return !isLive && !tour; }

/** 운영자가 아니라는 안내. AdminPage도 같은 문구를 쓴다 */
export function OperatorDenied() {
  return <div className="page"><div className="empty-state admin-denied"><ShieldUser size={34} strokeWidth={1.2}/><h2>운영자만 볼 수 있는 화면이에요.</h2><p>권한이 필요하면 기존 운영자에게 등록을 요청해 주세요.</p></div></div>;
}

export function OperatorOnly({children}: {children: ReactNode}) {
  const {isOperator, authReady, meReady} = useApp();
  if (!authReady || !meReady) {
    return <div className="page"><div className="initial-loading" role="status"><div className="loading-orbit"/><strong>권한을 확인하고 있어요.</strong></div></div>;
  }
  if (!isOperator && !previewOk()) return <OperatorDenied/>;
  return <>{children}</>;
}
