"""Error types. No message in this module may carry key material."""

STATUS = {"unauthorized": 401, "invalid_request": 400, "unknown_chain": 400, "unknown_store": 400, "cap_exceeded": 403,
          "daily_cap_exceeded": 403, "token_not_allowed": 403, "idempotency_conflict": 409, "rate_limited": 429,
          "keystore_missing": 503, "not_found": 404, "internal": 500}  # fmt: skip


class ApiError(Exception):
    def __init__(self, code: str, detail: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.status = code, detail, status if status is not None else STATUS[code]


class StartError(Exception):
    """The service must not start, or a command must stop (config, master key, token, keystore, journal, audit log)."""


ConfigError, KeystoreError, AuditError, JournalError = (
    type(name, (StartError,), {}) for name in ("ConfigError", "KeystoreError", "AuditError", "JournalError")
)
