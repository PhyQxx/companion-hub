from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db import (
    AppUserRecord,
    AuthCredentialRecord,
    AuthSessionRecord,
    Database,
)
from app.ids import uuid7

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SESSION_TTL = timedelta(hours=8)
# 活跃滑动续期不断顺延 8 小时滚动窗口；硬顶限制单个令牌的绝对寿命，
# 到顶后 expires_at 不再变化，到期必须重新登录，防止长期活跃令牌无限续期。
_SESSION_MAX_LIFETIME = timedelta(days=7)
# 本地账号名：字母/数字/下划线/点/横线（含中文），2~64 位，统一小写存储
_USERNAME_PATTERN = re.compile(r"^[\w.-]{2,64}$", re.UNICODE)


class AuthSetupExists(RuntimeError):
    pass


class InvalidCredentials(RuntimeError):
    pass


class InvalidSession(RuntimeError):
    pass


class InvalidUsername(ValueError):
    pass


def normalize_username(raw: str) -> str:
    """本地账号名统一去空白并小写后校验；不合法抛 InvalidUsername。"""
    username = raw.strip().lower()
    if not _USERNAME_PATTERN.match(username):
        raise InvalidUsername("username must be 2-64 chars of letters/digits/_/./-")
    return username


@dataclass(frozen=True, slots=True)
class ChatPrincipal:
    session_id: UUID
    user_id: UUID
    display_name: str
    expires_at: datetime
    role: str = "member"
    username: str | None = None


@dataclass(frozen=True, slots=True)
class AuthSession:
    access_token: str
    principal: ChatPrincipal


class AuthService:
    def __init__(
        self,
        database: Database,
        *,
        session_ttl: timedelta = _SESSION_TTL,
        session_max_lifetime: timedelta = _SESSION_MAX_LIFETIME,
    ) -> None:
        self._database = database
        self._session_ttl = session_ttl
        self._session_max_lifetime = session_max_lifetime

    async def setup_required(self) -> bool:
        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AuthCredentialRecord)
                .where(AuthCredentialRecord.setup_slot == 1)
            )
        return not count

    async def setup(self, *, display_name: str, password: str) -> AuthSession:
        """初始化业主的本地密码凭据（多用户下的离线降级通道，仅业主持有）。"""
        secret_hash = await asyncio.to_thread(_hash_password, password)
        now = datetime.now(UTC)
        try:
            async with self._database.sessions.begin() as session:
                existing = await session.scalar(
                    select(AuthCredentialRecord.id)
                    .where(AuthCredentialRecord.setup_slot == 1)
                    .limit(1)
                    .with_for_update()
                )
                if existing is not None:
                    raise AuthSetupExists("chat identity is already configured")
                # 只认领未绑定凭据的业主；SSO 开户的 member 不参与本地密码通道。
                unclaimed = list(
                    await session.scalars(
                        select(AppUserRecord)
                        .outerjoin(
                            AuthCredentialRecord,
                            AuthCredentialRecord.user_id == AppUserRecord.id,
                        )
                        .where(
                            AuthCredentialRecord.id.is_(None),
                            AppUserRecord.role == "owner",
                        )
                        .order_by(AppUserRecord.created_at)
                        .limit(2)
                    )
                )
                if len(unclaimed) == 1:
                    user = unclaimed[0]
                    user.display_name = display_name
                else:
                    user = AppUserRecord(
                        id=uuid7(),
                        display_name=display_name,
                        locale="zh-CN",
                        timezone="Asia/Shanghai",
                        status="active",
                        role="owner",
                        created_at=now,
                    )
                    session.add(user)
                session.add(
                    AuthCredentialRecord(
                        id=uuid7(),
                        user_id=user.id,
                        kind="password",
                        secret_hash=secret_hash,
                        setup_slot=1,
                        params_version=1,
                        created_at=now,
                    )
                )
                result = self._new_session(user, now)
                session.add(result[0])
        except IntegrityError as error:
            raise AuthSetupExists("chat identity is already configured") from error
        return result[1]

    async def login(self, *, password: str, username: str | None = None) -> AuthSession:
        """用户名+密码登录；username 缺省时保持旧「单密码」兼容语义
        （对全部活跃密码凭据验证——单用户遗留部署）。"""
        async with self._database.sessions() as session:
            query = (
                select(AuthCredentialRecord, AppUserRecord)
                .join(AppUserRecord, AppUserRecord.id == AuthCredentialRecord.user_id)
                .where(
                    AuthCredentialRecord.kind == "password",
                    AuthCredentialRecord.revoked_at.is_(None),
                    AppUserRecord.status == "active",
                )
            )
            if username is not None:
                query = query.where(
                    AppUserRecord.username == normalize_username(username)
                )
            rows = list(await session.execute(query))
        matched: tuple[AuthCredentialRecord, AppUserRecord] | None = None
        for credential, user in rows:
            if credential.secret_hash and await asyncio.to_thread(
                _verify_password, password, credential.secret_hash
            ):
                matched = (credential, user)
        if matched is None:
            raise InvalidCredentials("invalid chat credential")
        now = datetime.now(UTC)
        record, auth_session = self._new_session(matched[1], now)
        async with self._database.sessions.begin() as session:
            session.add(record)
        return auth_session

    async def login_sso(self, *, user_id: UUID) -> AuthSession:
        """为已验证归属的本地用户直接建立会话（归属校验由调用方完成）。"""
        async with self._database.sessions() as session:
            user = (
                await session.execute(
                    select(AppUserRecord).where(
                        AppUserRecord.id == user_id,
                        AppUserRecord.status == "active",
                    )
                )
            ).scalar_one_or_none()
        if user is None:
            raise InvalidCredentials("sso user not found or inactive")
        now = datetime.now(UTC)
        record, auth_session = self._new_session(user, now)
        async with self._database.sessions.begin() as session:
            session.add(record)
        return auth_session

    async def login_sso_provisioned(
        self, *, sub: str, display_name: str, owner_sub: str
    ) -> AuthSession:
        """pnkx 统一登录（多用户）：按 sso_sub 映射本地用户并建立会话。

        白名单校验由调用方完成。未绑定的 sub 首次登录自动开户：
        owner_sub 回填到尚未绑定 pnkx 账号的存量业主（单用户部署的数据
        归属迁移），其余 sub 以 member 开户。display_name 以 pnkx 为身份源
        每次登录同步。停用用户拒绝登录。
        """
        if not sub:
            raise InvalidCredentials("sso sub is required")
        now = datetime.now(UTC)
        for attempt in range(2):
            try:
                async with self._database.sessions.begin() as session:
                    user = (
                        await session.execute(
                            select(AppUserRecord).where(AppUserRecord.sso_sub == sub)
                        )
                    ).scalar_one_or_none()
                    if user is None and sub == owner_sub:
                        user = (
                            await session.execute(
                                select(AppUserRecord)
                                .where(
                                    AppUserRecord.role == "owner",
                                    AppUserRecord.sso_sub.is_(None),
                                )
                                .order_by(AppUserRecord.created_at)
                                .limit(1)
                                .with_for_update(skip_locked=True)
                            )
                        ).scalar_one_or_none()
                        if user is not None:
                            user.sso_sub = sub
                    if user is None:
                        user = AppUserRecord(
                            id=uuid7(),
                            display_name=display_name or "用户",
                            locale="zh-CN",
                            timezone="Asia/Shanghai",
                            status="active",
                            sso_sub=sub,
                            role="owner" if sub == owner_sub else "member",
                            created_at=now,
                        )
                        session.add(user)
                    if user.status != "active":
                        raise InvalidCredentials("sso user not found or inactive")
                    if display_name:
                        user.display_name = display_name
                    record, auth_session = self._new_session(user, now)
                    session.add(record)
                return auth_session
            except IntegrityError:
                # 并发首登同一 sub 撞唯一索引：重试一次走已存在分支。
                if attempt:
                    raise
                continue
        raise InvalidCredentials("sso login conflict")

    async def authenticate(self, access_token: str) -> ChatPrincipal:
        access_hash = _hash_token(access_token)
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = (
                await session.execute(
                    select(AuthSessionRecord, AppUserRecord)
                    .join(AppUserRecord, AppUserRecord.id == AuthSessionRecord.user_id)
                    .where(AuthSessionRecord.access_hash == access_hash)
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                raise InvalidSession("invalid chat session")
            record, user = row
            expires_at = _aware(record.expires_at)
            if record.revoked_at is not None or expires_at <= now or user.status != "active":
                raise InvalidSession("invalid chat session")
            record.last_seen_at = now
            # 滑动续期：剩余寿命不足一半时才写库顺延，避免每个请求都
            # 产生一次 UPDATE；到达硬顶后不再延长，到期必须重新登录。
            if expires_at - now < self._session_ttl / 2:
                ceiling = _aware(record.issued_at) + self._session_max_lifetime
                candidate = min(now + self._session_ttl, ceiling)
                if candidate > expires_at:
                    record.expires_at = candidate
                    expires_at = candidate
        return ChatPrincipal(
            session_id=record.id,
            user_id=user.id,
            display_name=user.display_name,
            expires_at=expires_at,
            role=user.role,
            username=user.username,
        )

    async def logout(self, principal: ChatPrincipal) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            record = await session.get(AuthSessionRecord, principal.session_id)
            if record is not None and record.revoked_at is None:
                record.revoked_at = now

    async def reset_password(self, new_password: str) -> None:
        """重置聊天密码并撤销所有活跃会话，强制重新登录。"""
        secret_hash = await asyncio.to_thread(_hash_password, new_password)
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            credential = await session.scalar(
                select(AuthCredentialRecord)
                .where(
                    AuthCredentialRecord.kind == "password",
                    AuthCredentialRecord.revoked_at.is_(None),
                )
                .limit(1)
            )
            if credential is None:
                raise InvalidCredentials("no active password credential found")
            credential.secret_hash = secret_hash
            credential.params_version = credential.params_version + 1
            # 撤销所有活跃会话，强制重新登录
            await session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.revoked_at.is_(None),
                    AuthSessionRecord.expires_at > now,
                )
                .values(revoked_at=now)
            )

    def _new_session(
        self, user: AppUserRecord, now: datetime
    ) -> tuple[AuthSessionRecord, AuthSession]:
        token = f"aria_chat_{secrets.token_urlsafe(32)}"
        expires_at = now + self._session_ttl
        record = AuthSessionRecord(
            id=uuid7(),
            user_id=user.id,
            access_hash=_hash_token(token),
            issued_at=now,
            expires_at=expires_at,
            last_seen_at=now,
        )
        principal = ChatPrincipal(
            session_id=record.id,
            user_id=user.id,
            display_name=user.display_name,
            expires_at=expires_at,
            role=user.role,
            username=user.username,
        )
        return record, AuthSession(access_token=token, principal=principal)

    async def set_password(
        self,
        user_id: UUID,
        new_password: str,
        *,
        current_password: str | None = None,
        require_current: bool = True,
    ) -> None:
        """设置/修改本人密码；已有密码时必须提供正确的当前密码
        （require_current=False 仅供业主在用户管理中重置）。"""
        secret_hash = await asyncio.to_thread(_hash_password, new_password)
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            user = await session.get(AppUserRecord, user_id)
            if user is None or user.status != "active":
                raise InvalidCredentials("user not found or inactive")
            credential = await session.scalar(
                select(AuthCredentialRecord)
                .where(
                    AuthCredentialRecord.user_id == user_id,
                    AuthCredentialRecord.kind == "password",
                    AuthCredentialRecord.revoked_at.is_(None),
                )
                .order_by(AuthCredentialRecord.created_at)
                .limit(1)
                .with_for_update()
            )
            if credential is not None and require_current:
                if not current_password or not credential.secret_hash:
                    raise InvalidCredentials("current password required")
                verified = await asyncio.to_thread(
                    _verify_password, current_password, credential.secret_hash
                )
                if not verified:
                    raise InvalidCredentials("current password mismatch")
            if credential is not None:
                credential.secret_hash = secret_hash
                credential.params_version = credential.params_version + 1
            else:
                session.add(
                    AuthCredentialRecord(
                        id=uuid7(),
                        user_id=user_id,
                        kind="password",
                        secret_hash=secret_hash,
                        setup_slot=None,
                        params_version=1,
                        created_at=now,
                    )
                )

    async def set_username(self, user_id: UUID, raw_username: str) -> str:
        """设置本地账号名（小写唯一）；被占用抛 InvalidCredentials。"""
        username = normalize_username(raw_username)
        try:
            async with self._database.sessions.begin() as session:
                user = await session.get(AppUserRecord, user_id, with_for_update=True)
                if user is None:
                    raise InvalidCredentials("user not found")
                user.username = username
        except IntegrityError as error:
            raise InvalidCredentials("username already taken") from error
        return username

    async def create_local_user(
        self, *, display_name: str, username: str, password: str
    ) -> UUID:
        """业主在用户管理中创建本地成员（用户名+密码，无 pnkx 绑定）。"""
        normalized = normalize_username(username)
        secret_hash = await asyncio.to_thread(_hash_password, password)
        now = datetime.now(UTC)
        user_id = uuid7()
        try:
            async with self._database.sessions.begin() as session:
                session.add(
                    AppUserRecord(
                        id=user_id,
                        display_name=display_name,
                        locale="zh-CN",
                        timezone="Asia/Shanghai",
                        status="active",
                        role="member",
                        username=normalized,
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(
                    AuthCredentialRecord(
                        id=uuid7(),
                        user_id=user_id,
                        kind="password",
                        secret_hash=secret_hash,
                        setup_slot=None,
                        params_version=1,
                        created_at=now,
                    )
                )
        except IntegrityError as error:
            raise InvalidCredentials("username already taken") from error
        return user_id


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        )
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
