from pydantic import BaseModel, Field
from typing import Optional


class LoginForm(BaseModel):
    username: str
    password: str


class SessionCreate(BaseModel):
    title: str = "Untitled Session"
    cwd: str = ""
    prompt: str = ""
    model: str = ""
    mode: str = "bypassPermissions"
    command: str = "claude"
    env_vars: str = ""


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    cwd: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = None
    status: Optional[str] = None
    command: Optional[str] = None
    env_vars: Optional[str] = None


class TemplateCreate(BaseModel):
    name: str
    title: str = ""
    cwd: str = ""
    prompt: str = ""
    model: str = ""
    mode: str = "bypassPermissions"
    command: str = "claude"
    env_vars: str = ""


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    cwd: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = None
    command: Optional[str] = None
    env_vars: Optional[str] = None


class ChatMessage(BaseModel):
    text: str


class BulkAction(BaseModel):
    session_ids: list[int]
    action: str  # "delete" or "interrupt"
