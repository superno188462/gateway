"""统一身份认证端点。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator

from app.application.auth import AuthService, RegistrationConflictError
from app.container import bearer_scheme, get_auth_service, get_current_user
from app.infrastructure.db.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    """统一用户登录请求。"""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    """普通用户注册请求。"""

    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("用户名去除空格后至少需要 3 个字符")
        return normalized


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


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册普通用户",
    responses={409: {"description": "用户名已存在"}},
)
async def register(
    payload: RegisterRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserResponse:
    """创建普通用户，注册接口不会创建管理员。"""
    try:
        user = await auth_service.register(payload.username.strip(), payload.password)
    except RegistrationConflictError as error:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "username_taken", "message": str(error)},
        ) from error
    return UserResponse(id=str(user.id), username=user.username, role=user.role)


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
