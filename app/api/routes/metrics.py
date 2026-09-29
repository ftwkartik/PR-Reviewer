from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.dependencies import SettingsDep
from app.core.security import constant_time_equal

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(
    settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> Response:
    token = settings.metrics_token.get_secret_value()
    if token and not (
        authorization
        and authorization.startswith("Bearer ")
        and constant_time_equal(authorization[7:], token)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "metrics token required")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
