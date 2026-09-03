import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, BigInteger, Float, DateTime, Text, ForeignKey, text
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost/dbname")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(AsyncAttrs, DeclarativeBase):
    pass

class Driver(Base):
    __tablename__ = "drivers"
    id: Mapped[int] = mapped_keyword = mapped_column(Integer, primary_key=True, autoincrement=True) if hasattr(Base, 'metadata') else mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(100), nullable=True)
    wallet_balance: Mapped[float] = mapped_column(Float, default=0.0)
    is_approved: Mapped[bool] = mapped_column(default=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=True)

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    customer_id: Mapped[int] = mapped_column(BigInteger)
    vehicle: Mapped[str] = mapped_column(String(50))
    date_str: Mapped[str] = mapped_column(String(20))
    time_str: Mapped[str] = mapped_column(String(20))
    hours: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(String(255))
    passengers: Mapped[int] = mapped_column(Integer)
    fare_mmk: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_PAYMENT")
    payment_method: Mapped[str] = mapped_column(String(50), nullable=True)
    payment_receipt_file_id: Mapped[str] = mapped_column(String(255), nullable=True)
    driver_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    driver_name: Mapped[str] = mapped_column(String(100), nullable=True)
    customer_phone: Mapped[str] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    driver_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Float)
    type: Mapped[str] = mapped_column(String(50))
    booking_id: Mapped[str] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Automatically patch existing tables if columns are missing
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(30);"))
        await conn.execute(text("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS phone VARCHAR(30);"))
