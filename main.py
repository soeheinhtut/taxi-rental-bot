import os
import logging
import calendar
import math
import asyncio  # <== Update Phase1_18Sep26
import json  # <== Update Phase1_18Sep26
from urllib.request import Request as URLRequest, urlopen  # <== Update Phase1_18Sep26
from datetime import datetime, timedelta  # <== Update Phase1_18Sep26
from zoneinfo import ZoneInfo  # <== Update Phase1_18Sep26
from fastapi import FastAPI, Request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from sqlalchemy import select
from database import AsyncSessionLocal, Booking, Driver, WalletTransaction, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")  
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))  
DRIVER_GROUP_ID = int(os.getenv("DRIVER_GROUP_ID", "0"))
RUN_MODE = os.getenv("RUN_MODE", "polling")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

MMK_PER_POINT = 1000

PACKAGE_RATES = { 
    "Sedan": {3: 75000, 5: 100000, 8: 160000}, 
    "SUV": {3: 90000, 5: 125000, 8: 200000}, 
    "Alphard / VIP": {3: 105000, 5: 150000, 8: 240000} 
} 

# <== Update Phase1_18Sep26: Phase 1 service, scheduling, and Kilo Car settings.
YANGON_TZ = ZoneInfo("Asia/Yangon")  # <== Update Phase1_18Sep26
SERVICE_START_MINUTES = 5 * 60  # <== Update Phase1_18Sep26
SERVICE_END_MINUTES = 22 * 60  # <== Update Phase1_18Sep26
TIME_STEP_MINUTES = 15  # <== Update Phase1_18Sep26
INSTANT_DISPATCH_LEAD_MINUTES = 15  # <== Update Phase1_18Sep26
DRIVER_SCHEDULE_BUFFER_MINUTES = 30  # <== Update Phase1_18Sep26
KILO_DRIVER_BLOCK_MINUTES = 60  # <== Update Phase1_18Sep26
KILO_BASE_FARE = 8500.0  # <== Update Phase1_18Sep26
KILO_INCLUDED_KM = 5.0  # <== Update Phase1_18Sep26
KILO_EXTRA_KM_RATE = 1100.0  # <== Update Phase1_18Sep26
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")  # <== Update Phase1_18Sep26
OSRM_TIMEOUT_SECONDS = float(os.getenv("OSRM_TIMEOUT_SECONDS", "8"))  # <== Update Phase1_18Sep26

TOPUP_PACKAGES = {
    "pkg_1": {"points": 1, "price": 1 * MMK_PER_POINT},       
    "pkg_10": {"points": 10, "price": 10 * MMK_PER_POINT},
    "pkg_50": {"points": 50, "price": 50 * MMK_PER_POINT},
    "pkg_100": {"points": 100, "price": 100 * MMK_PER_POINT},
    "pkg_1000": {"points": 1000, "price": 1000 * MMK_PER_POINT},
}

# <== Update Phase1_18Sep26: Added booking mode and final confirmation states.
VEHICLE, BOOKING_MODE, DATE, TIME, HOURS, LOCATION, DROP_LOCATION, PASSENGERS, C_PHONE, CONFIRM_BOOKING = range(10)  # <== Update Phase1_18Sep26
D_NAME, D_PHONE, D_VEHICLE, D_PLATE = range(10, 14)  # <== Update Phase1_18Sep26
TOPUP_PKG, TOPUP_RECEIPT = range(14, 16)  # <== Update Phase1_18Sep26

app = FastAPI()
telegram_app = None

def calculate_distance(lat1, lon1, lat2, lon2):  
    R = 6371.0  
    dlat = math.radians(lat2 - lat1)  
    dlon = math.radians(lon2 - lon1)  
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2  
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))  
    return R * c  

def get_yangon_now():  # <== Update Phase1_18Sep26
    return datetime.now(YANGON_TZ)  # <== Update Phase1_18Sep26


def format_picker_time(hour24, minute):  # <== Update Phase1_18Sep26
    return datetime.strptime(f"{hour24:02d}:{minute:02d}", "%H:%M").strftime("%I:%M %p")  # <== Update Phase1_18Sep26


def normalize_picker_time(hour24, minute, min_total=None):  # <== Update Phase1_18Sep26
    total_minutes = hour24 * 60 + minute  # <== Update Phase1_18Sep26
    min_allowed = SERVICE_START_MINUTES if min_total is None else max(SERVICE_START_MINUTES, int(min_total))  # <== Update Phase1_18Sep26
    min_allowed = ((min_allowed + TIME_STEP_MINUTES - 1) // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  # <== Update Phase1_18Sep26
    total_minutes = max(min_allowed, min(SERVICE_END_MINUTES, total_minutes))  # <== Update Phase1_18Sep26
    total_minutes = (total_minutes // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  # <== Update Phase1_18Sep26
    total_minutes = max(min_allowed, min(SERVICE_END_MINUTES, total_minutes))  # <== Update Phase1_18Sep26
    return total_minutes // 60, total_minutes % 60  # <== Update Phase1_18Sep26


def next_quarter_hour(dt_obj, lead_minutes=0):  # <== Update Phase1_18Sep26
    candidate = dt_obj + timedelta(minutes=lead_minutes)  # <== Update Phase1_18Sep26
    minute_floor = (candidate.minute // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  # <== Update Phase1_18Sep26
    candidate = candidate.replace(minute=minute_floor, second=0, microsecond=0)  # <== Update Phase1_18Sep26
    if candidate.minute < dt_obj.minute or (candidate == dt_obj and lead_minutes > 0):  # <== Update Phase1_18Sep26
        candidate += timedelta(minutes=TIME_STEP_MINUTES)  # <== Update Phase1_18Sep26
    if lead_minutes > 0 and candidate < dt_obj + timedelta(minutes=lead_minutes):  # <== Update Phase1_18Sep26
        candidate += timedelta(minutes=TIME_STEP_MINUTES)  # <== Update Phase1_18Sep26
    return candidate  # <== Update Phase1_18Sep26


def get_default_time_for_date(selected_date):  # <== Update Phase1_18Sep26
    today = get_yangon_now().date()  # <== Update Phase1_18Sep26
    selected = datetime.strptime(selected_date, "%Y-%m-%d").date()  # <== Update Phase1_18Sep26
    if selected > today:  # <== Update Phase1_18Sep26
        return 5, 0, SERVICE_START_MINUTES  # <== Update Phase1_18Sep26
    now = get_yangon_now()  # <== Update Phase1_18Sep26
    minimum = next_quarter_hour(now, INSTANT_DISPATCH_LEAD_MINUTES)  # <== Update Phase1_18Sep26
    minimum_total = minimum.hour * 60 + minimum.minute  # <== Update Phase1_18Sep26
    minimum_total = max(SERVICE_START_MINUTES, min(SERVICE_END_MINUTES, minimum_total))  # <== Update Phase1_18Sep26
    return normalize_picker_time(minimum.hour, minimum.minute, minimum_total)[0], normalize_picker_time(minimum.hour, minimum.minute, minimum_total)[1], minimum_total  # <== Update Phase1_18Sep26


def get_time_picker_keyboard(hour24=5, minute=0, min_total=None):  # <== Update Phase1_18Sep26
    hour24, minute = normalize_picker_time(hour24, minute, min_total)  # <== Update Phase1_18Sep26
    current_time = format_picker_time(hour24, minute)  # <== Update Phase1_18Sep26
    min_allowed = SERVICE_START_MINUTES if min_total is None else max(SERVICE_START_MINUTES, int(min_total))  # <== Update Phase1_18Sep26

    current_total = hour24 * 60 + minute  # <== Update Phase1_18Sep26
    prev_total = max(min_allowed, current_total - TIME_STEP_MINUTES)  # <== Update Phase1_18Sep26
    next_total = min(SERVICE_END_MINUTES, current_total + TIME_STEP_MINUTES)  # <== Update Phase1_18Sep26

    keyboard = [  # <== Update Phase1_18Sep26
        [  # <== Update Phase1_18Sep26
            InlineKeyboardButton("⬆️ +1 Hour", callback_data=f"time_hour_{min(SERVICE_END_MINUTES // 60, hour24 + 1)}"),  # <== Update Phase1_18Sep26
            InlineKeyboardButton("⬆️ +15 Min", callback_data=f"time_total_{next_total}")  # <== Update Phase1_18Sep26
        ],  # <== Update Phase1_18Sep26
        [  # <== Update Phase1_18Sep26
            InlineKeyboardButton(f"🕐 {current_time}", callback_data="time_noop"),  # <== Update Phase1_18Sep26
            InlineKeyboardButton("✅ Select", callback_data="time_confirm")  # <== Update Phase1_18Sep26
        ],  # <== Update Phase1_18Sep26
        [  # <== Update Phase1_18Sep26
            InlineKeyboardButton("⬇️ -1 Hour", callback_data=f"time_hour_{max(5, hour24 - 1)}"),  # <== Update Phase1_18Sep26
            InlineKeyboardButton("⬇️ -15 Min", callback_data=f"time_total_{prev_total}")  # <== Update Phase1_18Sep26
        ],  # <== Update Phase1_18Sep26
        [  # <== Update Phase1_18Sep26
            InlineKeyboardButton("ℹ️ 05:00 AM – 10:00 PM • 15-min steps", callback_data="time_noop")  # <== Update Phase1_18Sep26
        ]  # <== Update Phase1_18Sep26
    ]  # <== Update Phase1_18Sep26
    return InlineKeyboardMarkup(keyboard)  # <== Update Phase1_18Sep26


def get_calendar_keyboard(year, month):  # <== Update Phase1_18Sep26
    keyboard = []  # <== Update Phase1_18Sep26
    today = get_yangon_now().date()  # <== Update Phase1_18Sep26
    keyboard.append([InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")])  # <== Update Phase1_18Sep26
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])  # <== Update Phase1_18Sep26

    month_calendar = calendar.monthcalendar(year, month)  # <== Update Phase1_18Sep26
    for week in month_calendar:  # <== Update Phase1_18Sep26
        row = []  # <== Update Phase1_18Sep26
        for day in week:  # <== Update Phase1_18Sep26
            if day == 0:  # <== Update Phase1_18Sep26
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))  # <== Update Phase1_18Sep26
            else:  # <== Update Phase1_18Sep26
                day_date = datetime(year, month, day).date()  # <== Update Phase1_18Sep26
                if day_date < today:  # <== Update Phase1_18Sep26
                    row.append(InlineKeyboardButton("·", callback_data="date_past"))  # <== Update Phase1_18Sep26
                else:  # <== Update Phase1_18Sep26
                    row.append(InlineKeyboardButton(str(day), callback_data=f"date_{year}_{month}_{day}"))  # <== Update Phase1_18Sep26
        keyboard.append(row)  # <== Update Phase1_18Sep26

    current_month_start = datetime(today.year, today.month, 1).date()  # <== Update Phase1_18Sep26
    shown_month_start = datetime(year, month, 1).date()  # <== Update Phase1_18Sep26
    prev_month = month - 1 if month > 1 else 12  # <== Update Phase1_18Sep26
    prev_year = year if month > 1 else year - 1  # <== Update Phase1_18Sep26
    next_month = month + 1 if month < 12 else 1  # <== Update Phase1_18Sep26
    next_year = year if month < 12 else year + 1  # <== Update Phase1_18Sep26

    prev_button = InlineKeyboardButton("⏪", callback_data=f"cal_{prev_year}_{prev_month}") if shown_month_start > current_month_start else InlineKeyboardButton("⏪", callback_data="ignore")  # <== Update Phase1_18Sep26

    keyboard.append([  # <== Update Phase1_18Sep26
        prev_button,  # <== Update Phase1_18Sep26
        InlineKeyboardButton("📅 Today", callback_data=f"date_{today.year}_{today.month}_{today.day}"),  # <== Update Phase1_18Sep26
        InlineKeyboardButton("⏩", callback_data=f"cal_{next_year}_{next_month}")  # <== Update Phase1_18Sep26
    ])  # <== Update Phase1_18Sep26
    return InlineKeyboardMarkup(keyboard)  # <== Update Phase1_18Sep26


def get_booking_mode_keyboard():  # <== Update Phase1_18Sep26
    return InlineKeyboardMarkup([  # <== Update Phase1_18Sep26
        [InlineKeyboardButton("⚡ Book Now (ASAP)", callback_data="mode_instant")],  # <== Update Phase1_18Sep26
        [InlineKeyboardButton("📅 Schedule for Later", callback_data="mode_scheduled")]  # <== Update Phase1_18Sep26
    ])  # <== Update Phase1_18Sep26


def parse_booking_start(date_str, time_str):  # <== Update Phase1_18Sep26
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p").replace(tzinfo=YANGON_TZ)  # <== Update Phase1_18Sep26


def format_end_time(start_dt, minutes):  # <== Update Phase1_18Sep26
    return (start_dt + timedelta(minutes=minutes)).strftime("%I:%M %p")  # <== Update Phase1_18Sep26


def get_booking_window(date_str, time_str, vehicle, hours):  # <== Update Phase1_18Sep26
    start_dt = parse_booking_start(date_str, time_str)  # <== Update Phase1_18Sep26
    if vehicle == "Kilo Car":  # <== Update Phase1_18Sep26
        stored_hours = max(1, int(hours or 0))  # <== Update Phase1_18Sep26
        duration_minutes = max(KILO_DRIVER_BLOCK_MINUTES, stored_hours * 60)  # <== Update Phase1_18Sep26
    else:  # <== Update Phase1_18Sep26
        duration_minutes = max(1, int(hours)) * 60  # <== Update Phase1_18Sep26
    return start_dt, start_dt + timedelta(minutes=duration_minutes)  # <== Update Phase1_18Sep26


def calculate_kilo_fare(distance_km):  # <== Update Phase1_18Sep26
    if distance_km <= KILO_INCLUDED_KM:  # <== Update Phase1_18Sep26
        return KILO_BASE_FARE  # <== Update Phase1_18Sep26
    extra_km = math.ceil(distance_km - KILO_INCLUDED_KM)  # <== Update Phase1_18Sep26
    return KILO_BASE_FARE + (extra_km * KILO_EXTRA_KM_RATE)  # <== Update Phase1_18Sep26


async def get_road_route_km_duration(pickup_lat, pickup_lng, drop_lat, drop_lng):  # <== Update Phase1_18Sep26
    """
    Use OSRM's road-routing API for driving distance and duration.
    Returns (distance_km, duration_minutes, source).
    Falls back to straight-line Haversine distance if routing is unavailable.
    """  # <== Update Phase1_18Sep26
    url = (  # <== Update Phase1_18Sep26
        f"{OSRM_BASE_URL}/route/v1/driving/"  # <== Update Phase1_18Sep26
        f"{pickup_lng},{pickup_lat};{drop_lng},{drop_lat}?overview=false"  # <== Update Phase1_18Sep26
    )  # <== Update Phase1_18Sep26

    def _request():  # <== Update Phase1_18Sep26
        req = URLRequest(url, headers={"User-Agent": "MMDRIVE-Car-Rental-Bot/1.0"})  # <== Update Phase1_18Sep26
        with urlopen(req, timeout=OSRM_TIMEOUT_SECONDS) as response:  # <== Update Phase1_18Sep26
            return json.loads(response.read().decode("utf-8"))  # <== Update Phase1_18Sep26

    try:  # <== Update Phase1_18Sep26
        data = await asyncio.to_thread(_request)  # <== Update Phase1_18Sep26
        if data.get("code") != "Ok" or not data.get("routes"):  # <== Update Phase1_18Sep26
            raise ValueError(data.get("message", "No route returned"))  # <== Update Phase1_18Sep26
        route = data["routes"][0]  # <== Update Phase1_18Sep26
        return float(route["distance"]) / 1000.0, float(route["duration"]) / 60.0, "Road distance (OSRM)"  # <== Update Phase1_18Sep26
    except Exception as e:  # <== Update Phase1_18Sep26
        straight_km = calculate_distance(pickup_lat, pickup_lng, drop_lat, drop_lng)  # <== Update Phase1_18Sep26
        logger.warning(f"OSRM route lookup failed, using Haversine fallback: {e}")  # <== Update Phase1_18Sep26
        return straight_km, None, "Estimated straight-line distance (routing unavailable)"  # <== Update Phase1_18Sep26


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type != "private":
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("🚗 Book a Car", callback_data="start_booking")],
        [InlineKeyboardButton("👨‍✈️ Driver Register", callback_data="driver_register")],
        [InlineKeyboardButton("💳 Driver Top Up", callback_data="topup_start")],
        [InlineKeyboardButton("💰 Driver Profile & Check Balance", callback_data="driver_balance")]
    ]
    await update.message.reply_text(
        "Welcome to MMDRIVE Car Rental Service! Please choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def check_balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Driver).where(Driver.telegram_id == query.from_user.id))
        driver = res.scalar_one_or_none()
        
        if not driver or not driver.is_approved:
            await query.edit_message_text("❌ You are not an approved driver yet.")
            return

        text = (
            f"👤 **Driver Profile & Wallet Status**\n\n"
            f"📛 Name: {driver.name}\n"
            f"💰 Point Balance: **{driver.wallet_balance:,.0f} Points**\n"
            f"📞 Phone: `{driver.phone}`\n"
            f"🚙 Car Model: {driver.car_model}\n"
            f"🔢 License Plate: `{driver.license_plate}`"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

async def check_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Driver).where(Driver.telegram_id == update.effective_user.id))
        driver = res.scalar_one_or_none()
        
        if not driver or not driver.is_approved:
            await update.message.reply_text("❌ You are not an approved driver yet.")
            return

        text = (
            f"👤 **Driver Profile & Wallet Status**\n\n"
            f"📛 Name: {driver.name}\n"
            f"💰 Point Balance: **{driver.wallet_balance:,.0f} Points**\n"
            f"📞 Phone: `{driver.phone}`\n"
            f"🚙 Car Model: {driver.car_model}\n"
            f"🔢 License Plate: `{driver.license_plate}`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

async def start_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🚕 Kilo Car (Min 5km 8,500 MMK)", callback_data="Kilo Car")], 
        [InlineKeyboardButton("Sedan", callback_data="Sedan")],                  
        [InlineKeyboardButton("SUV", callback_data="SUV")],                      
        [InlineKeyboardButton("Alphard / VIP", callback_data="Alphard / VIP")] 
    ]
    await query.edit_message_text(
        "🚘 Select Vehicle Type:\n*(Note: Available Within Yangon City)*", 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode="Markdown" 
    )
    return VEHICLE

async def vehicle_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["vehicle"] = query.data

    await query.edit_message_text(
        f"🚘 Vehicle: **{query.data}**\n\nChoose booking type:",
        reply_markup=get_booking_mode_keyboard(),  # <== Update Phase1_18Sep26
        parse_mode="Markdown"
    )
    return BOOKING_MODE  # <== Update Phase1_18Sep26


async def booking_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # <== Update Phase1_18Sep26
    query = update.callback_query  # <== Update Phase1_18Sep26
    await query.answer()  # <== Update Phase1_18Sep26
    mode = query.data  # <== Update Phase1_18Sep26
    today = get_yangon_now().date()  # <== Update Phase1_18Sep26

    if mode == "mode_instant":  # <== Update Phase1_18Sep26
        now = get_yangon_now()  # <== Update Phase1_18Sep26
        if now.hour * 60 + now.minute >= SERVICE_END_MINUTES:  # <== Update Phase1_18Sep26
            await query.edit_message_text(
                "⏰ **Instant booking is currently closed.**\n\n"
                "Service hours: 05:00 AM – 10:00 PM.\n"  # <== Update Phase1_18Sep26
                "Please choose **Schedule for Later**.",  # <== Update Phase1_18Sep26
                reply_markup=get_booking_mode_keyboard(),  # <== Update Phase1_18Sep26
                parse_mode="Markdown"
            )
            return BOOKING_MODE  # <== Update Phase1_18Sep26

        pickup_dt = next_quarter_hour(now, INSTANT_DISPATCH_LEAD_MINUTES)  # <== Update Phase1_18Sep26
        if pickup_dt.hour * 60 + pickup_dt.minute > SERVICE_END_MINUTES:  # <== Update Phase1_18Sep26
            await query.edit_message_text(
                "⏰ **Instant booking is unavailable for the rest of today.**\n\n"
                "Please choose **Schedule for Later**.",  # <== Update Phase1_18Sep26
                reply_markup=get_booking_mode_keyboard(),  # <== Update Phase1_18Sep26
                parse_mode="Markdown"
            )
            return BOOKING_MODE  # <== Update Phase1_18Sep26

        context.user_data["booking_mode"] = "INSTANT"  # <== Update Phase1_18Sep26
        context.user_data["date"] = pickup_dt.strftime("%Y-%m-%d")  # <== Update Phase1_18Sep26
        context.user_data["time"] = pickup_dt.strftime("%I:%M %p")  # <== Update Phase1_18Sep26
        context.user_data["time_hour"] = pickup_dt.hour  # <== Update Phase1_18Sep26
        context.user_data["time_minute"] = pickup_dt.minute  # <== Update Phase1_18Sep26
        context.user_data["time_min_total"] = SERVICE_START_MINUTES  # <== Update Phase1_18Sep26

        return await proceed_after_time_selection(query, context, instant=True)  # <== Update Phase1_18Sep26

    if mode == "mode_scheduled":  # <== Update Phase1_18Sep26
        context.user_data["booking_mode"] = "SCHEDULED"  # <== Update Phase1_18Sep26
        await query.edit_message_text(
            f"🚘 Vehicle: **{context.user_data.get('vehicle', 'Sedan')}**\n\n📅 Select Pickup Date:",
            reply_markup=get_calendar_keyboard(today.year, today.month),  # <== Update Phase1_18Sep26
            parse_mode="Markdown"
        )
        return DATE  # <== Update Phase1_18Sep26

    await query.edit_message_text("❌ Invalid booking type. Please try again.")  # <== Update Phase1_18Sep26
    return BOOKING_MODE  # <== Update Phase1_18Sep26


async def proceed_after_time_selection(query, context, instant=False):  # <== Update Phase1_18Sep26
    selected_time = context.user_data["time"]  # <== Update Phase1_18Sep26
    vehicle = context.user_data.get("vehicle", "Sedan")  # <== Update Phase1_18Sep26

    if vehicle in ["TAXI", "Kilo Car"]:  # <== Update Phase1_18Sep26
        location_keyboard = ReplyKeyboardMarkup(  # <== Update Phase1_18Sep26
            [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
            one_time_keyboard=True, resize_keyboard=True
        )
        mode_label = "⚡ Book Now (ASAP)" if instant else "📅 Scheduled"  # <== Update Phase1_18Sep26
        pickup_prompt = (  # <== Update Phase1_18Sep26
            "📍 Please share your exact **GPS Pickup Location**."  # <== Update Phase1_18Sep26
            if vehicle == "Kilo Car"  # <== Update Phase1_18Sep26
            else "📍 Please share your exact GPS Pickup Location or type your address:"  # <== Update Phase1_18Sep26
        )  # <== Update Phase1_18Sep26
        await query.edit_message_text(
            f"🕐 Pickup Time: **{selected_time}**\n"
            f"📌 Booking Type: **{mode_label}**\n\n"
            f"{pickup_prompt}",
            parse_mode="Markdown"
        )
        await query.message.reply_text("Click button to send GPS location:", reply_markup=location_keyboard)
        return LOCATION

    packages = PACKAGE_RATES.get(vehicle, {3: 75000, 5: 100000, 8: 160000})  # <== Update Phase1_18Sep26
    keyboard = [
        [InlineKeyboardButton(f"3 Hours ({packages[3]:,.0f} MMK)", callback_data="3")],
        [InlineKeyboardButton(f"5 Hours ({packages[5]:,.0f} MMK)", callback_data="5")],
        [InlineKeyboardButton(f"8 Hours ({packages[8]:,.0f} MMK)", callback_data="8")]
    ]

    mode_label = "⚡ Book Now (ASAP)" if instant else "📅 Scheduled"  # <== Update Phase1_18Sep26
    await query.edit_message_text(
        f"🕐 Pickup Time: **{selected_time}**\n"
        f"📌 Booking Type: **{mode_label}**\n\n"
        f"⏱ Select Rental Package for **{vehicle}**:\n"
        f"*(Note: Available Within Yangon City)*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return HOURS


async def date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ignore":  # <== Update Phase1_18Sep26
        return DATE  # <== Update Phase1_18Sep26

    if data == "date_past":  # <== Update Phase1_18Sep26
        await query.answer("❌ Past dates are not available.", show_alert=True)  # <== Update Phase1_18Sep26
        return DATE  # <== Update Phase1_18Sep26

    if data.startswith("cal_"):
        _, year, month = data.split("_")
        reply_markup = get_calendar_keyboard(int(year), int(month))
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return DATE

    if data.startswith("date_"):
        _, year, month, day = data.split("_")
        selected_date = f"{year}-{int(month):02d}-{int(day):02d}"  # <== Update Phase1_18Sep26
        selected_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()  # <== Update Phase1_18Sep26

        if selected_date_obj < get_yangon_now().date():  # <== Update Phase1_18Sep26
            await query.answer("❌ Past dates are not available.", show_alert=True)  # <== Update Phase1_18Sep26
            return DATE  # <== Update Phase1_18Sep26

        context.user_data["date"] = selected_date  # <== Update Phase1_18Sep26
        hour24, minute, min_total = get_default_time_for_date(selected_date)  # <== Update Phase1_18Sep26
        context.user_data["time_hour"] = hour24  # <== Update Phase1_18Sep26
        context.user_data["time_minute"] = minute  # <== Update Phase1_18Sep26
        context.user_data["time_min_total"] = min_total  # <== Update Phase1_18Sep26

        reply_markup = get_time_picker_keyboard(hour24, minute, min_total)  # <== Update Phase1_18Sep26
        await query.edit_message_text(
            f"📅 Date: **{selected_date}**\n\n"
            f"🕐 **Select Pickup Time**\n\n"
            f"Use the scroll buttons to choose a time in 15-minute steps:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return TIME


async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # <== Update Phase1_18Sep26
    query = update.callback_query
    data = query.data

    hour24 = int(context.user_data.get("time_hour", 5))  # <== Update Phase1_18Sep26
    minute = int(context.user_data.get("time_minute", 0))  # <== Update Phase1_18Sep26
    min_total = int(context.user_data.get("time_min_total", SERVICE_START_MINUTES))  # <== Update Phase1_18Sep26

    if data == "time_noop":  # <== Update Phase1_18Sep26
        await query.answer()  # <== Update Phase1_18Sep26
        return TIME  # <== Update Phase1_18Sep26

    if data.startswith("time_hour_"):  # <== Update Phase1_18Sep26
        await query.answer()  # <== Update Phase1_18Sep26
        hour24 = int(data.split("_")[2])  # <== Update Phase1_18Sep26
        hour24, minute = normalize_picker_time(hour24, minute, min_total)  # <== Update Phase1_18Sep26
        context.user_data["time_hour"] = hour24  # <== Update Phase1_18Sep26
        context.user_data["time_minute"] = minute  # <== Update Phase1_18Sep26
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute, min_total))
        return TIME

    if data.startswith("time_total_"):  # <== Update Phase1_18Sep26
        await query.answer()  # <== Update Phase1_18Sep26
        total_minutes = int(data.split("_")[2])  # <== Update Phase1_18Sep26
        hour24 = total_minutes // 60  # <== Update Phase1_18Sep26
        minute = total_minutes % 60  # <== Update Phase1_18Sep26
        hour24, minute = normalize_picker_time(hour24, minute, min_total)  # <== Update Phase1_18Sep26
        context.user_data["time_hour"] = hour24  # <== Update Phase1_18Sep26
        context.user_data["time_minute"] = minute  # <== Update Phase1_18Sep26
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute, min_total))
        return TIME

    if data == "time_confirm":  # <== Update Phase1_18Sep26
        selected_total = hour24 * 60 + minute  # <== Update Phase1_18Sep26
        if selected_total < min_total or selected_total > SERVICE_END_MINUTES:  # <== Update Phase1_18Sep26
            await query.answer("❌ Please choose a valid service time.", show_alert=True)  # <== Update Phase1_18Sep26
            return TIME  # <== Update Phase1_18Sep26

        selected_time = format_picker_time(hour24, minute)
        context.user_data["time"] = selected_time
        await query.answer("✅ Time selected")  # <== Update Phase1_18Sep26
        return await proceed_after_time_selection(query, context, instant=False)  # <== Update Phase1_18Sep26

    await query.answer("❌ Invalid time option.", show_alert=True)  # <== Update Phase1_18Sep26
    return TIME  # <== Update Phase1_18Sep26


async def hours_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    hours = int(query.data)
    vehicle = context.user_data["vehicle"]

    total_fare = PACKAGE_RATES[vehicle][hours]

    context.user_data["hours"] = hours
    context.user_data["fare"] = total_fare

    location_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
        one_time_keyboard=True, resize_keyboard=True
    )

    start_dt = parse_booking_start(context.user_data["date"], context.user_data["time"])  # <== Update Phase1_18Sep26
    end_dt = start_dt + timedelta(hours=hours)  # <== Update Phase1_18Sep26
    context.user_data["end_time"] = end_dt.strftime("%I:%M %p")  # <== Update Phase1_18Sep26

    await query.edit_message_text(
        f"⏱ **Package Selected:** {hours} Hours\n"
        f"🕐 Pickup: **{context.user_data['time']}**\n"
        f"🏁 Scheduled End: **{context.user_data['end_time']}**\n"
        f"💰 **Total Fare:** {total_fare:,.0f} MMK\n\n"
        f"📍 Please share your exact GPS pickup location or type your address:",
        parse_mode="Markdown"
    )
    await query.message.reply_text("Click button to send GPS location:", reply_markup=location_keyboard)
    return LOCATION


async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if loc:
        context.user_data["location"] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}" 
        context.user_data["pickup_lat"] = loc.latitude  
        context.user_data["pickup_lng"] = loc.longitude  
    else: 
        context.user_data["location"] = update.message.text 

    if context.user_data.get("vehicle") in ["TAXI", "Kilo Car"]:  
        if context.user_data.get("vehicle") == "Kilo Car" and (
            context.user_data.get("pickup_lat") is None or context.user_data.get("pickup_lng") is None
        ):  # <== Update Phase1_18Sep26
            await update.message.reply_text(  # <== Update Phase1_18Sep26
                "❌ Kilo Car requires GPS pickup location. Please tap the 📎 / Location button and send your current pickup point."  # <== Update Phase1_18Sep26
            )  # <== Update Phase1_18Sep26
            return LOCATION  # <== Update Phase1_18Sep26

        await update.message.reply_text( 
            "📍 Please click the 📎 (paperclip) icon, choose **Location**, "
            "select your **Drop-off point** on the map, and send it.",
            reply_markup=ReplyKeyboardRemove()
        ) 
        return DROP_LOCATION

    await update.message.reply_text(
        "👥 How many passengers will be riding?",
        reply_markup=ReplyKeyboardRemove()
    ) 
    return PASSENGERS 


async def drop_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location  
    if loc:  
        context.user_data["drop_location"] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"  
        context.user_data["drop_lat"] = loc.latitude  
        context.user_data["drop_lng"] = loc.longitude  
    else:  
        await update.message.reply_text(
            "❌ Text input is disabled. Please use 📎 (paperclip) -> Location to choose on the map."
        ) 
        return DROP_LOCATION 

    await update.message.reply_text(
        "👥 How many passengers will be riding?",
        reply_markup=ReplyKeyboardRemove()
    )
    return PASSENGERS


async def passengers_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data["passengers"] = update.message.text 

    phone_keyboard = ReplyKeyboardMarkup( 
        [[KeyboardButton("📞 Share Contact Phone", request_contact=True)]], 
        one_time_keyboard=True, resize_keyboard=True 
    ) 
    await update.message.reply_text( 
        "📞 Please enter or share your **Phone Contact Number**:", 
        reply_markup=phone_keyboard 
    ) 
    return C_PHONE 


async def customer_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    contact = update.message.contact 
    phone = contact.phone_number if contact else update.message.text 

    context.user_data["customer_phone"] = phone 
    data = context.user_data 
    data["preview_customer_id"] = update.message.from_user.id  # <== Update Phase1_18Sep26

    booking_id = f"RNT-{get_yangon_now().strftime('%Y%m%d')}-{int(get_yangon_now().timestamp()) % 10000}"  # <== Update Phase1_18Sep26
    vehicle = data["vehicle"]

    if vehicle in ["TAXI", "Kilo Car"]:
        p_lat = data.get("pickup_lat")
        p_lng = data.get("pickup_lng")
        d_lat = data.get("drop_lat")
        d_lng = data.get("drop_lng")

        if p_lat is None or p_lng is None or d_lat is None or d_lng is None:  # <== Update Phase1_18Sep26
            await update.message.reply_text(
                "❌ Pickup and drop-off GPS coordinates are required for Kilo Car."
            )
            return DROP_LOCATION

        direction_link = f"https://www.google.com/maps/dir/?api=1&origin={p_lat},{p_lng}&destination={d_lat},{d_lng}"

        road_km, route_minutes, distance_source = await get_road_route_km_duration(  # <== Update Phase1_18Sep26
            p_lat, p_lng, d_lat, d_lng
        )  # <== Update Phase1_18Sep26
        calculated_fare = calculate_kilo_fare(road_km)  # <== Update Phase1_18Sep26

        final_location = (
            f"**Pickup:** {data['location']}\n"
            f"**Drop-off:** {data.get('drop_location', 'N/A')}\n"
            f"**🗺️ Route Map:** {direction_link}"
        )

        hours_label = "Point-to-Point (Kilo Car)"
        points_required = 1.0
        hours_db_value = max(1, math.ceil(route_minutes / 60.0)) if route_minutes is not None else 1  # <== Update Phase1_18Sep26
        fare_display = f"{calculated_fare:,.0f} MMK ({road_km:.1f} km)"  # <== Update Phase1_18Sep26
        fare_db_value = calculated_fare

        data["route_distance_km"] = road_km  # <== Update Phase1_18Sep26
        data["route_duration_minutes"] = route_minutes  # <== Update Phase1_18Sep26
        data["distance_source"] = distance_source  # <== Update Phase1_18Sep26
        data["fare"] = calculated_fare  # <== Update Phase1_18Sep26

        if route_minutes is not None:  # <== Update Phase1_18Sep26
            start_dt = parse_booking_start(data["date"], data["time"])  # <== Update Phase1_18Sep26
            eta_dt = start_dt + timedelta(minutes=round(route_minutes))  # <== Update Phase1_18Sep26
            data["eta_time"] = eta_dt.strftime("%I:%M %p")  # <== Update Phase1_18Sep26
        else:
            data["eta_time"] = None  # <== Update Phase1_18Sep26

    else:
        final_location = data["location"]
        hours_label = f"{data['hours']} Hours"
        points_required = float(data["hours"])
        fare_display = f"{data['fare']:,.0f} MMK"
        fare_db_value = float(data["fare"])
        hours_db_value = int(data["hours"])

    start_dt = parse_booking_start(data["date"], data["time"])  # <== Update Phase1_18Sep26
    booking_mode_label = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  # <== Update Phase1_18Sep26

    if vehicle == "Kilo Car":
        end_label = f"Estimated Arrival: **{data['eta_time']}**" if data.get("eta_time") else "Arrival: **Routing ETA unavailable**"  # <== Update Phase1_18Sep26
    else:
        end_label = f"Scheduled End: **{data.get('end_time', format_end_time(start_dt, hours_db_value * 60))}**"  # <== Update Phase1_18Sep26

    summary = (  # <== Update Phase1_18Sep26
        f"🧾 **PLEASE CONFIRM YOUR BOOKING**\n\n"
        f"🆔 Booking ID: `{booking_id}`\n"
        f"🚙 Vehicle: {vehicle}\n"
        f"📌 Booking Type: **{booking_mode_label}**\n"
        f"📅 Date: {data['date']}\n"
        f"🕐 Pickup: {data['time']}\n"
        f"{end_label}\n"
        f"⏱ Package: {hours_label}\n"
        f"📍 Location:\n{final_location}\n"
        f"👥 Passengers: {data['passengers']}\n"
        f"📞 Contact: `{phone}`\n"
    )  # <== Update Phase1_18Sep26

    if vehicle == "Kilo Car":  # <== Update Phase1_18Sep26
        summary += (  # <== Update Phase1_18Sep26
            f"\n📏 Road Distance: **{data.get('route_distance_km', 0):.1f} km**\n"
            f"🛣️ Distance Basis: {data.get('distance_source', 'N/A')}\n"
        )  # <== Update Phase1_18Sep26
        if data.get("route_duration_minutes") is not None:  # <== Update Phase1_18Sep26
            summary += f"⏱ Estimated Drive: **{round(data['route_duration_minutes'])} min**\n"  # <== Update Phase1_18Sep26

    summary += f"\n💰 **Total Fare: {fare_display}**\n\n"  # <== Update Phase1_18Sep26
    if vehicle != "Kilo Car":
        summary += "ℹ️ Please confirm that the pickup time, package, and scheduled end time are correct.\n\n"  # <== Update Phase1_18Sep26

    confirm_kb = [  # <== Update Phase1_18Sep26
        [InlineKeyboardButton("✅ Confirm Booking", callback_data=f"booking_confirm_{booking_id}")],  # <== Update Phase1_18Sep26
        [InlineKeyboardButton("❌ Cancel", callback_data="booking_cancel")],  # <== Update Phase1_18Sep26
    ]  # <== Update Phase1_18Sep26

    # Store all derived booking details until the customer confirms.
    data["pending_booking_id"] = booking_id  # <== Update Phase1_18Sep26
    data["pending_final_location"] = final_location  # <== Update Phase1_18Sep26
    data["pending_hours_label"] = hours_label  # <== Update Phase1_18Sep26
    data["pending_points_required"] = points_required  # <== Update Phase1_18Sep26
    data["pending_fare_display"] = fare_display  # <== Update Phase1_18Sep26
    data["pending_fare_db_value"] = fare_db_value  # <== Update Phase1_18Sep26
    data["pending_hours_db_value"] = hours_db_value  # <== Update Phase1_18Sep26

    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(confirm_kb),
        disable_web_page_preview=True
    )
    return CONFIRM_BOOKING  # <== Update Phase1_18Sep26


async def booking_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # <== Update Phase1_18Sep26
    query = update.callback_query  # <== Update Phase1_18Sep26
    await query.answer("Processing booking…")  # <== Update Phase1_18Sep26

    if not query.data.startswith("booking_confirm_"):  # <== Update Phase1_18Sep26
        return CONFIRM_BOOKING  # <== Update Phase1_18Sep26

    data = context.user_data  # <== Update Phase1_18Sep26
    booking_id = query.data.replace("booking_confirm_", "", 1)  # <== Update Phase1_18Sep26

    if data.get("pending_booking_id") != booking_id:  # <== Update Phase1_18Sep26
        await query.edit_message_text("❌ Booking session expired. Please start a new booking.")  # <== Update Phase1_18Sep26
        return ConversationHandler.END  # <== Update Phase1_18Sep26

    vehicle = data["vehicle"]  # <== Update Phase1_18Sep26
    hours_db_value = int(data.get("pending_hours_db_value", 0))  # <== Update Phase1_18Sep26
    fare_db_value = float(data.get("pending_fare_db_value", 0))  # <== Update Phase1_18Sep26
    hours_label = data["pending_hours_label"]  # <== Update Phase1_18Sep26
    points_required = float(data["pending_points_required"])  # <== Update Phase1_18Sep26
    final_location = data["pending_final_location"]  # <== Update Phase1_18Sep26
    fare_display = data["pending_fare_display"]  # <== Update Phase1_18Sep26
    phone = data["customer_phone"]  # <== Update Phase1_18Sep26

    # Final schedule validation before creating the booking.
    start_dt = parse_booking_start(data["date"], data["time"])  # <== Update Phase1_18Sep26
    if start_dt < get_yangon_now() - timedelta(minutes=1):  # <== Update Phase1_18Sep26
        await query.edit_message_text(  # <== Update Phase1_18Sep26
            "⚠️ This pickup time has already passed. Please start a new booking and choose a future time.",  # <== Update Phase1_18Sep26
            parse_mode="Markdown"  # <== Update Phase1_18Sep26
        )  # <== Update Phase1_18Sep26
        return ConversationHandler.END  # <== Update Phase1_18Sep26

    # Persist the exact service window so future admin reports and database-level
    # schedule queries do not need to reconstruct it from text fields.  # <== Update Phase1_18Sep26
    schedule_start_dt, schedule_end_dt = get_booking_window(  # <== Update Phase1_18Sep26
        data["date"], data["time"], vehicle, hours_db_value
    )  # <== Update Phase1_18Sep26

    booking = Booking(
        id=booking_id,
        customer_id=query.from_user.id,
        vehicle=vehicle,
        date_str=data["date"],
        time_str=data["time"],
        hours=hours_db_value,
        booking_mode=data.get("booking_mode", "SCHEDULED"),  # <== Update Phase1_18Sep26
        start_at=schedule_start_dt,  # <== Update Phase1_18Sep26
        end_at=schedule_end_dt,  # <== Update Phase1_18Sep26
        location=final_location,
        passengers=int(data["passengers"]),
        fare_mmk=fare_db_value,
        status="AVAILABLE",
        payment_method="DIRECT",
        payment_receipt_file_id=None,
        driver_id=None,
        driver_name=None,
        customer_phone=phone,
        pickup_lat=data.get("pickup_lat"),  # <== Update Phase1_18Sep26
        pickup_lng=data.get("pickup_lng"),  # <== Update Phase1_18Sep26
        drop_lat=data.get("drop_lat"),  # <== Update Phase1_18Sep26
        drop_lng=data.get("drop_lng"),  # <== Update Phase1_18Sep26
        route_distance_km=data.get("route_distance_km"),  # <== Update Phase1_18Sep26
        route_duration_minutes=data.get("route_duration_minutes"),  # <== Update Phase1_18Sep26
        distance_source=data.get("distance_source"),  # <== Update Phase1_18Sep26
    )

    try:
        async with AsyncSessionLocal() as session:
            session.add(booking)
            await session.commit()
    except Exception as e:
        logger.error(f"Booking creation failed: {e}")  # <== Update Phase1_18Sep26
        await query.edit_message_text("❌ We could not create the booking. Please try again.")  # <== Update Phase1_18Sep26
        return ConversationHandler.END  # <== Update Phase1_18Sep26

    # Persist a better customer-facing confirmation only after DB commit.
    mode_label = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  # <== Update Phase1_18Sep26
    if vehicle == "Kilo Car":
        route_line = (
            f"📏 Road Distance: **{data.get('route_distance_km', 0):.1f} km**\n"
            f"🛣️ Distance Basis: {data.get('distance_source', 'N/A')}\n"
        )
        if data.get("route_duration_minutes") is not None:
            route_line += f"⏱ Estimated Drive: **{round(data['route_duration_minutes'])} min**\n"
            route_line += f"🏁 Estimated Arrival: **{data.get('eta_time', 'N/A')}**\n"
        end_line = route_line
    else:
        end_line = f"🏁 Scheduled End: **{data.get('end_time', 'N/A')}**\n"

    confirmed_text = (
        f"✅ **BOOKING CONFIRMED**\n\n"
        f"🆔 Booking ID: `{booking_id}`\n"
        f"🚙 Vehicle: {vehicle}\n"
        f"📌 Booking Type: **{mode_label}**\n"
        f"📅 Date: {data['date']}\n"
        f"🕐 Pickup: {data['time']}\n"
        f"{end_line}"
        f"⏱ Package: {hours_label}\n"
        f"📍 Location:\n{final_location}\n"
        f"👥 Passengers: {data['passengers']}\n"
        f"📞 Contact: `{phone}`\n\n"
        f"💰 **Total Fare: {fare_display}**\n\n"
        f"🔎 Status: **Finding a driver...**"
    )

    cancel_kb = [[InlineKeyboardButton("❌ Cancel Booking", callback_data=f"ccancel_{booking_id}")]]  # <== Update Phase1_18Sep26
    await query.edit_message_text(
        confirmed_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(cancel_kb),
        disable_web_page_preview=True
    )

    if DRIVER_GROUP_ID:
        try:
            booking_type_line = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  # <== Update Phase1_18Sep26
            if vehicle == "Kilo Car":  # <== Update Phase1_18Sep26
                timing_line = f"🛣️ Road Distance: {data.get('route_distance_km', 0):.1f} km"  # <== Update Phase1_18Sep26
                if data.get("route_duration_minutes") is not None:  # <== Update Phase1_18Sep26
                    timing_line += f" | ETA {data.get('eta_time', 'N/A')}"  # <== Update Phase1_18Sep26
            else:  # <== Update Phase1_18Sep26
                timing_line = f"🏁 End: {data.get('end_time', 'N/A')}"  # <== Update Phase1_18Sep26

            driver_text = (  # <== Update Phase1_18Sep26
                f"🚗 **NEW JOB AVAILABLE**\n\n"
                f"🆔 `{booking.id}`\n"
                f"📌 Booking Type: {booking_type_line}\n"
                f"📅 {booking.date_str} | 🕐 {booking.time_str}\n"
                f"{timing_line}\n"
                f"⏱ Package: {hours_label} | 👥 {booking.passengers} Pax\n"
                f"🚙 Vehicle: {booking.vehicle}\n"
                f"📍 **Location:**\n{final_location}\n\n"
                f"💰 Fare: **{fare_display}**\n"
                f"➕ Commission Deduction: **{points_required:,.0f} Points**"
            )
            kb = [[InlineKeyboardButton("✅ ACCEPT JOB", callback_data=f"accept_{booking.id}")]]
            await context.bot.send_message(
                chat_id=DRIVER_GROUP_ID,
                text=driver_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Failed to send to DRIVER_GROUP_ID: {e}")
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="⚠️ Booking was created, but we could not notify the driver group. Please contact support."
            )

    context.user_data.clear()  # <== Update Phase1_18Sep26
    return ConversationHandler.END  # <== Update Phase1_18Sep26


async def booking_cancelled_before_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # <== Update Phase1_18Sep26
    query = update.callback_query  # <== Update Phase1_18Sep26
    await query.answer("Booking cancelled")  # <== Update Phase1_18Sep26
    await query.edit_message_text("🚫 **Booking cancelled.**\n\nUse /start to make a new booking.", parse_mode="Markdown")  # <== Update Phase1_18Sep26
    context.user_data.clear()  # <== Update Phase1_18Sep26
    return ConversationHandler.END  # <== Update Phase1_18Sep26


async def customer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    b_id = query.data.split("_")[1]
    
    async with AsyncSessionLocal() as session:
        booking = (await session.execute(select(Booking).where(Booking.id == b_id))).scalar_one_or_none()
        if not booking or booking.status in ["CANCELLED", "TRIP_COMPLETED"]:
            await query.edit_message_text(f"{query.message.text}\n\n❌ Cannot cancel this booking.")
            return

        if booking.driver_id:
            driver = (await session.execute(select(Driver).where(Driver.telegram_id == booking.driver_id))).scalar_one_or_none()
            if driver:
                points_refund = 1.0 if booking.vehicle in ["TAXI", "Kilo Car"] else float(booking.hours)
                driver.wallet_balance += points_refund
                session.add(WalletTransaction(driver_telegram_id=driver.telegram_id, amount=points_refund, type="REFUND", booking_id=booking.id))
                
                await context.bot.send_message(
                    chat_id=driver.telegram_id,
                    text=f"⚠️ Customer cancelled Job #{b_id}. Your {points_refund:,.0f} points have been automatically refunded."
                )

        booking.status = "CANCELLED"
        await session.commit()
        await query.edit_message_text(f"{query.message.text}\n\n🚫 **BOOKING CANCELLED**")
# <== Updated 18Sep26

async def driver_register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query 
    await query.answer() 
    await query.edit_message_text("📝 Please enter your **Full Name**:") 
    return D_NAME 

async def driver_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['driver_name'] = update.message.text 
    phone_keyboard = ReplyKeyboardMarkup( 
        [[KeyboardButton("📞 Share Phone Number", request_contact=True)]], 
        one_time_keyboard=True, resize_keyboard=True 
    ) 
    await update.message.reply_text("📞 Please enter or share your **Phone Contact Number**:", reply_markup=phone_keyboard) 
    return D_PHONE 

async def driver_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    contact = update.message.contact 
    context.user_data['driver_phone'] = contact.phone_number if contact else update.message.text 
    await update.message.reply_text("🚗 Please enter your **Vehicle Brand and Model**:", reply_markup=ReplyKeyboardRemove()) 
    return D_VEHICLE 

async def driver_vehicle_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['driver_vehicle'] = update.message.text 
    await update.message.reply_text("🔢 Please enter your **Car Plate Number**:") 
    return D_PLATE 

async def driver_plate_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    user = update.message.from_user 
    plate_number = update.message.text 
    data = context.user_data 
     
    async with AsyncSessionLocal() as session: 
        res = await session.execute(select(Driver).where(Driver.telegram_id == user.id)) 
        driver = res.scalar_one_or_none() 
        if not driver: 
            driver = Driver( 
                telegram_id=user.id,  
                name=data['driver_name'],  
                username=user.username,  
                wallet_balance=1.0,  
                is_approved=False,
                phone=data['driver_phone'], 
                car_model=data['driver_vehicle'],     
                license_plate=plate_number            
            ) 
            session.add(driver) 
        else: 
            driver.name = data['driver_name'] 
            driver.phone = data['driver_phone']
            driver.car_model = data['driver_vehicle']     
            driver.license_plate = plate_number            
        await session.commit() 
         
    await update.message.reply_text("✅ Registration details submitted! You received **1 Welcome Point** 🎉. Please wait for admin approval.", parse_mode="Markdown")
     
    if ADMIN_GROUP_ID: 
        try: 
            text = ( 
                f"👨‍✈️ **NEW DRIVER REGISTRATION (1 Pt Bonus)**\n\n"
                f"👤 Name: {data['driver_name']}\n" 
                f"📞 Phone: `{data['driver_phone']}`\n" 
                f"🚙 Vehicle: {data['driver_vehicle']}\n" 
                f"🔢 Plate Number: `{plate_number}`" 
            ) 
            keyboard = [[InlineKeyboardButton("✅ Approve Driver", callback_data=f"approve_driver_{user.id}")]] 
            await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)) 
        except Exception as e: 
            logger.error(f"Failed to send driver registration: {e}") 
             
    return ConversationHandler.END 

async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query 
    await query.answer() 
    async with AsyncSessionLocal() as session: 
        res = await session.execute(select(Driver).where(Driver.telegram_id == query.from_user.id)) 
        driver = res.scalar_one_or_none() 
        if not driver or not driver.is_approved: 
            await query.edit_message_text("❌ You are not an approved driver yet.") 
            return ConversationHandler.END 

    keyboard = [ 
        [InlineKeyboardButton("1 Point", callback_data="pkg_1")],
        [InlineKeyboardButton("10 Points", callback_data="pkg_10"), InlineKeyboardButton("50 Points", callback_data="pkg_50")], 
        [InlineKeyboardButton("100 Points", callback_data="pkg_100"), InlineKeyboardButton("1,000 Points", callback_data="pkg_1000")] 
    ] 
    await query.edit_message_text("💳 **Select Top-Up Package:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)) 
    return TOPUP_PKG 

async def topup_package_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query 
    await query.answer() 
    pkg = TOPUP_PACKAGES[query.data] 
    context.user_data['topup_points'] = pkg["points"] 
    context.user_data['topup_price'] = pkg["price"] 

    text = (
        f"💳 Send payment for **{pkg['points']} Points ({pkg['price']:,} MMK)**\n\n"
        f"📲 **Transfer to (KBZPay / AYAPay / WAVEPay):**\n"
        f"Phone: `09254417659`\n\n"
        f"📸 After transfer, please upload your screenshot here."
    )
    await query.edit_message_text(text, parse_mode="Markdown")
    return TOPUP_RECEIPT 

async def topup_receipt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    if not update.message.photo: 
        await update.message.reply_text("Please upload screenshot.") 
        return TOPUP_RECEIPT 
    file_id = update.message.photo[-1].file_id 
    user = update.message.from_user 
    points = context.user_data.get('topup_points', 0) 
    await update.message.reply_text("✅ Uploaded successfully! Pending approval.") 
    if ADMIN_GROUP_ID: 
        keyboard = [[InlineKeyboardButton(f"✅ Approve (+{points} Pts)", callback_data=f"tapp_{user.id}_{points}"), InlineKeyboardButton("❌ Reject", callback_data=f"trej_{user.id}")]] 
        await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=file_id, caption=f"Top-up request: {points} Pts", reply_markup=InlineKeyboardMarkup(keyboard)) 
    return ConversationHandler.END 

async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query 
    await query.answer() 
    data = query.data 
    if data.startswith("approve_driver_"): 
        d_id = int(data.split("_")[2]) 
        async with AsyncSessionLocal() as session: 
            driver = (await session.execute(select(Driver).where(Driver.telegram_id == d_id))).scalar_one_or_none() 
            if driver: 
                driver.is_approved = True 
                await session.commit() 
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ DRIVER APPROVED") 
        
        invite_link = None
        if DRIVER_GROUP_ID:
            try:
                invite = await context.bot.create_chat_invite_link(chat_id=DRIVER_GROUP_ID, member_limit=1)
                invite_link = invite.invite_link
            except Exception as e:
                logger.error(f"Failed to create invite link: {e}")

        if invite_link:
            await context.bot.send_message(
                chat_id=d_id, 
                text=f"🎉 Your driver account is approved!\n\nJoin the Driver Dispatch Group here: {invite_link}"
            )
        else:
            await context.bot.send_message(chat_id=d_id, text="🎉 Your driver account is approved!")
            
    elif data.startswith("tapp_"): 
        _, d_id_str, pts_str = data.split("_") 
        d_id, pts = int(d_id_str), float(pts_str) 
        async with AsyncSessionLocal() as session: 
            driver = (await session.execute(select(Driver).where(Driver.telegram_id == d_id))).scalar_one_or_none() 
            if driver: 
                driver.wallet_balance += pts 
                session.add(WalletTransaction(driver_telegram_id=d_id, amount=pts, type="TOP_UP")) 
                await session.commit() 
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ APPROVED") 
        await context.bot.send_message(chat_id=d_id, text=f"✅ Top-up Approved (+{pts:,.0f} Pts)!") 
        
    elif data.startswith("trej_"): 
        d_id = int(data.split("_")[1]) 
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ REJECTED") 
        await context.bot.send_message(chat_id=d_id, text="❌ Top-up rejected.") 

    # <== Updated 18Sep26: Admin cancel approval added below
    elif data.startswith("dcancelapp_"):
        parts = data.split("_")
        b_id = parts[1]
        d_id = int(parts[2])
        
        async with AsyncSessionLocal() as session:
            booking = (await session.execute(select(Booking).where(Booking.id == b_id))).scalar_one_or_none()
            if booking and booking.status not in ["CANCELLED", "TRIP_COMPLETED"]:
                driver = (await session.execute(select(Driver).where(Driver.telegram_id == d_id))).scalar_one_or_none()
                if driver:
                    points_refund = 1.0 if booking.vehicle in ["TAXI", "Kilo Car"] else float(booking.hours)
                    driver.wallet_balance += points_refund
                    session.add(WalletTransaction(driver_telegram_id=d_id, amount=points_refund, type="REFUND", booking_id=booking.id))
                
                booking.status = "AVAILABLE" 
                booking.driver_id = None
                booking.driver_name = None
                await session.commit()
                
                await context.bot.send_message(chat_id=d_id, text=f"✅ Admin approved your cancellation for Job #{b_id}. Points refunded.")
                await context.bot.send_message(chat_id=booking.customer_id, text=f"⚠️ Your driver had to cancel Job #{b_id}. We are finding a new driver.")
        
        await query.edit_message_text(f"{query.message.text}\n\n✅ Approved and Refunded. Job is Available again.")
    # <== Updated 18Sep26

    # <== Updated 18Sep26: Admin cancel rejection added below
    elif data.startswith("dcancelrej_"):
        parts = data.split("_")
        b_id = parts[1]
        d_id = int(parts[2])
        await context.bot.send_message(chat_id=d_id, text=f"❌ Admin rejected your cancellation for Job #{b_id}. Please complete the trip.")
        await query.edit_message_text(f"{query.message.text}\n\n❌ Rejected.")
    # <== Updated 18Sep26

# <== Updated 18Sep26: Driver cancel request function added below
async def driver_cancel_req(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    b_id = query.data.split("_")[1]
    driver_user = query.from_user

    if ADMIN_GROUP_ID:
        text = f"⚠️ **DRIVER CANCELLATION REQUEST**\n\nDriver: {driver_user.full_name}\nJob ID: `{b_id}`\n\nPlease approve or reject."
        kb = [
            [InlineKeyboardButton("✅ Approve & Refund", callback_data=f"dcancelapp_{b_id}_{driver_user.id}")],
            [InlineKeyboardButton("❌ Reject", callback_data=f"dcancelrej_{b_id}_{driver_user.id}")]
        ]
        await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        
        await query.edit_message_text(f"{query.message.text}\n\n⏳ Cancellation requested. Waiting for admin approval.")
# <== Updated 18Sep26

async def accept_job(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query 
    driver_user = query.from_user 
    b_id = query.data.split("_")[1] 
    await query.answer()  # <== Update Phase1_18Sep26

    async with AsyncSessionLocal() as session: 
        driver = (await session.execute(select(Driver).where(Driver.telegram_id == driver_user.id))).scalar_one_or_none() 
        if not driver or not driver.is_approved: 
            await query.answer("❌ You are not an approved driver!", show_alert=True) 
            return 

        booking = (await session.execute(
            select(Booking).where(Booking.id == b_id).with_for_update()
        )).scalar_one_or_none() 
        if not booking or booking.status != "AVAILABLE": 
            await query.answer("❌ Job no longer available!", show_alert=True) 
            return 

        # Phase 1 overlap check: compare actual time windows, not only identical start times.
        try:  # <== Update Phase1_18Sep26
            new_start, new_end = get_booking_window(  # <== Update Phase1_18Sep26
                booking.date_str, booking.time_str, booking.vehicle, booking.hours
            )  # <== Update Phase1_18Sep26
        except Exception as e:  # <== Update Phase1_18Sep26
            logger.error(f"Could not parse booking schedule {b_id}: {e}")  # <== Update Phase1_18Sep26
            await query.answer("❌ Invalid booking schedule. Please contact admin.", show_alert=True)  # <== Update Phase1_18Sep26
            return  # <== Update Phase1_18Sep26

        conflict_stmt = select(Booking).where(  # <== Update Phase1_18Sep26
            Booking.driver_id == driver.telegram_id,  # <== Update Phase1_18Sep26
            Booking.status.in_(["ASSIGNED", "ON_THE_WAY", "DRIVER_ARRIVED", "TRIP_STARTED"])  # <== Update Phase1_18Sep26
        )  # <== Update Phase1_18Sep26
        existing_jobs = (await session.execute(conflict_stmt)).scalars().all()  # <== Update Phase1_18Sep26

        has_conflict = False  # <== Update Phase1_18Sep26
        conflict_text = None  # <== Update Phase1_18Sep26
        for existing in existing_jobs:  # <== Update Phase1_18Sep26
            try:  # <== Update Phase1_18Sep26
                existing_start, existing_end = get_booking_window(  # <== Update Phase1_18Sep26
                    existing.date_str, existing.time_str, existing.vehicle, existing.hours
                )  # <== Update Phase1_18Sep26
            except Exception:  # <== Update Phase1_18Sep26
                continue  # <== Update Phase1_18Sep26

            buffered_existing_start = existing_start - timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  # <== Update Phase1_18Sep26
            buffered_existing_end = existing_end + timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  # <== Update Phase1_18Sep26
            buffered_new_start = new_start - timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  # <== Update Phase1_18Sep26
            buffered_new_end = new_end + timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  # <== Update Phase1_18Sep26

            if buffered_existing_start < buffered_new_end and buffered_new_start < buffered_existing_end:  # <== Update Phase1_18Sep26
                has_conflict = True  # <== Update Phase1_18Sep26
                conflict_text = (  # <== Update Phase1_18Sep26
                    f"❌ Schedule conflict!\n\n"
                    f"Existing job: {existing.time_str}"
                    f" → {format_end_time(existing_start, int((existing_end - existing_start).total_seconds() / 60))}\n"
                    f"New job: {booking.time_str}"
                    f" → {format_end_time(new_start, int((new_end - new_start).total_seconds() / 60))}\n\n"
                    f"A {DRIVER_SCHEDULE_BUFFER_MINUTES}-minute safety buffer is included."
                )  # <== Update Phase1_18Sep26
                break  # <== Update Phase1_18Sep26

        if has_conflict:  # <== Update Phase1_18Sep26
            await query.answer(conflict_text, show_alert=True)  # <== Update Phase1_18Sep26
            return  # <== Update Phase1_18Sep26

        required_points = 1.0 if booking.vehicle in ["TAXI", "Kilo Car"] else float(booking.hours) 
        if driver.wallet_balance < required_points: 
            await query.answer(f"❌ Insufficient points. Required: {required_points:,.0f}", show_alert=True) 
            return 

        driver.wallet_balance -= required_points 
        booking.status = "ASSIGNED" 
        booking.driver_id = driver.telegram_id 
        booking.driver_name = driver.name 
        session.add(WalletTransaction(driver_telegram_id=driver.telegram_id, amount=-required_points, type="COMMISSION", booking_id=booking.id)) 
        await session.commit() 

        customer_phone = getattr(booking, "customer_phone", None) or "N/A"
        driver_phone = getattr(driver, "phone", None) or "N/A"
        fare_display = f"{booking.fare_mmk:,.0f} MMK" if booking.fare_mmk > 0 else "Kilo Car Rate"

        await query.edit_message_text(
            text=f"🔒 **JOB #{b_id} ACCEPTED**\nDriver: {driver.name}",
            parse_mode="Markdown"
        )

        kb = [
            [InlineKeyboardButton("🏎️ On The Way", callback_data=f"ontheway_{b_id}")],
            [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] 
        ]
        await context.bot.send_message(
            chat_id=driver_user.id, 
            text=(
                f"📋 **ACCEPTED TRIP (#{b_id})**\n"
                f"Vehicle: {booking.vehicle}\n"
                f"📅 Pickup: {booking.date_str} {booking.time_str}\n"
                f"📍 Location:\n{booking.location}\n"
                f"📞 **Customer Phone:** `{customer_phone}`\n"
                f"💰 Fare: **{fare_display}**"
            ), 
            parse_mode="Markdown", 
            reply_markup=InlineKeyboardMarkup(kb)
        ) 

        await context.bot.send_message(
            chat_id=booking.customer_id,
            text=f"🚖 **DRIVER ASSIGNED!**\nName: {driver.name}\nPhone: `{driver_phone}`",
            parse_mode="Markdown"
        )

async def trip_lifecycle(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query 
    await query.answer() 
    action, b_id = query.data.split("_") 
     
    async with AsyncSessionLocal() as session: 
        booking = (await session.execute(select(Booking).where(Booking.id == b_id))).scalar_one_or_none() 
        if not booking: 
            return 
            
        if action == "ontheway":
            booking.status = "ON_THE_WAY"
            await session.commit()
            
            kb = [
                [InlineKeyboardButton("📍 Driver Arrived", callback_data=f"arrived_{b_id}")],
                [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] # <== Updated 18Sep26
            ]
            await query.edit_message_text(
                text=f"🏎️ **JOB #{b_id}**\nStatus: On The Way to Pickup",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            
            loc_keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share Live Location", request_location=True)]],
                one_time_keyboard=True, resize_keyboard=True
            )
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="📍 Please tap the button below to share your **Live Location** so the customer can track you:",
                reply_markup=loc_keyboard
            )
            
            await context.bot.send_message(
                chat_id=booking.customer_id, 
                text=f"🏎️ **Driver ({booking.driver_name}) is on the way to your pickup location!**"
            )

        elif action == "arrived": 
            booking.status = "DRIVER_ARRIVED" 
            await session.commit() 
            kb = [
                [InlineKeyboardButton("▶️ Start Trip", callback_data=f"starttrip_{b_id}")],
                [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] # <== Updated 18Sep26
            ]
            await query.edit_message_text(
                text=f"📍 **JOB #{b_id}**\nDriver Arrived", 
                reply_markup=InlineKeyboardMarkup(kb)
            ) 
            await context.bot.send_message(chat_id=booking.customer_id, text="📍 Driver has arrived at your location.") 

        elif action == "starttrip": 
            booking.status = "TRIP_STARTED" 
            await session.commit() 
            await query.edit_message_text(
                text=f"▶️ **JOB #{b_id}**\nTrip Started", 
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏁 End Trip", callback_data=f"endtrip_{b_id}")]])
            ) 
            await context.bot.send_message(chat_id=booking.customer_id, text="▶️ Your trip has started.") 

        elif action == "endtrip": 
            booking.status = "TRIP_COMPLETED" 
            await session.commit() 
            await query.edit_message_text(text=f"🏁 **JOB #{b_id}**\nCompleted") 
            await context.bot.send_message(chat_id=booking.customer_id, text="🏁 Trip completed. Thank you for riding with us!") 

async def driver_location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return
        
    driver_user_id = msg.from_user.id
    lat = msg.location.latitude
    lng = msg.location.longitude

    async with AsyncSessionLocal() as session:
        stmt = select(Booking).where(
            Booking.driver_id == driver_user_id,
            Booking.status.in_(["ON_THE_WAY", "TRIP_STARTED"])
        )
        res = await session.execute(stmt)
        booking = res.scalar_one_or_none()
        
        if not booking:
            return

        if hasattr(booking, 'driver_lat'):
            booking.driver_lat = lat
            booking.driver_lng = lng
            await session.commit()

        try:
            msg_id = getattr(booking, 'live_map_msg_id', None)
            if msg_id:
                await context.bot.edit_message_live_location(
                    chat_id=booking.customer_id,
                    message_id=msg_id,
                    latitude=lat,
                    longitude=lng
                )
            else:
                sent_msg = await context.bot.send_location(
                    chat_id=booking.customer_id,
                    latitude=lat,
                    longitude=lng,
                    live_period=1800
                )
                if hasattr(booking, 'live_map_msg_id'):
                    booking.live_map_msg_id = sent_msg.message_id
                    await session.commit()
        except Exception as e:
            logger.error(f"Error updating live location for booking {booking.id}: {e}")

@app.on_event("startup") 
async def startup_event(): 
    global telegram_app 
    await init_db() 
    telegram_app = Application.builder().token(BOT_TOKEN).build() 
     
    conv_handler = ConversationHandler( 
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start_booking_callback, pattern="^start_booking$")], 
        states={ 
            VEHICLE: [CallbackQueryHandler(vehicle_chosen)], 
            BOOKING_MODE: [CallbackQueryHandler(booking_mode_chosen, pattern="^mode_(instant|scheduled)$")],  # <== Update Phase1_18Sep26
            DATE: [CallbackQueryHandler(date_chosen)],
            TIME: [CallbackQueryHandler(time_chosen)],
            HOURS: [CallbackQueryHandler(hours_chosen)], 
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)], 
            DROP_LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, drop_location_received)],  
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, passengers_received)], 
            C_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, customer_phone_received)],
            CONFIRM_BOOKING: [  # <== Update Phase1_18Sep26
                CallbackQueryHandler(booking_confirmed, pattern="^booking_confirm_"),  # <== Update Phase1_18Sep26
                CallbackQueryHandler(booking_cancelled_before_confirm, pattern="^booking_cancel$")  # <== Update Phase1_18Sep26
            ]  # <== Update Phase1_18Sep26
        }, 
        fallbacks=[CommandHandler("start", start)] 
    ) 
 
    driver_conv = ConversationHandler( 
        entry_points=[CallbackQueryHandler(driver_register_start, pattern="^driver_register$")], 
        states={ 
            D_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_name_received)], 
            D_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, driver_phone_received)], 
            D_VEHICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_vehicle_received)], 
            D_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_plate_received)] 
        }, 
        fallbacks=[CommandHandler("start", start)] 
    ) 
 
    topup_conv = ConversationHandler( 
        entry_points=[CallbackQueryHandler(topup_start, pattern="^topup_start$")], 
        states={ 
            TOPUP_PKG: [CallbackQueryHandler(topup_package_chosen, pattern="^pkg_")], 
            TOPUP_RECEIPT: [MessageHandler(filters.PHOTO, topup_receipt_received)] 
        }, 
        fallbacks=[CommandHandler("start", start)] 
    ) 
     
    telegram_app.add_handler(conv_handler) 
    telegram_app.add_handler(driver_conv) 
    telegram_app.add_handler(topup_conv) 
    telegram_app.add_handler(CommandHandler("balance", check_balance_command)) 
    
    # <== Updated 18Sep26: Handlers updated below
    telegram_app.add_handler(CallbackQueryHandler(check_balance_callback, pattern="^driver_balance$")) 
    telegram_app.add_handler(CallbackQueryHandler(admin_actions, pattern="^(approve_|tapp_|trej_|dcancelapp_|dcancelrej_)")) 
    telegram_app.add_handler(CallbackQueryHandler(accept_job, pattern="^accept_")) 
    telegram_app.add_handler(CallbackQueryHandler(trip_lifecycle, pattern="^(ontheway_|arrived_|starttrip_|endtrip_)")) 
    telegram_app.add_handler(CallbackQueryHandler(customer_cancel, pattern="^ccancel_"))
    telegram_app.add_handler(CallbackQueryHandler(driver_cancel_req, pattern="^dcancelreq_"))
    # <== Updated 18Sep26
    
    telegram_app.add_handler(MessageHandler(filters.LOCATION, driver_location_handler))
 
    await telegram_app.initialize() 
    if RUN_MODE == "webhook": 
        await telegram_app.start() 
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram", secret_token=WEBHOOK_SECRET) 
    else: 
        import asyncio 
        asyncio.create_task(telegram_app.run_polling()) 
 
@app.post("/telegram") 
async def webhook_endpoint(request: Request): 
    if RUN_MODE == "webhook": 
        data = await request.json() 
        update = Update.de_json(data, telegram_app.bot) 
        await telegram_app.process_update(update) 
    return {"status": "ok"} 

@app.get("/") 
def home(): 
    return {"status": "Bot is active!"}
