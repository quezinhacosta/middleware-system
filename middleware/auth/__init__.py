from middleware.auth.authenticator import AuthMiddleware
from middleware.auth.context import (
    AuthContextMiddleware,
    auth_token_var,
    get_auth_token,
    set_auth_token,
)
from middleware.auth.rbac import UserIdentity, get_current_user, require_roles
from middleware.auth.service_auth import (
    ServiceAuthenticator,
    ServiceForbiddenError,
    ServiceIdentity,
    ServiceTokenProvider,
    get_authenticator,
    require_internal_service,
)
from middleware.auth.token_provider import (
    AuthenticatedUser,
    AuthError,
    InvalidTokenError,
    TokenExpiredError,
    TokenManager,
    auth_router,
)

__all__ = [
    "AuthError",
    "AuthMiddleware",
    "AuthenticatedUser",
    "InvalidTokenError",
    "TokenExpiredError",
    "TokenManager",
    "UserIdentity",
    "auth_router",
    "get_current_user",
    "require_roles",
    "ServiceAuthenticator",
    "ServiceForbiddenError",
    "ServiceIdentity",
    "ServiceTokenProvider",
    "get_authenticator",
    "require_internal_service",
    "AuthContextMiddleware",
    "auth_token_var",
    "get_auth_token",
    "set_auth_token",
]
