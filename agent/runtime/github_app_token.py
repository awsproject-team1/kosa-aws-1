"""Mint a GitHub App installation token from a stored credential, in the standard library only.

**왜 이 모듈이 있는가.** 지금까지 Secrets Manager에는 installation **token**이 들어 있었다. 그
token은 1시간 만에 만료되므로 평가를 돌릴 때마다 사람이 재발급해 넣어야 했고, 넣지 않으면 worker가
`LOAD_IAC`에서 401로 죽었다 — 라이브에서 실제로 그렇게 멈췄다. 자격의 정체를 token에서 **App
private key**로 바꾸면 worker가 필요할 때 스스로 발급한다. 사람의 개입은 key를 한 번 넣는 것으로
끝난다.

**왜 서명을 직접 구현하는가.** 함수 ZIP은 third-party 의존성을 갖지 않는다(`scripts/package-m0-lambda.sh`)
— 그것이 결정적 빌드와 승인 경계의 전제다. Lambda Layer에도 `cryptography`는 없다(배포된 Layer의
top-level 패키지 76개를 확인했다). RS256은 RSA PKCS#1 v1.5 서명이고, 그 연산은 결정적이며 난수도
비밀 유지 로직도 필요 없다 — 패딩된 다이제스트를 개인 지수로 거듭제곱하는 것이 전부다. Python의
정수 `pow`가 그것을 그대로 한다. 구현이 틀리면 GitHub이 401로 거절하므로 실패는 닫히는 쪽이다
(`tests/unit/test_github_app_token.py`가 openssl이 만든 서명과 바이트로 대조한다).

**더 단단하게 만들려면** private key를 KMS에 두고 `kms:Sign`을 쓴다. 그러면 key가 Secrets Manager를
떠나지 않고 CloudTrail에 서명 기록이 남는다. 그 전환은 key 반입 절차와 IAM 변경을 요구하므로 이
모듈의 서명 함수 하나를 교체하는 후속 작업으로 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import time
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: App JWT의 수명. GitHub은 10분을 넘는 JWT를 거절한다. 9분은 그 안에서 시계 오차를 견딘다.
_JWT_LIFETIME_SECONDS = 540

#: 발급 시각을 60초 뒤로 미룬다. Lambda와 GitHub의 시계가 몇 초 어긋나도 `iat`가 미래가 되지 않는다.
_JWT_BACKDATE_SECONDS = 60

#: 만료 이 시간 전이면 새로 발급한다. 평가 한 건이 끝나기 전에 token이 죽지 않게 한다.
_REFRESH_MARGIN_SECONDS = 300

#: SHA-256 DigestInfo의 DER 접두사 (RFC 8017 §9.2). 다이제스트 32바이트가 뒤에 붙는다.
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class GitHubAppTokenError(RuntimeError):
    """Raised when an installation token cannot be minted.

    사유에 key 내용이나 token 값을 담지 않는다. 이 예외는 호출자에게 그대로 올라가고, 호출자는
    이미 GitHub 실패를 fail-closed로 다룬다.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class GitHubAppCredentials:
    """The three values needed to mint an installation token."""

    app_id: str
    installation_id: str
    private_key_pem: str

    def __post_init__(self) -> None:
        for name in ("app_id", "installation_id", "private_key_pem"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if "PRIVATE KEY" not in self.private_key_pem:
            raise ValueError("private_key_pem must be a PEM-encoded RSA private key")


def parse_app_credentials(secret_value: str) -> GitHubAppCredentials | None:
    """Read a stored GitHub credential, returning None when it is a plain token.

    같은 secret이 두 모양을 갖는다. 예전 모양은 installation token 문자열 하나이고, 새 모양은
    App 자격 JSON이다. 두 모양을 모두 읽어야 secret을 바꾸는 순간과 배포 순간이 서로를 기다리지
    않는다 — 어느 쪽이 먼저 와도 그 사이에 평가가 멈추지 않는다.
    """
    if not isinstance(secret_value, str) or not secret_value.strip():
        raise GitHubAppTokenError("the stored GitHub credential is empty")
    try:
        parsed = json.loads(secret_value)
    except ValueError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    try:
        return GitHubAppCredentials(
            app_id=str(parsed["app_id"]),
            installation_id=str(parsed["installation_id"]),
            private_key_pem=str(parsed["private_key"]),
        )
    except (KeyError, ValueError) as error:
        # JSON이지만 App 자격이 아니다. token 문자열로 되돌리면 그 JSON이 그대로 Authorization
        # 헤더에 실려 나가므로, 여기서 거부한다.
        raise GitHubAppTokenError(
            "the stored GitHub credential is not a usable App credential"
        ) from error


class GitHubAppTokenProvider:
    """A `token_provider` that mints and reuses one installation token.

    호출자 계약은 그대로다 — `Callable[[], str]`. 그래서 5개 조립 지점(api·assessment·remediation·
    deployment×2)이 전달하던 lambda를 이것으로 바꾸기만 하면 된다.

    캐시는 인스턴스에 둔다. 한 번의 Lambda 실행이 여러 리소스·여러 관점을 평가하므로 그 안에서
    재사용하는 것이 실질적인 절약이고, 모듈 전역 캐시가 만드는 공유 상태는 그 이득에 비해 비싸다.
    """

    def __init__(
        self,
        *,
        secret_reader: Callable[[], str],
        now: Callable[[], float] = time.time,
        request: Callable[[str, Mapping[str, str]], Mapping[str, object]] | None = None,
        refresh_margin_seconds: int = _REFRESH_MARGIN_SECONDS,
    ) -> None:
        if not callable(secret_reader) or not callable(now):
            raise TypeError("secret_reader and now must be callable")
        self._secret_reader = secret_reader
        self._now = now
        self._request = request or _post_installation_token
        self._refresh_margin = refresh_margin_seconds
        self._token: str | None = None
        self._expires_at: float | None = None

    def __call__(self) -> str:
        stored = self._secret_reader()
        credentials = parse_app_credentials(stored)
        if credentials is None:
            # 저장된 것이 이미 token이다. 그대로 쓴다 — 만료 판단은 GitHub이 한다.
            return stored
        if self._token is not None and self._expires_at is not None:
            if self._expires_at - self._refresh_margin > self._now():
                return self._token
        token, expires_at = self._mint(credentials)
        self._token, self._expires_at = token, expires_at
        return token

    def _mint(self, credentials: GitHubAppCredentials) -> tuple[str, float]:
        jwt = _app_jwt(credentials, now=int(self._now()))
        url = (
            f"https://api.github.com/app/installations/{credentials.installation_id}/access_tokens"
        )
        body = self._request(url, _headers(jwt))
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubAppTokenError("GitHub returned no installation token")
        return token, _expiry_epoch(body.get("expires_at"), fallback=self._now() + 3600)


def _headers(jwt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _post_installation_token(url: str, headers: Mapping[str, str]) -> Mapping[str, object]:
    request = Request(url, method="POST", headers=dict(headers))
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https GitHub host
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        # 응답 본문에는 App 식별자가 들어갈 수 있으므로 원문을 전달하지 않는다.
        raise GitHubAppTokenError("GitHub installation token request failed") from error
    if not isinstance(payload, Mapping):
        raise GitHubAppTokenError("GitHub installation token response is invalid")
    return payload


def _expiry_epoch(value: object, *, fallback: float) -> float:
    """Read GitHub's RFC 3339 `expires_at`, falling back to one hour when it is unusable."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return fallback


def _app_jwt(credentials: GitHubAppCredentials, *, now: int) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "iat": now - _JWT_BACKDATE_SECONDS,
                "exp": now + _JWT_LIFETIME_SECONDS,
                "iss": credentials.app_id,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(rs256_signature(signing_input, credentials.private_key_pem))
    return f"{header}.{payload}.{signature}"


def _b64url(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def rs256_signature(message: bytes, private_key_pem: str) -> bytes:
    """Sign one message with RSASSA-PKCS1-v1_5 over SHA-256 (RFC 8017 §8.2.1).

    결정적 연산이다. 서명은 패딩된 다이제스트를 개인 지수로 거듭제곱한 값이며, 그것을 modulus
    길이의 big-endian 바이트로 낸다.
    """
    modulus, private_exponent = _rsa_private_numbers(private_key_pem)
    key_bytes = (modulus.bit_length() + 7) // 8
    encoded = _pkcs1_v15_encode(message, key_bytes)
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(key_bytes, "big")


def _pkcs1_v15_encode(message: bytes, key_bytes: int) -> bytes:
    """`0x00 || 0x01 || 0xFF... || 0x00 || DigestInfo` (RFC 8017 §9.2)."""
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = key_bytes - len(digest_info) - 3
    if padding_length < 8:
        raise GitHubAppTokenError("the RSA key is too small to sign a SHA-256 digest")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info


def _rsa_private_numbers(private_key_pem: str) -> tuple[int, int]:
    """Return `(modulus, private_exponent)` from a PKCS#1 or PKCS#8 PEM.

    GitHub은 PKCS#1(`BEGIN RSA PRIVATE KEY`)로 내려주지만, 사람이 변환해 넣는 경우가 있으므로
    PKCS#8(`BEGIN PRIVATE KEY`)도 읽는다. 두 모양의 차이는 바깥 SEQUENCE 하나뿐이다.
    """
    der = _pem_body(private_key_pem)
    fields = _der_sequence(der)
    if len(fields) >= 9 and all(tag == 0x02 for tag, _ in fields[:9]):
        # PKCS#1: version, n, e, d, p, q, dp, dq, qinv
        return _der_integer(fields[1][1]), _der_integer(fields[3][1])
    if len(fields) >= 3 and fields[2][0] == 0x04:
        # PKCS#8: version, AlgorithmIdentifier, PrivateKey OCTET STRING wrapping the PKCS#1 body
        inner = _der_sequence(fields[2][1])
        if len(inner) >= 4 and all(tag == 0x02 for tag, _ in inner[:4]):
            return _der_integer(inner[1][1]), _der_integer(inner[3][1])
    raise GitHubAppTokenError("the stored private key is not an RSA private key")


def _pem_body(private_key_pem: str) -> bytes:
    lines = [
        line.strip()
        for line in private_key_pem.replace("\\n", "\n").splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    if not lines:
        raise GitHubAppTokenError("the stored private key has no PEM body")
    try:
        return b64decode("".join(lines))
    except ValueError as error:
        raise GitHubAppTokenError("the stored private key is not valid base64") from error


def _der_sequence(der: bytes) -> list[tuple[int, bytes]]:
    """Return the `(tag, content)` pairs directly inside one DER SEQUENCE."""
    tag, content, remainder = _der_read(der)
    if tag != 0x30 or remainder:
        raise GitHubAppTokenError("the stored private key is not a DER sequence")
    fields: list[tuple[int, bytes]] = []
    while content:
        field_tag, field_content, content = _der_read(content)
        fields.append((field_tag, field_content))
    return fields


def _der_read(der: bytes) -> tuple[int, bytes, bytes]:
    """Read one DER TLV, returning `(tag, content, remainder)`."""
    if len(der) < 2:
        raise GitHubAppTokenError("the stored private key is truncated")
    tag, first = der[0], der[1]
    if first < 0x80:
        length, start = first, 2
    else:
        length_bytes = first & 0x7F
        if length_bytes == 0 or len(der) < 2 + length_bytes:
            raise GitHubAppTokenError("the stored private key has an invalid DER length")
        length = int.from_bytes(der[2 : 2 + length_bytes], "big")
        start = 2 + length_bytes
    end = start + length
    if end > len(der):
        raise GitHubAppTokenError("the stored private key is truncated")
    return tag, der[start:end], der[end:]


def _der_integer(content: bytes) -> int:
    return int.from_bytes(content, "big")
