"""Deterministic provider for tests and offline demos. Scripts what the 'model' replies."""

from collections.abc import Callable

from pydantic import BaseModel

from app.llm.base import LLMRequest, LLMResult, LLMUsage


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        responder: Callable[[LLMRequest[BaseModel]], BaseModel | Exception],
    ) -> None:
        self._responder = responder
        self.requests: list[LLMRequest[BaseModel]] = []

    async def generate[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        self.requests.append(request)  # type: ignore[arg-type]
        out = self._responder(request)  # type: ignore[arg-type]
        if isinstance(out, Exception):
            raise out
        assert isinstance(out, request.schema)  # noqa: S101 - test double contract
        return LLMResult(out, LLMUsage(input_tokens=1000, output_tokens=200, calls=1), self.model,
                         [f"fake-req-{len(self.requests)}"])  # fmt: skip
