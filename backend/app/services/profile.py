"""회원 정보(이름, 전화번호, 주소) 검사, 암호화 저장, 마스킹 (docs/specs/user-profile.md).

- 저장: 형식 검사 → crypto.encrypt_pii(PII_ENCRYPTION_KEY)로 잠금 → DB에는 잠긴 글자만. 열쇠가 없으면 원문을 저장하지 않고 ProfileError(no_key)
- 읽기: 본인 화면에만 원문(decrypt). 운영자 고객 관리에는 mask_*로 가린 값만
- 지우기: 탈퇴(services/account.py)가 부른다
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from backend.app import crypto

# 이름 길이는 코드포인트(파이썬 len) 기준이다. 화면 글자(grapheme) 기준은 표준 라이브러리로 셀 수 없어 쓰지 않는다.
# 그래서 가족 이모지(사람 + ZWJ + 사람 ...)나 국기처럼 여러 코드포인트로 된 글자는 한 글자가 여러 자로 세진다.
# 한글·영문 이름에는 영향이 없고 30자면 결합 이모지가 몇 개 섞여도 넉넉하다 (10-08 QA A5 메모)
NAME_MAX = 30
ADDRESS_MIN, ADDRESS_MAX = 5, 200
# 휴대전화: 010은 뒤 8자리만, 011·016~019(옛 번호)는 7~8자리. 하이픈·공백·괄호·점은 빼고 본다 (10-08 QA A5)
_PHONE = re.compile(r"^(?:010\d{8}|01[16789]\d{7,8})$")
# 전화 칸에 허용하는 글자: 숫자와 구분 기호만. 전에는 숫자 아닌 글자를 모두 지우고 봐서
# "010-abcd-1234-5678"이 01012345678로 통과했다 (10-08 QA A5)
_PHONE_CHARS = re.compile(r"^[0-9\s\-().]*$")
# 폭 없는 공백류. 보이지 않는데 글자 수와 비교·검색을 어긋나게 해서 지운다 (U+200B ZWSP, U+2060 WJ, U+FEFF BOM, U+180E)
_ZERO_WIDTH = dict.fromkeys((0x200B, 0x2060, 0xFEFF, 0x180E))
# 서식 문자(Cf) 중 남겨도 되는 것: ZWJ(이모지 결합)와 ZWNJ(일부 문자 표기). 나머지(방향 바꾸기 U+202E 등)는 거절한다
_ALLOWED_FORMAT = {chr(0x200C), chr(0x200D)}


class ProfileError(Exception):
    """저장할 수 없다. kind: consent / name / phone / address / no_key."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Profile:
    name: str
    phone: str     # 숫자만, 화면에 보일 때 format_phone으로 하이픈
    address: str


def _text(value: str | None, kind: str) -> str:
    """글자 칸 하나를 다듬는다 (10-08 QA A5).
    NFC로 맞추고, 폭 없는 공백은 지우고, 제어 문자(NUL 등 Cc)와 허용하지 않는 서식 문자(Cf)가 있으면 kind로 거절한다.
    줄바꿈·탭·U+2028 같은 공백류는 split이 공백 하나로 접는다(앞뒤 공백 제거 포함)."""
    text = unicodedata.normalize("NFC", value or "").translate(_ZERO_WIDTH)
    text = " ".join(text.split())  # 줄바꿈·탭을 먼저 공백으로 접고, 그 뒤에 남은 제어 문자(NUL 등)만 거절한다
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc" or (cat == "Cf" and ch not in _ALLOWED_FORMAT):
            raise ProfileError(kind)
    return text


def clean(name: str, phone: str, address: str, consent: bool) -> Profile:
    """입력을 다듬고 검사한다. 동의가 없으면 다른 칸보다 먼저 거절한다.
    consent는 진짜 참(True)만 동의로 본다. 라우터가 StrictBool로 받아 "yes"·1 같은 값은 422로 먼저 막는다 (10-08 QA A5)."""
    if consent is not True:
        raise ProfileError("consent")
    name = _text(name, "name")
    if not 1 <= len(name) <= NAME_MAX:
        raise ProfileError("name")
    raw_phone = (phone or "").translate(_ZERO_WIDTH)
    if not _PHONE_CHARS.match(raw_phone):  # 숫자·구분 기호 밖의 글자가 섞이면 지우지 않고 거절한다
        raise ProfileError("phone")
    digits = re.sub(r"\D", "", raw_phone)
    if not _PHONE.match(digits):
        raise ProfileError("phone")
    address = _text(address, "address")
    if not ADDRESS_MIN <= len(address) <= ADDRESS_MAX:
        raise ProfileError("address")
    return Profile(name=name, phone=digits, address=address)


def format_phone(digits: str) -> str:
    """01012345678 -> 010-1234-5678, 0111234567 -> 011-123-4567."""
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def mask_name(name: str | None) -> str | None:
    """첫 글자만 남긴다. operators.mask_name과 같은 규칙."""
    if not name or not name.strip():
        return None
    name = name.strip()
    return name[0] + "*" * (len(name) - 1)


def mask_phone(digits: str | None) -> str | None:
    """010-****-5678. 앞 3자리와 뒤 4자리만."""
    if not digits or len(digits) < 7:
        return None
    return f"{digits[:3]}-****-{digits[-4:]}"


def mask_address(address: str | None) -> str | None:
    """앞 두 마디(시·도, 시·군·구)만 남긴다. 서울특별시 강남구 테헤란로 1 -> 서울특별시 강남구 ***."""
    if not address or not address.strip():
        return None
    parts = address.split()
    return " ".join(parts[:2]) + " ***" if len(parts) > 2 else parts[0] + " ***"


def save(conn, user_id: str, profile: Profile, *, by_operator: bool = False) -> None:
    """잠가서 저장한다. 하나라도 못 잠그면(열쇠 없음) 아무것도 저장하지 않는다.
    by_operator: 운영자가 고친 경우. 본인 동의 시각(consent_at)은 그대로 둔다."""
    sealed = [crypto.encrypt_pii(v) for v in (profile.name, profile.phone, profile.address)]  # B/L 열쇠와 분리된 PII 열쇠
    if any(s is None for s in sealed):
        raise ProfileError("no_key")
    consent = "user_profiles.consent_at" if by_operator else "now()"
    conn.execute(
        f"""
        INSERT INTO user_profiles (user_id, name_enc, phone_enc, address_enc, consent_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET name_enc = EXCLUDED.name_enc, phone_enc = EXCLUDED.phone_enc,
          address_enc = EXCLUDED.address_enc, consent_at = {consent}, updated_at = now()
        """,
        [user_id, *sealed],
    )


def _open(row) -> Profile | None:
    """잠긴 세 칸을 연다. 하나라도 못 열면(열쇠 바뀜) None."""
    values = [crypto.decrypt_pii(v) for v in row]
    return None if any(v is None for v in values) else Profile(*values)


def load(conn, user_id: str) -> Profile | None:
    """본인 정보 원문. 없거나 열 수 없으면 None."""
    row = conn.execute("SELECT name_enc, phone_enc, address_enc FROM user_profiles WHERE user_id = %s",
                       [user_id]).fetchone()
    return _open(row) if row else None


def exists(conn, user_id: str) -> bool:
    """입력을 마쳤는가. 열 수 있는지까지는 보지 않는다 (/api/me를 가볍게)."""
    return conn.execute("SELECT 1 FROM user_profiles WHERE user_id = %s", [user_id]).fetchone() is not None


def masked_by_user(conn) -> dict[str, dict]:
    """운영자 고객 관리용. user_id별로 가린 값만 돌려준다. 원문은 이 함수 밖으로 나가지 않는다."""
    out = {}
    for user_id, *sealed in conn.execute(
            "SELECT user_id, name_enc, phone_enc, address_enc FROM user_profiles").fetchall():
        p = _open(sealed)
        out[user_id] = ({"name": mask_name(p.name), "phone": mask_phone(p.phone), "address": mask_address(p.address)}
                        if p else {"name": None, "phone": None, "address": None, "unreadable": True})
    return out


def delete(conn, user_id: str) -> int:
    """탈퇴 때 지운다. 지운 줄 수."""
    return conn.execute("DELETE FROM user_profiles WHERE user_id = %s", [user_id]).rowcount
