from app.routers.admin import router as admin_router
from app.routers.generate import router as generate_router
from app.routers.linuxdo_auth import router as linuxdo_auth_router
from app.routers.user_auth import router as user_auth_router

__all__ = ["admin_router", "generate_router", "linuxdo_auth_router", "user_auth_router"]
