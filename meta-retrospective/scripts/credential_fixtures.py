"""Synthetic credential-like strings shared by every consumer test.

Each fixture is (label, text, kind): `text` embeds a fake secret and `kind` is the
label the detector must report. Nothing here is a real credential; the pieces are
concatenated so this file does not itself look like a leak to scanners.
"""

FAKE = "Zx9Qw3Lp7Rt5Vb2N"

FIXTURES = (
    ("private-key", "-----BEGIN " + "RSA PRIVATE KEY-----", "private-key"),
    ("bearer", "curl -H 'Authorization: Bear" + "er " + FAKE + "'", "authorization"),
    ("basic", "header Bas" + "ic " + FAKE + "==", "authorization"),
    ("query-token", "GET /v1/items?to" + "ken=" + FAKE, "query-secret"),
    ("query-access-token", "GET /cb?a=1&access_" + "token=" + FAKE, "query-secret"),
    ("query-key", "https://host.example/x?ke" + "y=" + FAKE, "query-secret"),
    ("query-refresh", "/oauth?refresh_" + "token=" + FAKE, "query-secret"),
    ("assign-password", "pass" + "word: " + FAKE, "assignment"),
    ("assign-unquoted-short", "pass" + "word=hunter2", "assignment"),
    ("assign-refresh", "refresh_" + "token = " + FAKE, "assignment"),
    ("assign-api-key", "api_" + "key=" + FAKE, "assignment"),
    ("openai-style", "sk" + "-" + FAKE + "abcdef", "token-prefix"),
    ("github-style", "gh" + "p_" + "A" * 24, "token-prefix"),
    ("jwt", "eyJ" + "hbGciOi" + "." + "eyJzdWIiOiIx" + "." + "c2lnbmF0dXJl", "jwt"),
    ("uri-password", "postgres://user:" + FAKE + "@db.example/app", "uri-password"),
)

# Strings that must NOT be flagged (placeholders and ordinary prose).
BENIGN = (
    "pass" + "word=<redacted>",
    "to" + "ken: ${TOKEN_FROM_ENV}",
    "GET /v1/items?to" + "ken=<token>",
    "Use a bearer token from the environment, never inline.",
    "Basic configuration steps are listed below.",
    "token budget exceeded twice in one session",
)

CREDENTIAL_KEYS = ("pass" + "word", "api_" + "key", "access_" + "token", "Authori" + "zation", "client_" + "secret")
