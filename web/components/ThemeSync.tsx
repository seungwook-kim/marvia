'use client';
/** 저장된 화면 색을 다시 반영하고, "시스템 설정 따르기"면 운영체제 설정이 바뀔 때 따라가게 한다 (lib/theme.ts). 그리는 것은 없다 */
import {useEffect} from 'react';
import {applyTheme, readThemePref} from '@/lib/theme';

export function ThemeSync() {
  useEffect(() => { applyTheme(readThemePref(), false); }, []);
  return null;
}
