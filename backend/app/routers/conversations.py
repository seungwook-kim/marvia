"""대화 목록 창구 (ChatGPT·Claude식, 10-06 정운 결정, services/conversations.py, migrations/018).

GET /api/conversations         내 대화 목록 (최근순 50건)
GET /api/conversations/{id}    대화 하나: 말풍선, 마지막 판정(context·result)
DELETE /api/conversations/{id} 대화 하나 지우기
POST /api/conversations/{id}/attachment-turns  채팅 없이 끝난 사진 첨부 턴 저장 (10-08)

모두 로그인이 필요하다 (routers/history.py와 같은 401). DB를 못 쓰면 503이다: 목록이 비어 보이는 것과 못 읽은 것을 구분한다.
남의 대화 번호를 넣어도 사용자 번호로 거르므로 "없음"(404)과 같게 답한다. 저장은 여기가 아니라 routers/chat.py가 답 뒤에 한다.
"""
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.auth import current_user_id
from backend.app.db import connect
from backend.app.schemas.agent import SESSION_ID_MAX
from backend.app.services.chat import MESSAGE_MAX
from backend.app.observability.logging import log_failure
from backend.app.services import conversations as svc

router = APIRouter(prefix="/api", tags=["conversations"])

UNAVAILABLE = "대화 목록을 불러오지 못했습니다. 잠시 뒤 다시 시도하세요"


class ConversationItem(BaseModel):
    id: str
    title: str
    updated_at: str  # ISO 8601


class ConversationList(BaseModel):
    conversations: list[ConversationItem]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    extra: dict | None = None  # 답 말풍선의 화면 자료 (kind·vessels·map·cargos·watch_offer·watch_set 등). 사용자 줄은 null


class ConversationDetail(BaseModel):
    id: str
    title: str
    messages: list[Message]
    context: dict | None = None  # 마지막 AgentRequest
    result: dict | None = None   # 마지막 AgentResponse


class Deleted(BaseModel):
    deleted: bool


def _need_user(user_id: str | None) -> str:
    """로그인한 사용자 번호. 없으면 401 (로그인 꺼진 환경 포함). DB가 없으면 503."""
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인한 사용자만 대화 목록을 쓸 수 있습니다",
                            headers={"WWW-Authenticate": "Bearer"})
    if not os.environ.get("DATABASE_URL"):
        log_failure("conversations", "not_configured")
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return user_id


def _run(stage: str, fn, *args):
    """연결을 열어 fn(conn, *args)를 한 번 실행한다. 연결·SQL 실패는 종류만 로그에 남기고 503 (HTTPException은 그대로)."""
    try:
        with connect() as conn:
            return fn(conn, *args)
    except Exception as e:  # noqa: BLE001
        log_failure(stage, type(e).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None


@router.get("/conversations", response_model=ConversationList)
def list_conversations(user_id: Annotated[str | None, Depends(current_user_id)] = None) -> ConversationList:
    """내 대화 목록. 최근에 대화한 순서, 최대 50건."""
    uid = _need_user(user_id)
    return ConversationList(conversations=_run("conversations_list", svc.list_for_user, uid))


@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
def get_conversation(conv_id: str, user_id: Annotated[str | None, Depends(current_user_id)] = None) -> ConversationDetail:
    """대화 하나를 다시 연다. 내 것이 아니거나 없으면 404."""
    uid = _need_user(user_id)
    found = _run("conversations_get", svc.get_one, uid, conv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    return ConversationDetail(**found)


class AttachmentTurn(BaseModel):
    """채팅을 부르지 않고 끝난 사진 첨부 턴 (10-08). 사진에서 B/L을 못 찾았거나 확실하지 않고 쓴 글도 없을 때다.
    채팅을 안 불렀으니 routers/chat.py가 저장하지 못한다. 화면이 보인 그대로(첨부 이름, 사용자 글, 안내 한 줄) 여기로 남긴다.
    안내 줄은 화면 코드(web/lib/docAttach.ts)가 쓴 고정 문장이고 LLM 답이 아니다. 판정·조회 값은 없다."""
    text: str = Field(default="", max_length=MESSAGE_MAX)
    attachment_name: str = Field(min_length=1, max_length=svc.ATTACHMENT_NAME_MAX)
    note: str = Field(min_length=1, max_length=svc.ATTACHMENT_NOTE_MAX)


class Saved(BaseModel):
    saved: bool


@router.post("/conversations/{conv_id}/attachment-turns", response_model=Saved)
def save_attachment_turn(conv_id: str, body: AttachmentTurn,
                         user_id: Annotated[str | None, Depends(current_user_id)] = None) -> Saved:
    """채팅 없이 끝난 사진 첨부 턴을 대화에 저장한다 (10-08). 대화 번호는 채팅과 같은 session_id다.
    잠그지 못하면(PII 열쇠 없음) saved=false. 사진은 받지도 저장하지도 않는다(이름만)."""
    uid = _need_user(user_id)
    if len(conv_id) > SESSION_ID_MAX:
        raise HTTPException(status_code=422, detail="대화 번호가 너무 깁니다")
    if not svc.ready():  # PII 열쇠가 없으면 잠글 수 없어 저장하지 않는다 (save_later와 같은 조건)
        log_failure("conversations_attachment_turn", "not_configured")
        return Saved(saved=False)
    extra = svc.user_extra_of(body.attachment_name, None)
    reply = {"reply": body.note, "kind": "message"}
    _run("conversations_attachment_turn", svc.save_turn, uid, conv_id, body.text, reply, extra)
    return Saved(saved=True)


@router.delete("/conversations/{conv_id}", response_model=Deleted)
def delete_conversation(conv_id: str, user_id: Annotated[str | None, Depends(current_user_id)] = None) -> Deleted:
    """대화 하나를 지운다. 내 것이 아니거나 없으면 404."""
    uid = _need_user(user_id)
    if not _run("conversations_delete", svc.delete_one, uid, conv_id):
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    return Deleted(deleted=True)
