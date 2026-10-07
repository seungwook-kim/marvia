"""운영자가 고객에게 하는 일: 수정, 강제 탈퇴, 마스킹 해제 (docs/specs/operator-admin.md 추가 절).

세 동작 모두 admin_actions에 한 줄 남긴다 (INSERT만).
- 마스킹 해제: 사유를 검사하고 먼저 기록한 뒤에 원문을 돌려준다. 기록이 실패하면 원문은 나가지 않는다
- 강제 탈퇴: 운영자 계정은 해지 먼저, 자기 자신은 안 됨. 지우는 순서는 본인 탈퇴(services/account.py)와 같다.
  이력은 기록 삭제와 같은 트랜잭션에 쓴다. 이력 없는 탈퇴가 생기지 않는다 (10-08 QA A8)
- 수정: 검사 규칙은 본인 입력과 같다(services/profile.clean). 동의 시각은 바꾸지 않는다
- 정지·해제: 우리 DB(suspended_users + 이력)를 먼저 바꾸고 같은 트랜잭션 안에서 Supabase 차단을 건다. Supabase가 실패하면
  트랜잭션이 되돌아가 DB도 그대로다. DB 확정이 실패하면 방금 건 Supabase 차단을 되돌린다 (10-08 QA A7).
  전에는 Supabase를 먼저 걸어 DB 기록이 실패하면 Supabase만 차단되고 화면에서 풀 길이 없었다
"""
from __future__ import annotations

from backend.app.clients.supabase_admin import AdminError, get_user, set_ban
from backend.app.observability.logging import log_failure
from backend.app.services import operators as ops
from backend.app.services import profile as profile_svc
from backend.app.services.account import withdraw

REASON_MIN, REASON_MAX = 10, 500  # 사유서 길이 (Spec EC-09). 한 단어로 때우지 못하게 하한을 둔다
ACTION_LIMIT = 100  # 화면에 보여 줄 이력 수


class CustomerError(Exception):
    """kind: reason / not_found / is_operator / self / already_suspended / not_suspended."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _record(conn, action: str, target_user_id: str, target_email: str | None, actor_id: str,
            actor_email: str | None, reason: str | None = None) -> None:
    """이력 한 줄. 대상은 가린 이메일로만 남긴다 (탈퇴 뒤에도 누구였는지 알아볼 정도만)."""
    # 처리한 운영자 이메일도 평문으로 두지 않는다 (마이그레이션 013). 암호문 + 가린 값
    a_enc, a_masked = ops._seal(actor_email)
    conn.execute(
        "INSERT INTO admin_actions (action, target_user_id, target_email_masked, actor_user_id, actor_email_enc, "
        "actor_email_masked, reason) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        [action, target_user_id, ops.mask_email(target_email), actor_id, a_enc, a_masked, reason],
    )


def _user_or_404(user_id: str) -> dict:
    user = get_user(user_id)
    if not user:
        raise CustomerError("not_found")  # EC-10
    return user


def unmask(conn, user_id: str, actor_id: str, actor_email: str | None, reason: str) -> dict:
    """사유를 남기고 고객 한 명의 원문을 돌려준다. 사유가 짧으면 기록도 원문도 없다 (EC-09)."""
    reason = " ".join((reason or "").split())
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        raise CustomerError("reason")
    user = _user_or_404(user_id)
    email = (user.get("email") or "").lower() or None
    _record(conn, "unmask", user_id, email, actor_id, actor_email, reason)  # 먼저 기록, 실패하면 여기서 예외
    p = profile_svc.load(conn, user_id)
    meta = user.get("user_metadata") or {}
    return {
        "email": email,
        "name": p.name if p else (meta.get("full_name") or meta.get("name")),
        "phone": profile_svc.format_phone(p.phone) if p else None,
        "address": p.address if p else None,
    }


def update(conn, user_id: str, actor_id: str, actor_email: str | None, name: str, phone: str, address: str) -> None:
    """운영자가 고객 정보를 고친다. 동의는 고객 본인이 한 것이라 검사에서는 동의를 있다고 보고 시각은 그대로 둔다."""
    user = _user_or_404(user_id)
    p = profile_svc.clean(name, phone, address, consent=True)
    with conn.transaction():
        profile_svc.save(conn, user_id, p, by_operator=True)
        _record(conn, "customer_update", user_id, user.get("email"), actor_id, actor_email)


def delete(conn, user_id: str, actor_id: str, actor_email: str | None):
    """강제 탈퇴. services/account.WithdrawResult를 돌려준다. 운영자 계정이면 해지 먼저 (EC-12), 자기 자신은 안 된다.

    이력(customer_delete)은 withdraw의 기록 삭제 트랜잭션 안에서 쓴다 (10-08 QA A8). 이력을 못 쓰면 기록도 안 지워지고,
    기록을 지웠으면 이력도 남는다. 정지 줄 정리도 withdraw가 같은 트랜잭션에서 한다.
    Supabase 계정 삭제가 실패해 다시 누르면 기록은 이미 없으니 deleted_records=0, resumed=True로 끝난다.
    이력은 시도마다 한 줄씩 남는다(실제로 누른 횟수 그대로)."""
    if user_id == actor_id:
        raise CustomerError("self")
    user = _user_or_404(user_id)
    email = (user.get("email") or "").lower() or None
    if ops.is_operator(conn, email):
        raise CustomerError("is_operator")
    # 기록 → 회원 정보 → 계정. 실패하면 WithdrawError (stage로 어디까지 됐는지 안다, 다시 누르면 이어서)
    return withdraw(user_id, in_transaction=lambda tx: _record(tx, "customer_delete", user_id, email, actor_id, actor_email))


def list_actions(conn, limit: int = ACTION_LIMIT) -> list[dict]:
    """운영자 작업 이력, 최신순."""
    rows = conn.execute(
        "SELECT action, target_user_id, target_email_masked, actor_user_id, actor_email_enc, actor_email_masked, reason, "
        "created_at FROM admin_actions ORDER BY created_at DESC, id DESC LIMIT %s", [limit]).fetchall()
    return [{"action": r[0], "target_user_id": r[1], "target_email_masked": r[2], "actor_user_id": r[3],
             "actor_email": ops._open(r[4], r[5]), "reason": r[6], "created_at": r[7]} for r in rows]


# ── 일시 정지 (operator-admin "고객 계정 일시 정지") ──

def is_suspended(conn, user_id: str) -> bool:
    """지금 정지 중인가. auth.current_user가 요청마다 부른다."""
    return conn.execute("SELECT 1 FROM suspended_users WHERE user_id = %s", [user_id]).fetchone() is not None


def is_withdrawn(conn, user_id: str) -> bool:
    """탈퇴한 계정인가 (migrations/027, 10-08 QA A3). auth.current_user가 요청마다 부른다.
    30일 지난 줄은 purge_old_withdrawn_users()가 지운다. 그때는 출입증(최대 1시간)이 이미 만료된 지 오래다."""
    return conn.execute("SELECT 1 FROM withdrawn_users WHERE user_id = %s", [user_id]).fetchone() is not None


def suspended_ids(conn) -> set[str]:
    """정지 중인 사람 전부. 고객 목록에 표시할 때."""
    return {r[0] for r in conn.execute("SELECT user_id FROM suspended_users").fetchall()}


def _banned_in_supabase(user: dict | None) -> bool:
    """Supabase 계정에 차단이 걸려 있나. banned_until이 지금보다 뒤면 차단이다 (Supabase Auth admin 사용자 응답).
    정지 표와 Supabase가 어긋난 경우를 고객 목록에 보여 주고(routers/admin.py), 해제로 바로잡을 때 쓴다 (10-08 QA A7)."""
    from datetime import datetime, timezone

    until = (user or {}).get("banned_until")
    if not until or not isinstance(until, str):
        return False
    try:
        return datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return True  # 모양을 모르겠으면 차단이 있다고 본다. 해제로 풀 수 있게 하는 쪽이 안전하다


def _undo_ban(user_id: str, banned: bool, stage: str) -> None:
    """DB 확정이 실패했을 때 방금 바꾼 Supabase 차단을 되돌린다. 되돌리기도 실패하면 로그만 남긴다.
    그 경우 고객 목록의 supabase_banned 칸이 어긋남을 보여 주고, 같은 버튼을 다시 누르면 맞춰진다."""
    try:
        set_ban(user_id, not banned)
    except AdminError as e:
        log_failure(stage, f"ban_undo_failed {e.kind}")


def _ban_then_commit(conn, user_id: str, banned: bool, stage: str, db_change) -> None:
    """정지·해제 공통 순서 (10-08 QA A7).
    1. 트랜잭션을 열고 우리 표·이력을 바꾼다(db_change)
    2. 같은 트랜잭션 안에서 Supabase 차단을 바꾼다. 실패(AdminError)하면 예외가 트랜잭션을 되돌려 DB도 그대로다
    3. 트랜잭션 확정(COMMIT)이 실패하면 2를 되돌린다
    외부 호출 동안 트랜잭션을 열어 두지만 한 사람 줄 하나이고 호출 상한(10초, clients/supabase_admin.py)이 있다."""
    applied = False
    try:
        with conn.transaction():
            db_change()
            set_ban(user_id, banned)
            applied = True
    except AdminError:
        raise  # Supabase가 실패했다. DB는 되돌아갔다
    except Exception:
        if applied:  # Supabase는 바뀌었는데 DB 확정이 실패했다
            _undo_ban(user_id, banned, stage)
        raise


def suspend(conn, user_id: str, actor_id: str, actor_email: str | None, reason: str) -> None:
    """정지. 사유 필수. 운영자·자기 자신 불가. 우리 표·이력과 Supabase 차단을 함께 바꾼다(_ban_then_commit)."""
    reason = " ".join((reason or "").split())
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        raise CustomerError("reason")
    if user_id == actor_id:
        raise CustomerError("self")
    user = _user_or_404(user_id)
    email = (user.get("email") or "").lower() or None
    if ops.is_operator(conn, email):
        raise CustomerError("is_operator")
    if is_suspended(conn, user_id):
        raise CustomerError("already_suspended")

    def change():
        conn.execute("INSERT INTO suspended_users (user_id, suspended_by, reason) VALUES (%s, %s, %s)",
                     [user_id, actor_id, reason])
        _record(conn, "customer_suspend", user_id, email, actor_id, actor_email, reason)

    _ban_then_commit(conn, user_id, True, "admin_suspend", change)


def resume(conn, user_id: str, actor_id: str, actor_email: str | None) -> None:
    """정지 해제. 우리 표에서 지우고 Supabase 차단을 푼다(_ban_then_commit).

    정지 표에는 없는데 Supabase에만 차단이 남은 경우(예전 순서에서 DB 기록이 실패했던 계정)도 여기서 푼다 (10-08 QA A7).
    이미 탈퇴한 계정(get_user가 None)이면 Supabase는 건너뛰고 우리 표만 정리한다."""
    user = get_user(user_id)
    in_table = is_suspended(conn, user_id)
    if not in_table and not _banned_in_supabase(user):
        raise CustomerError("not_suspended")

    def change():
        conn.execute("DELETE FROM suspended_users WHERE user_id = %s", [user_id])
        _record(conn, "customer_resume", user_id, (user or {}).get("email"), actor_id, actor_email)

    if not user:
        with conn.transaction():
            change()
        return
    _ban_then_commit(conn, user_id, False, "admin_resume", change)
