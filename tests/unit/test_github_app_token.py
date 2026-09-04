"""The worker mints its own GitHub installation token, so nobody has to paste one hourly.

이 파일이 고정하는 것은 넷이다.

1. **서명이 실제로 RS256이다.** openssl이 만든 서명과 바이트로 대조한다. 손으로 쓴 암호 코드는
   "돌아간다"로 충분하지 않다 — 참조 구현과 같은 바이트여야 한다.
2. **예전 모양의 secret이 그대로 동작한다.** secret 교체와 배포는 서로를 기다리지 않는다.
3. **발급한 token을 그 실행 안에서 재사용한다.** 평가 한 건이 리소스마다 GitHub을 다시 부르지
   않는다.
4. **실패는 닫힌다.** 자격이 아닌 JSON을 token으로 착각해 Authorization 헤더에 싣지 않는다.
"""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.runtime.github_app_token import (
    GitHubAppCredentials,
    GitHubAppTokenError,
    GitHubAppTokenProvider,
    parse_app_credentials,
    rs256_signature,
)

#: 이 테스트만을 위한 RSA 키. 실제 App 키는 저장소에 두지 않는다.
_KEY_SIZE_BITS = 2048


def _generate_key(directory: str, *, pkcs8: bool = False) -> str:
    path = Path(directory) / "test-key.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(path), str(_KEY_SIZE_BITS)],
        check=True,
        capture_output=True,
    )
    if not pkcs8:
        return path.read_text(encoding="utf-8")
    converted = subprocess.run(
        ["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return converted.stdout


def _openssl_signature(directory: str, pem: str, message: bytes) -> bytes:
    path = Path(directory) / "signing-key.pem"
    path.write_text(pem, encoding="utf-8")
    return subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(path)],
        input=message,
        check=True,
        capture_output=True,
    ).stdout


class Rs256SignatureTest(unittest.TestCase):
    MESSAGE = b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiI0ODEzNjA5In0"

    def test_the_signature_matches_openssl_byte_for_byte(self) -> None:
        """참조 구현과 같은 바이트여야 한다. GitHub이 받아주는지는 그 다음 문제다."""
        with TemporaryDirectory() as directory:
            pem = _generate_key(directory)

            signature = rs256_signature(self.MESSAGE, pem)

            self.assertEqual(signature, _openssl_signature(directory, pem, self.MESSAGE))
            self.assertEqual(len(signature), _KEY_SIZE_BITS // 8)

    def test_a_pkcs8_key_signs_identically(self) -> None:
        """GitHub은 PKCS#1로 내려주지만 사람이 변환해 넣는 경우가 있다."""
        with TemporaryDirectory() as directory:
            pkcs8 = _generate_key(directory, pkcs8=True)

            self.assertEqual(
                rs256_signature(self.MESSAGE, pkcs8),
                _openssl_signature(directory, pkcs8, self.MESSAGE),
            )

    def test_a_key_that_is_not_rsa_is_refused(self) -> None:
        with self.assertRaises(GitHubAppTokenError):
            rs256_signature(
                self.MESSAGE, "-----BEGIN PRIVATE KEY-----\nZm9v\n-----END PRIVATE KEY-----"
            )


class StoredCredentialTest(unittest.TestCase):
    def test_a_plain_token_is_recognised_as_a_token(self) -> None:
        self.assertIsNone(parse_app_credentials("ghs_example"))

    def test_an_app_credential_is_parsed(self) -> None:
        credentials = parse_app_credentials(
            json.dumps(
                {
                    "app_id": 4813609,
                    "installation_id": 158679675,
                    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nZm9v\n-----END RSA PRIVATE KEY-----",
                }
            )
        )

        assert credentials is not None
        # 숫자로 저장돼도 식별자는 문자열이다 — URL과 JWT 발급자에 그대로 들어간다.
        self.assertEqual(credentials.app_id, "4813609")
        self.assertEqual(credentials.installation_id, "158679675")

    def test_json_that_is_not_a_credential_is_refused_rather_than_used_as_a_token(self) -> None:
        """자격이 아닌 JSON을 token으로 되돌리면 그 JSON이 Authorization 헤더에 실려 나간다."""
        with self.assertRaises(GitHubAppTokenError):
            parse_app_credentials(json.dumps({"app_id": "1"}))

    def test_an_empty_secret_is_refused(self) -> None:
        with self.assertRaises(GitHubAppTokenError):
            parse_app_credentials("   ")

    def test_a_credential_without_a_pem_body_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            GitHubAppCredentials(app_id="1", installation_id="2", private_key_pem="not a key")


class TokenProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.credential = json.dumps(
            {
                "app_id": "4813609",
                "installation_id": "158679675",
                "private_key": _generate_key(self._directory.name),
            }
        )
        self.requests: list[tuple[str, dict[str, str]]] = []

    def _provider(self, *, clock: list[float], expires_at: str = "2026-09-04T15:00:00Z"):
        def request(url: str, headers: dict[str, str]) -> dict[str, object]:
            self.requests.append((url, dict(headers)))
            return {"token": f"ghs_minted_{len(self.requests)}", "expires_at": expires_at}

        return GitHubAppTokenProvider(
            secret_reader=lambda: self.credential,
            now=lambda: clock[0],
            request=request,
        )

    def test_a_stored_app_credential_mints_a_token(self) -> None:
        provider = self._provider(clock=[0.0])

        self.assertEqual(provider(), "ghs_minted_1")
        url, headers = self.requests[0]
        self.assertEqual(url, "https://api.github.com/app/installations/158679675/access_tokens")
        # App JWT는 세 부분이고, 그것으로 installation token을 받는다.
        self.assertEqual(len(headers["Authorization"].removeprefix("Bearer ").split(".")), 3)

    def test_the_minted_token_is_reused_until_it_is_close_to_expiring(self) -> None:
        clock = [1_788_000_000.0]
        provider = self._provider(clock=clock)
        provider()

        clock[0] += 60
        self.assertEqual(provider(), "ghs_minted_1")
        self.assertEqual(len(self.requests), 1)

    def test_a_token_close_to_expiry_is_replaced(self) -> None:
        """평가 한 건이 끝나기 전에 token이 죽으면 안 된다."""
        import datetime

        expiry = datetime.datetime(2026, 9, 4, 15, 0, tzinfo=datetime.UTC).timestamp()
        clock = [expiry - 3600]
        provider = self._provider(clock=clock)
        provider()

        clock[0] = expiry - 60
        self.assertEqual(provider(), "ghs_minted_2")
        self.assertEqual(len(self.requests), 2)

    def test_a_plain_token_secret_is_passed_through_without_calling_github(self) -> None:
        provider = GitHubAppTokenProvider(
            secret_reader=lambda: "ghs_already_a_token",
            request=lambda url, headers: self.fail("a plain token must not be minted"),
        )

        self.assertEqual(provider(), "ghs_already_a_token")

    def test_a_response_without_a_token_fails_closed(self) -> None:
        provider = GitHubAppTokenProvider(
            secret_reader=lambda: self.credential,
            request=lambda url, headers: {"message": "Bad credentials"},
        )

        with self.assertRaises(GitHubAppTokenError):
            provider()

    def test_an_unreadable_expiry_still_bounds_the_cached_token(self) -> None:
        clock = [1_788_000_000.0]
        provider = self._provider(clock=clock, expires_at="not-a-timestamp")
        provider()

        # 만료를 읽지 못하면 한 시간으로 본다. 무기한 재사용하지 않는다.
        clock[0] += 3600
        self.assertEqual(provider(), "ghs_minted_2")


if __name__ == "__main__":
    unittest.main()
