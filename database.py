import os
from datetime import datetime

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    Float,
    DateTime,
    Text,
    text,
)


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost/dbname",
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql+asyncpg://", 1
    )
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(100), nullable=True)
    wallet_balance: Mapped[float] = mapped_column(Float, default=0.0)
    is_approved: Mapped[bool] = mapped_column(
        default=False, index=True
    )
    phone: Mapped[str] = mapped_column(String(30), nullable=True)
    car_model: Mapped[str] = mapped_column(String(100), nullable=True)
    license_plate: Mapped[str] = mapped_column(
        String(50), nullable=True
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True
    )
    customer_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )
    vehicle: Mapped[str] = mapped_column(
        String(50), index=True
    )

    date_str: Mapped[str] = mapped_column(
        String(20), index=True
    )
    time_str: Mapped[str] = mapped_column(String(20))
    hours: Mapped[int] = mapped_column(Integer)

    booking_mode: Mapped[str] = mapped_column(
        String(20), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    location: Mapped[str] = mapped_column(Text)
    passengers: Mapped[int] = mapped_column(Integer)
    fare_mmk: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(
        String(30),
        default="PENDING_PAYMENT",
        index=True,
    )

    payment_method: Mapped[str] = mapped_column(
        String(50), nullable=True
    )
    payment_receipt_file_id: Mapped[str] = mapped_column(
        String(255), nullable=True
    )
    driver_id: Mapped[int] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    driver_name: Mapped[str] = mapped_column(
        String(100), nullable=True
    )
    customer_phone: Mapped[str] = mapped_column(
        String(30), nullable=True
    )

    # GPS / live tracking
    pickup_lat: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    pickup_lng: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    drop_lat: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    drop_lng: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    driver_lat: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    driver_lng: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    live_map_msg_id: Mapped[int] = mapped_column(
        BigInteger, nullable=True
    )

    # Kilo Car route calculation
    route_distance_km: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    route_duration_minutes: Mapped[float] = mapped_column(
        Float, nullable=True
    )
    distance_source: Mapped[str] = mapped_column(
        String(100), nullable=True
    )

    # Driver cancellation workflow
    # CANCEL_REQUESTED freezes the booking until admin decides.
    cancel_previous_status: Mapped[str] = mapped_column(
        String(30), nullable=True
    )
    cancel_reason: Mapped[str] = mapped_column(
        Text, nullable=True
    )
    cancel_requested_by: Mapped[int] = mapped_column(
        BigInteger, nullable=True
    )
    cancel_requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    driver_telegram_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )
    amount: Mapped[float] = mapped_column(Float)
    type: Mapped[str] = mapped_column(
        String(50), index=True
    )
    booking_id: Mapped[str] = mapped_column(
        String(30), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Existing compatibility migrations
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(30);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE drivers "
                "ADD COLUMN IF NOT EXISTS phone VARCHAR(30);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE drivers "
                "ADD COLUMN IF NOT EXISTS car_model VARCHAR(100);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE drivers "
                "ADD COLUMN IF NOT EXISTS license_plate VARCHAR(50);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS driver_lat FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS driver_lng FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS live_map_msg_id BIGINT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ALTER COLUMN location TYPE TEXT;"
            )
        )

        # Phase 1 booking/scheduling fields
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS booking_mode VARCHAR(20);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS pickup_lat FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS pickup_lng FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS drop_lat FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS drop_lng FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS route_distance_km FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS route_duration_minutes FLOAT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS distance_source VARCHAR(100);"
            )
        )

        # Driver cancellation workflow fields
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS cancel_previous_status VARCHAR(30);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS cancel_reason TEXT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS cancel_requested_by BIGINT;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE bookings "
                "ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP;"
            )
        )

        # Helpful indexes
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_bookings_driver_schedule "
                "ON bookings (driver_id, start_at, end_at);"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_bookings_status_date "
                "ON bookings (status, date_str);"
            )
        )
