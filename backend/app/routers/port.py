"""GET /api/port/busan/traffic: 부산항 최근 입항 추세 카드 (IMF PortWatch).

공개 집계 자료라 로그인 없이 연다. 원천 호출은 services/port_traffic.py가 캐시한다.
판정과 무관한 참고 맥락이라 화물 판정 응답에는 넣지 않는다.
참고 카드 하나 때문에 화면에 오류가 뜨지 않도록 실패해도 200이고, 본문 status(ok / partial / unavailable)로
구분한다 (SDD 8절 partial 경로).
"""
from fastapi import APIRouter

from backend.app.services import port_traffic

router = APIRouter(prefix="/api/port", tags=["port"])


@router.get("/busan/traffic")
def busan_traffic() -> dict:
    """부산항 컨테이너선 입항 최근 7일 평균과 28일 평균 비교. 동기 함수라 스레드에서 돌아 원천 호출이 막혀도 이벤트 루프는 안 막힌다."""
    return port_traffic.get_busan_traffic()
