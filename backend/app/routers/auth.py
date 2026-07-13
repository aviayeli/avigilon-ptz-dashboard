from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/api/auth")


class LoginRequest(BaseModel):
    username: str
    password: str


def require_auth(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(401, "Not authenticated")


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    username_match = payload.username == settings.DASHBOARD_USERNAME
    password_match = payload.password == settings.DASHBOARD_PASSWORD
    print(
        f"[LOGIN ATTEMPT] received_username={payload.username!r} "
        f"username_match={username_match} password_match={password_match}",
        flush=True,
    )

    if not username_match or not password_match:
        raise HTTPException(401, "פרטי התחברות שגויים")

    request.session["authenticated"] = True
    return {"ok": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
