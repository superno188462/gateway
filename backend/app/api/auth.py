"""统一身份认证端点。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.application.auth import AuthService
from app.container import bearer_scheme, get_auth_service, get_current_user
from app.infrastructure.db.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    """统一用户登录请求。"""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """管理员访问令牌响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="令牌有效期，单位为秒。")


class UserResponse(BaseModel):
    """当前用户公开信息。"""

    id: str
    username: str
    role: str


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(
    payload: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """验证用户凭据并签发包含角色的短期 Bearer 令牌。"""
    token, expires_in = await auth_service.login(payload.username, payload.password)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="用户退出",
    responses={401: {"description": "令牌无效或缺失"}},
)
async def logout(
    current_user: Annotated[User, Depends(get_current_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    response: Response,
) -> Response:
    """撤销当前令牌；客户端也应清理本地令牌。"""
    del current_user
    await auth_service.logout(credentials.credentials)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    response_model=UserResponse,
    summary="获取当前用户",
    responses={401: {"description": "令牌无效或缺失"}},
)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """返回当前登录用户的非敏感信息。"""
    return UserResponse(
        id=str(current_user.id), username=current_user.username, role=current_user.role
    )
