"""GET /api/port/busan/traffic: 부산항 최근 입항 추세 카드 (IMF PortWatch, 10-08 결정).

로그인 없이 연다. 공개 집계 자료이고 개인 정보가 없으며, 서버가 12시간에 한 번만 위로 부른다
(services/port_traffic.py 캐시). 판정과 무관한 참고 맥락이라 화물 판정 응답에는 넣지 않는다.

실패해도 200이다. 본문의 status(ok / partial / unavailable)로 화면이 알아본다.
참고 카드 하나 때문에 화면에 오류가 뜨지 않게 하려는 것이다 (SDD 8절 partial 경로).
"""
from fastapi import APIRouter

from backend.app.services import port_traffic

router = APIRouter(prefix="/api/port", tags=["port"])


@router.get("/busan/traffic")
def busan_traffic() -> dict:
    """부산항 컨테이너선 입항 최근 7일 평균과 28일 평균 비교. 동기 함수라 FastAPI가 스레드에서 돌린다(위 호출이 막혀도 이벤트 루프는 안 막힌다)."""
    return port_traffic.get_busan_traffic()
