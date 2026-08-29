import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base, GenerationLog, User, is_abnormal_generation_failure
from app.services.user_auth import build_user_usage_snapshot
from app.time_utils import utcnow_naive


def _user(**overrides):
    values = dict(
        id="temporary-user",
        username="temporary-user",
        password_hash="unused",
        status="active",
        auth_kind="invite_guest",
        daily_quota=5,
        daily_used=2,
        total_requests=2,
        in_flight=0,
        max_inflight=1,
        last_used_at=utcnow_naive() - timedelta(days=2),
    )
    values.update(overrides)
    return User(**values)


def test_invite_quota_does_not_reset_on_a_new_day(monkeypatch):
    user = _user()
    monkeypatch.setattr("app.services.user_auth.get_user_auth_service", lambda: None)
    snapshot = build_user_usage_snapshot(user, now=utcnow_naive())
    assert snapshot["daily_used"] == 2
    assert snapshot["quota_remaining"] == 3


def test_abnormal_classifier_excludes_network_failures():
    assert is_abnormal_generation_failure(
        "Error in Node Text to Image: Your request was rejected by the safety system"
    )
    assert not is_abnormal_generation_failure("Error in Node Text to Image: Network timeout")


def test_four_abnormal_failures_temporarily_disable_user_for_three_hours():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with factory() as session:
                session.add(_user(daily_used=0, last_used_at=None))
                await session.commit()
                for index in range(4):
                    session.add(
                        GenerationLog(
                            id=f"failure-{index}",
                            user_id="temporary-user",
                            mode="text2img",
                            status="error",
                            error_message="Error in Node Text to Image: rejected by the safety system",
                        )
                    )
                    await session.commit()
                session.expire_all()
                user = await session.get(User, "temporary-user")
                assert user.abnormal_failure_count == 4
                remaining = user.disabled_until - utcnow_naive()
                assert timedelta(hours=2, minutes=59) < remaining <= timedelta(hours=3)
        finally:
            await engine.dispose()

    asyncio.run(run())
