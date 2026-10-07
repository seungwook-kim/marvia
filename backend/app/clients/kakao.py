"""카카오 로그인·카카오톡 "나에게 보내기" API 호출.

카카오 서버에 HTTP 요청을 보내고 결과를 Tokens나 KakaoError로 바꾸는 일만 한다. 토큰 저장과 발송 시점은
services/kakao.py, 주소 연결은 routers/kakao.py가 맡는다. 동의 화면이 끝나면 카카오는 브라우저를 화면(/settings/kakao)으로
돌려보내고, 그 화면이 로그인 토큰을 붙여 routers/kakao.py의 POST /callback을 부른다.

- exchange_code와 send_memo는 재시도하지 않는다. 인가 코드는 한 번만 쓸 수 있고, 보내기를 다시 부르면 알림이 두 번 갈 수 있다
- refresh와 unlink는 두 번 보내도 결과가 같아서 clients/retry.py(타임아웃·5xx만 재시도)를 건다
- 시크릿, 인가 코드, 토큰은 로그·예외 메시지·repr에 넣지 않는다. KakaoError에는 kind와 숫자 코드만 담고,
  httpx 예외는 `from None`으로 끊어 URL이나 본문이 따라 나오지 않게 한다. httpx 요청 로그는 main에서 꺼 두었다
- 키와 시크릿은 부를 때마다 환경변수에서 읽는다. 재시작 없이 바뀌고 테스트에서 바꾸기 쉽다
- 타임아웃 10초 (SDD 8절). transport는 테스트가 httpx.MockTransport를 꽂는 자리다

출처: 카카오 로그인 REST API 문서(인가 코드, 토큰, 연결 끊기), 카카오톡 메시지 REST API 문서(나에게 보내기).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from backend.app.clients.retry import with_retry

KAUTH = "https://kauth.kakao.com"
KAPI = "https://kapi.kakao.com"
DEFAULT_REDIRECT_URI = "http://localhost:3000/settings/kakao"  # 백엔드가 아니라 화면이 카카오 결과를 받는다
SCOPE = "talk_message"

TIMEOUT_SEC = 10  # SDD 8절
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}  # 토큰 요청 형식 (카카오 로그인 문서)
UNAUTHORIZED_CODE = -401  # 카카오 API 오류 코드: 토큰이 유효하지 않다
NO_CONSENT_CODES = (-402, -3)  # -402: 사용자가 동의하지 않음 / -3: 앱에서 이 동의 항목이 꺼져 있음 (메시지 REST API 문서)


class KakaoError(Exception):
    """카카오 호출 실패. kind:
    not_configured(키·시크릿 없음) / no_consent(메시지 전송 동의 없음) /
    unauthorized(토큰이 죽음, 다시 연결해야 함) / failed(그 밖의 실패). code: HTTP 상태 또는 카카오 오류 코드."""

    def __init__(self, kind: str, code: int | None = None):
        super().__init__(kind)
        self.kind, self.code = kind, code


@dataclass(frozen=True)
class Tokens:
    """토큰 발급·갱신 응답. 갱신 응답에는 refresh_token이 없을 수 있다(남은 기간 1달 이상이면 안 줌)."""

    access_token: str
    access_expires_in: int            # 초
    refresh_token: str | None
    refresh_expires_in: int | None    # 초
    scope: str | None                 # 공백 구분 동의 항목. talk_message가 있어야 보낼 수 있다

    def __repr__(self) -> str:
        # 토큰 값이 로그나 에러 화면에 repr로 새지 않게 막는다
        return "Tokens(<redacted>)"


def config() -> tuple[str, str, str]:
    """(REST API 키, 클라이언트 시크릿, 리다이렉트 주소(우리 화면 /settings/kakao)). 키나 시크릿이 비면 KakaoError(not_configured)."""
    key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    secret = os.environ.get("KAKAO_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("KAKAO_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    if not key or not secret:
        raise KakaoError("not_configured")
    return key, secret, redirect


def authorize_url(state: str) -> str:
    """사용자를 보낼 카카오 동의 화면 주소 (scope=talk_message, state 포함)."""
    key, _, redirect = config()  # 이 주소에는 시크릿이 들어가지 않는다. 브라우저에 보이는 값이다
    query = urlencode({"response_type": "code", "client_id": key, "redirect_uri": redirect,
                       "scope": SCOPE, "state": state})
    return f"{KAUTH}/oauth/authorize?{query}"


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    """요청 한 번에 쓸 클라이언트. 테스트는 가짜 transport를 넘긴다."""
    if transport is not None:
        return httpx.Client(transport=transport, timeout=TIMEOUT_SEC)
    return httpx.Client(timeout=TIMEOUT_SEC)


def _post(url: str, *, transport: httpx.BaseTransport | None, retry_name: str | None,
          data: dict | None = None, headers: dict | None = None) -> httpx.Response:
    """POST 한 번. retry_name이 있으면 타임아웃·5xx만 재시도하고, 없으면 딱 한 번만 부른다.
    4xx는 오류 본문을 호출한 쪽이 읽어야 하므로 예외로 올리지 않고 돌려준다. 네트워크 실패와 (재시도 끝난) 5xx는 failed."""

    def call() -> httpx.Response:
        with _client(transport) as client:
            resp = client.post(url, data=data, headers=headers)
        if resp.status_code >= 500:
            resp.raise_for_status()  # 5xx는 예외로 올려야 with_retry가 다시 부른다
        return resp

    try:
        return with_retry(call, name=retry_name) if retry_name else call()
    except httpx.HTTPStatusError as e:
        raise KakaoError("failed", e.response.status_code) from None
    except httpx.HTTPError:
        raise KakaoError("failed") from None


def _body(resp: httpx.Response) -> dict:
    """응답 본문을 JSON 객체로. JSON이 아니면 빈 dict (오류 본문 해석용)."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _tokens(resp: httpx.Response) -> Tokens:
    """토큰 응답을 Tokens로. 갱신 응답에는 refresh 항목이 없을 수 있어 그 경우 None이다."""
    body = _body(resp)
    access, expires = body.get("access_token"), body.get("expires_in")
    if not access or not isinstance(expires, int):
        raise KakaoError("failed", resp.status_code)  # 200인데 모양이 다르면 쓸 수 없다
    refresh_expires = body.get("refresh_token_expires_in")
    return Tokens(
        access_token=access,
        access_expires_in=expires,
        refresh_token=body.get("refresh_token") or None,
        refresh_expires_in=refresh_expires if isinstance(refresh_expires, int) else None,
        scope=body.get("scope") or None,
    )


def exchange_code(code: str, *, transport: httpx.BaseTransport | None = None) -> Tokens:
    """인가 코드를 토큰으로 바꾼다. 코드는 한 번만 쓸 수 있어 재시도하지 않는다.
    redirect_uri는 authorize_url에 쓴 값과 같아야 카카오가 받아 준다. 둘 다 config()에서 가져온다."""
    key, secret, redirect = config()
    resp = _post(f"{KAUTH}/oauth/token", transport=transport, retry_name=None, headers=FORM_HEADERS,
                 data={"grant_type": "authorization_code", "client_id": key, "redirect_uri": redirect,
                       "code": code, "client_secret": secret})
    if resp.status_code != 200:
        raise KakaoError("failed", resp.status_code)  # invalid_grant(코드 만료·재사용)도 failed. 처음부터 다시 연결한다
    return _tokens(resp)


def refresh(refresh_token: str, *, transport: httpx.BaseTransport | None = None) -> Tokens:
    """access 토큰을 갱신한다. refresh 토큰이 죽었으면 KakaoError(unauthorized)."""
    key, secret, _ = config()
    resp = _post(f"{KAUTH}/oauth/token", transport=transport, retry_name="kakao:refresh", headers=FORM_HEADERS,
                 data={"grant_type": "refresh_token", "client_id": key, "refresh_token": refresh_token,
                       "client_secret": secret})
    if resp.status_code != 200:
        if _body(resp).get("error") == "invalid_grant":  # refresh 토큰이 만료·폐기됨. 다시 연결해야 한다
            raise KakaoError("unauthorized", resp.status_code)
        raise KakaoError("failed", resp.status_code)
    return _tokens(resp)


def _api_error(resp: httpx.Response) -> KakaoError:
    """kapi 오류 응답(401, 또는 {"msg":..., "code":<음수>})을 KakaoError로 바꾼다."""
    code = _body(resp).get("code")
    if resp.status_code == 401 or code == UNAUTHORIZED_CODE:
        return KakaoError("unauthorized", UNAUTHORIZED_CODE)
    if resp.status_code == 403 and code in NO_CONSENT_CODES:
        return KakaoError("no_consent", code)
    return KakaoError("failed", resp.status_code)


def send_memo(access_token: str, text: str, link_url: str, button_title: str = "Marvia 열기",
              *, transport: httpx.BaseTransport | None = None) -> None:
    """나에게 보내기(기본 텍스트 템플릿). 같은 메시지가 두 번 가지 않게 재시도하지 않는다."""
    template = {"object_type": "text", "text": text,
                "link": {"web_url": link_url, "mobile_web_url": link_url}, "button_title": button_title}
    resp = _post(f"{KAPI}/v2/api/talk/memo/default/send", transport=transport, retry_name=None,
                 headers={**FORM_HEADERS, "Authorization": f"Bearer {access_token}"},
                 data={"template_object": json.dumps(template, ensure_ascii=False)})
    if not resp.is_success:
        raise _api_error(resp)


def unlink(access_token: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """카카오 쪽 연결 끊기. 이미 끊겼거나 토큰이 죽었으면(401) 성공으로 본다."""
    resp = _post(f"{KAPI}/v1/user/unlink", transport=transport, retry_name="kakao:unlink",
                 headers={"Authorization": f"Bearer {access_token}"})
    if resp.is_success:
        return
    error = _api_error(resp)
    if error.kind != "unauthorized":  # 401은 이미 끊긴 것이라 성공. 나머지는 실패
        raise error
