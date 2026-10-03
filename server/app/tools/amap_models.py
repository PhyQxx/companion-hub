"""Provider error contract independent of the HTTP client."""


class AmapProviderError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        *,
        candidates: list[dict[str, str]] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.candidates = candidates or []
        super().__init__(reason_code)
