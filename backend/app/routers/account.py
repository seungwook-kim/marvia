"""DELETE /api/account 회원 탈퇴.

로그인 확인 후 services.account.withdraw를 부르고 결과를 HTTP 응답으로 바꾼다.
지우는 순서(기록 먼저, 계정 나중)와 그 이유는 services/account.py에 있다.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.auth import current_user_id
from backend.app.observability.logging import log_failure
from backend.app.services.account import WithdrawError, withdraw

router = APIRouter(prefix="/api", tags=["account"])

RETRY_LATER = "탈퇴를 끝내지 못했습니다. 잠시 뒤 다시 시도하세요"
# 실패 단계별 문구 (services/account.WithdrawError.stage). 어디까지 지워졌는지 사실대로 알린다 (10-08 QA A8)
WITHDRAW_MESSAGES = {
    "account_records": "기록을 지우지 못해 아무것도 바뀌지 않았습니다. 잠시 뒤 다시 시도하세요",
    "account_delete": "기록은 모두 지웠지만 로그인 계정 삭제가 끝나지 않았습니다. 탈퇴를 다시 누르면 마저 끝납니다",
}


class AccountDeleteResponse(BaseModel):
    """탈퇴 결과. 지우는 범위는 services/account.py 머리 주석 (내 기록 삭제보다 넓다: 회원 정보·카카오·배 지켜보기·사용량까지)."""

    deleted_records: int  # 이번 요청에서 지운 조회 기록 수 (개인정보보호법 제36조 취지로 결과를 알린다)
    account_deleted: bool  # 여기까지 오면 항상 true. 실패는 503으로 돌려준다
    # 앞선 탈퇴 시도가 기록을 이미 지웠고(그때 계정 삭제만 실패) 이번에 마저 끝냈다. 이때 deleted_records는 0이 맞다 (10-08 QA A8)
    resumed: bool = False


@router.delete("/account", response_model=AccountDeleteResponse)
def delete_my_account(user_id: Annotated[str | None, Depends(current_user_id)] = None) -> AccountDeleteResponse:
    """로그인한 사용자의 기록과 계정을 지운다. 어느 단계든 실패하면 503이고, 다시 누르면 이어서 끝난다.
    정지된 계정, 앞선 시도로 탈퇴 표시가 남은 계정도 여기는 통과한다 (auth.DELETION_ROUTES, 10-08 QA A4)."""
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인한 사용자만 탈퇴할 수 있습니다",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        result = withdraw(user_id)
    except WithdrawError as e:
        log_failure(e.stage, e.reason)
        raise HTTPException(status_code=503, detail=WITHDRAW_MESSAGES.get(e.stage, RETRY_LATER))
    return AccountDeleteResponse(deleted_records=result.deleted_records, account_deleted=True, resumed=result.resumed)
