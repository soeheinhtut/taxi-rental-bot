import os
import logging
import calendar
import math
import asyncio  
import json  
from urllib.request import Request as URLRequest, urlopen  
from datetime import datetime, timedelta  
from zoneinfo import ZoneInfo  
from fastapi import FastAPI, Request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler,
    PicklePersistence
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

YANGON_TZ = ZoneInfo("Asia/Yangon")  
SERVICE_START_MINUTES = 5 * 60  
SERVICE_END_MINUTES = 22 * 60  
TIME_STEP_MINUTES = 15  
INSTANT_DISPATCH_LEAD_MINUTES = 15  
DRIVER_SCHEDULE_BUFFER_MINUTES = 30  
KILO_DRIVER_BLOCK_MINUTES = 60  
KILO_BASE_FARE = 8500.0  
KILO_INCLUDED_KM = 5.0  
KILO_EXTRA_KM_RATE = 1100.0  
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")  
OSRM_TIMEOUT_SECONDS = float(os.getenv("OSRM_TIMEOUT_SECONDS", "8"))  

TOPUP_PACKAGES = {
    "pkg_1": {"points": 1, "price": 1 * MMK_PER_POINT},       
    "pkg_10": {"points": 10, "price": 10 * MMK_PER_POINT},
    "pkg_50": {"points": 50, "price": 50 * MMK_PER_POINT},
    "pkg_100": {"points": 100, "price": 100 * MMK_PER_POINT},
    "pkg_1000": {"points": 1000, "price": 1000 * MMK_PER_POINT},
}

VEHICLE, BOOKING_MODE, DATE, TIME, HOURS, LOCATION, DROP_LOCATION, PASSENGERS, C_PHONE, CONFIRM_BOOKING = range(10)  
D_NAME, D_PHONE, D_VEHICLE, D_PLATE = range(10, 14)  
TOPUP_PKG, TOPUP_RECEIPT = range(14, 16)  

app = FastAPI()
telegram_app = None

def calculate_distance(lat1, lon1, lat2, lon2):  
    R = 6371.0  
    dlat = math.radians(lat2 - lat1)  
    dlon = math.radians(lon2 - lon1)  
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2  
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))  
    return R * c  

def get_yangon_now():  
    return datetime.now(YANGON_TZ)  

def format_picker_time(hour24, minute):  
    return datetime.strptime(f"{hour24:02d}:{minute:02d}", "%H:%M").strftime("%I:%M %p")  

def normalize_picker_time(hour24, minute, min_total=None):  
    total_minutes = hour24 * 60 + minute  
    min_allowed = SERVICE_START_MINUTES if min_total is None else max(SERVICE_START_MINUTES, int(min_total))  
    min_allowed = ((min_allowed + TIME_STEP_MINUTES - 1) // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  
    total_minutes = max(min_allowed, min(SERVICE_END_MINUTES, total_minutes))  
    total_minutes = (total_minutes // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  
    total_minutes = max(min_allowed, min(SERVICE_END_MINUTES, total_minutes))  
    return total_minutes // 60, total_minutes % 60  

def next_quarter_hour(dt_obj, lead_minutes=0):  
    candidate = dt_obj + timedelta(minutes=lead_minutes)  
    minute_floor = (candidate.minute // TIME_STEP_MINUTES) * TIME_STEP_MINUTES  
    candidate = candidate.replace(minute=minute_floor, second=0, microsecond=0)  
    if candidate.minute < dt_obj.minute or (candidate == dt_obj and lead_minutes > 0):  
        candidate += timedelta(minutes=TIME_STEP_MINUTES)  
    if lead_minutes > 0 and candidate < dt_obj + timedelta(minutes=lead_minutes):  
        candidate += timedelta(minutes=TIME_STEP_MINUTES)  
    return candidate  

def get_default_time_for_date(selected_date):  
    today = get_yangon_now().date()  
    selected = datetime.strptime(selected_date, "%Y-%m-%d").date()  
    if selected > today:  
        return 5, 0, SERVICE_START_MINUTES  
    now = get_yangon_now()  
    minimum = next_quarter_hour(now, INSTANT_DISPATCH_LEAD_MINUTES)  
    minimum_total = minimum.hour * 60 + minimum.minute  
    minimum_total = max(SERVICE_START_MINUTES, min(SERVICE_END_MINUTES, minimum_total))  
    return normalize_picker_time(minimum.hour, minimum.minute, minimum_total)[0], normalize_picker_time(minimum.hour, minimum.minute, minimum_total)[1], minimum_total  

def get_time_picker_keyboard(hour24=5, minute=0, min_total=None):  
    hour24, minute = normalize_picker_time(hour24, minute, min_total)  
    current_time = format_picker_time(hour24, minute)  
    min_allowed = SERVICE_START_MINUTES if min_total is None else max(SERVICE_START_MINUTES, int(min_total))  

    current_total = hour24 * 60 + minute  
    prev_total = max(min_allowed, current_total - TIME_STEP_MINUTES)  
    next_total = min(SERVICE_END_MINUTES, current_total + TIME_STEP_MINUTES)  

    keyboard = [  
        [  
            InlineKeyboardButton("⬆️ +1 Hour", callback_data=f"time_hour_{min(SERVICE_END_MINUTES // 60, hour24 + 1)}"),  
            InlineKeyboardButton("⬆️ +15 Min", callback_data=f"time_total_{next_total}")  
        ],  
        [  
            InlineKeyboardButton(f"🕐 {current_time}", callback_data="time_noop"),  
            InlineKeyboardButton("✅ Select", callback_data="time_confirm")  
        ],  
        [  
            InlineKeyboardButton("⬇️ -1 Hour", callback_data=f"time_hour_{max(5, hour24 - 1)}"),  
            InlineKeyboardButton("⬇️ -15 Min", callback_data=f"time_total_{prev_total}")  
        ],  
        [  
            InlineKeyboardButton("ℹ️ 05:00 AM – 10:00 PM • 15-min steps", callback_data="time_noop")  
        ]  
    ]  
    return InlineKeyboardMarkup(keyboard)  

def get_calendar_keyboard(year, month):  
    keyboard = []  
    today = get_yangon_now().date()  
    keyboard.append([InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")])  
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])  

    month_calendar = calendar.monthcalendar(year, month)  
    for week in month_calendar:  
        row = []  
        for day in week:  
            if day == 0:  
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))  
            else:  
                day_date = datetime(year, month, day).date()  
                if day_date < today:  
                    row.append(InlineKeyboardButton("·", callback_data="date_past"))  
                else:  
                    row.append(InlineKeyboardButton(str(day), callback_data=f"date_{year}_{month}_{day}"))  
        keyboard.append(row)  

    current_month_start = datetime(today.year, today.month, 1).date()  
    shown_month_start = datetime(year, month, 1).date()  
    prev_month = month - 1 if month > 1 else 12  
    prev_year = year if month > 1 else year - 1  
    next_month = month + 1 if month < 12 else 1  
    next_year = year if month < 12 else year + 1  

    prev_button = InlineKeyboardButton("⏪", callback_data=f"cal_{prev_year}_{prev_month}") if shown_month_start > current_month_start else InlineKeyboardButton("⏪", callback_data="ignore")  

    keyboard.append([  
        prev_button,  
        InlineKeyboardButton("📅 Today", callback_data=f"date_{today.year}_{today.month}_{today.day}"),  
        InlineKeyboardButton("⏩", callback_data=f"cal_{next_year}_{next_month}")  
    ])  
    return InlineKeyboardMarkup(keyboard)  

def get_booking_mode_keyboard():  
    return InlineKeyboardMarkup([  
        [InlineKeyboardButton("⚡ Book Now (ASAP)", callback_data="mode_instant")],  
        [InlineKeyboardButton("📅 Schedule for Later", callback_data="mode_scheduled")]  
    ])  

def parse_booking_start(date_str, time_str):  
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p").replace(tzinfo=YANGON_TZ)  

def format_end_time(start_dt, minutes):  
    return (start_dt + timedelta(minutes=minutes)).strftime("%I:%M %p")  

def get_booking_window(date_str, time_str, vehicle, hours):  
    start_dt = parse_booking_start(date_str, time_str)  
    if vehicle == "Kilo Car":  
        stored_hours = max(1, int(hours or 0))  
        duration_minutes = max(KILO_DRIVER_BLOCK_MINUTES, stored_hours * 60)  
    else:  
        duration_minutes = max(1, int(hours)) * 60  
    return start_dt, start_dt + timedelta(minutes=duration_minutes)  

def calculate_kilo_fare(distance_km):  
    if distance_km <= KILO_INCLUDED_KM:  
        return KILO_BASE_FARE  
    extra_km = math.ceil(distance_km - KILO_INCLUDED_KM)  
    return KILO_BASE_FARE + (extra_km * KILO_EXTRA_KM_RATE)  

async def get_road_route_km_duration(pickup_lat, pickup_lng, drop_lat, drop_lng):  
    url = (  
        f"{OSRM_BASE_URL}/route/v1/driving/"  
        f"{pickup_lng},{pickup_lat};{drop_lng},{drop_lat}?overview=false"  
    )  

    def _request():  
        req = URLRequest(url, headers={"User-Agent": "MMDRIVE-Car-Rental-Bot/1.0"})  
        with urlopen(req, timeout=OSRM_TIMEOUT_SECONDS) as response:  
            return json.loads(response.read().decode("utf-8"))  

    try:  
        data = await asyncio.to_thread(_request)  
        if data.get("code") != "Ok" or not data.get("routes"):  
            raise ValueError(data.get("message", "No route returned"))  
        route = data["routes"][0]  
        return float(route["distance"]) / 1000.0, float(route["duration"]) / 60.0, "Road distance (OSRM)"  
    except Exception as e:  
        straight_km = calculate_distance(pickup_lat, pickup_lng, drop_lat, drop_lng)  
        logger.warning(f"OSRM route lookup failed, using Haversine fallback: {e}")  
        return straight_km, None, "Estimated straight-line distance (routing unavailable)"  


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
        reply_markup=get_booking_mode_keyboard(),  
        parse_mode="Markdown"
    )
    return BOOKING_MODE  


async def booking_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  
    query = update.callback_query  
    await query.answer()  
    mode = query.data  
    today = get_yangon_now().date()  

    if mode == "mode_instant":  
        now = get_yangon_now()  
        if now.hour * 60 + now.minute >= SERVICE_END_MINUTES:  
            await query.edit_message_text(
                "⏰ **Instant booking is currently closed.**\n\n"
                "Service hours: 05:00 AM – 10:00 PM.\n"  
                "Please choose **Schedule for Later**.",  
                reply_markup=get_booking_mode_keyboard(),  
                parse_mode="Markdown"
            )
            return BOOKING_MODE  

        pickup_dt = next_quarter_hour(now, INSTANT_DISPATCH_LEAD_MINUTES)  
        if pickup_dt.hour * 60 + pickup_dt.minute > SERVICE_END_MINUTES:  
            await query.edit_message_text(
                "⏰ **Instant booking is unavailable for the rest of today.**\n\n"
                "Please choose **Schedule for Later**.",  
                reply_markup=get_booking_mode_keyboard(),  
                parse_mode="Markdown"
            )
            return BOOKING_MODE  

        context.user_data["booking_mode"] = "INSTANT"  
        context.user_data["date"] = pickup_dt.strftime("%Y-%m-%d")  
        context.user_data["time"] = pickup_dt.strftime("%I:%M %p")  
        context.user_data["time_hour"] = pickup_dt.hour  
        context.user_data["time_minute"] = pickup_dt.minute  
        context.user_data["time_min_total"] = SERVICE_START_MINUTES  

        return await proceed_after_time_selection(query, context, instant=True)  

    if mode == "mode_scheduled":  
        context.user_data["booking_mode"] = "SCHEDULED"  
        await query.edit_message_text(
            f"🚘 Vehicle: **{context.user_data.get('vehicle', 'Sedan')}**\n\n📅 Select Pickup Date:",
            reply_markup=get_calendar_keyboard(today.year, today.month),  
            parse_mode="Markdown"
        )
        return DATE  

    await query.edit_message_text("❌ Invalid booking type. Please try again.")  
    return BOOKING_MODE  


async def proceed_after_time_selection(query, context, instant=False):  
    selected_time = context.user_data["time"]  
    vehicle = context.user_data.get("vehicle", "Sedan")  

    if vehicle in ["TAXI", "Kilo Car"]:  
        location_keyboard = ReplyKeyboardMarkup(  
            [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
            one_time_keyboard=True, resize_keyboard=True
        )
        mode_label = "⚡ Book Now (ASAP)" if instant else "📅 Scheduled"  
        pickup_prompt = (  
            "📍 Please share your exact **GPS Pickup Location**."  
            if vehicle == "Kilo Car"  
            else "📍 Please share your exact GPS Pickup Location or type your address:"  
        )  
        await query.edit_message_text(
            f"🕐 Pickup Time: **{selected_time}**\n"
            f"📌 Booking Type: **{mode_label}**\n\n"
            f"{pickup_prompt}",
            parse_mode="Markdown"
        )
        await query.message.reply_text("Click button to send GPS location:", reply_markup=location_keyboard)
        return LOCATION

    packages = PACKAGE_RATES.get(vehicle, {3: 75000, 5: 100000, 8: 160000})  
    keyboard = [
        [InlineKeyboardButton(f"3 Hours ({packages[3]:,.0f} MMK)", callback_data="3")],
        [InlineKeyboardButton(f"5 Hours ({packages[5]:,.0f} MMK)", callback_data="5")],
        [InlineKeyboardButton(f"8 Hours ({packages[8]:,.0f} MMK)", callback_data="8")]
    ]

    mode_label = "⚡ Book Now (ASAP)" if instant else "📅 Scheduled"  
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

    if data == "ignore":  
        return DATE  

    if data == "date_past":  
        await query.answer("❌ Past dates are not available.", show_alert=True)  
        return DATE  

    if data.startswith("cal_"):
        _, year, month = data.split("_")
        reply_markup = get_calendar_keyboard(int(year), int(month))
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return DATE

    if data.startswith("date_"):
        _, year, month, day = data.split("_")
        selected_date = f"{year}-{int(month):02d}-{int(day):02d}"  
        selected_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()  

        if selected_date_obj < get_yangon_now().date():  
            await query.answer("❌ Past dates are not available.", show_alert=True)  
            return DATE  

        context.user_data["date"] = selected_date  
        hour24, minute, min_total = get_default_time_for_date(selected_date)  
        context.user_data["time_hour"] = hour24  
        context.user_data["time_minute"] = minute  
        context.user_data["time_min_total"] = min_total  

        reply_markup = get_time_picker_keyboard(hour24, minute, min_total)  
        await query.edit_message_text(
            f"📅 Date: **{selected_date}**\n\n"
            f"🕐 **Select Pickup Time**\n\n"
            f"Use the scroll buttons to choose a time in 15-minute steps:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return TIME


async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  
    query = update.callback_query
    data = query.data

    hour24 = int(context.user_data.get("time_hour", 5))  
    minute = int(context.user_data.get("time_minute", 0))  
    min_total = int(context.user_data.get("time_min_total", SERVICE_START_MINUTES))  

    if data == "time_noop":  
        await query.answer()  
        return TIME  

    if data.startswith("time_hour_"):  
        await query.answer()  
        hour24 = int(data.split("_")[2])  
        hour24, minute = normalize_picker_time(hour24, minute, min_total)  
        context.user_data["time_hour"] = hour24  
        context.user_data["time_minute"] = minute  
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute, min_total))
        return TIME

    if data.startswith("time_total_"):  
        await query.answer()  
        total_minutes = int(data.split("_")[2])  
        hour24 = total_minutes // 60  
        minute = total_minutes % 60  
        hour24, minute = normalize_picker_time(hour24, minute, min_total)  
        context.user_data["time_hour"] = hour24  
        context.user_data["time_minute"] = minute  
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute, min_total))
        return TIME

    if data == "time_confirm":  
        selected_total = hour24 * 60 + minute  
        if selected_total < min_total or selected_total > SERVICE_END_MINUTES:  
            await query.answer("❌ Please choose a valid service time.", show_alert=True)  
            return TIME  

        selected_time = format_picker_time(hour24, minute)
        context.user_data["time"] = selected_time
        await query.answer("✅ Time selected")  
        return await proceed_after_time_selection(query, context, instant=False)  

    await query.answer("❌ Invalid time option.", show_alert=True)  
    return TIME  


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

    start_dt = parse_booking_start(context.user_data["date"], context.user_data["time"])  
    end_dt = start_dt + timedelta(hours=hours)  
    context.user_data["end_time"] = end_dt.strftime("%I:%M %p")  

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
        ):  
            await update.message.reply_text(  
                "❌ Kilo Car requires GPS pickup location. Please tap the 📎 / Location button and send your current pickup point."  
            )  
            return LOCATION  

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
    data["preview_customer_id"] = update.message.from_user.id  

    booking_id = f"RNT-{get_yangon_now().strftime('%Y%m%d')}-{int(get_yangon_now().timestamp()) % 10000}"  
    vehicle = data["vehicle"]

    if vehicle in ["TAXI", "Kilo Car"]:
        p_lat = data.get("pickup_lat")
        p_lng = data.get("pickup_lng")
        d_lat = data.get("drop_lat")
        d_lng = data.get("drop_lng")

        if p_lat is None or p_lng is None or d_lat is None or d_lng is None:  
            await update.message.reply_text(
                "❌ Pickup and drop-off GPS coordinates are required for Kilo Car."
            )
            return DROP_LOCATION

        direction_link = f"https://www.google.com/maps/dir/?api=1&origin={p_lat},{p_lng}&destination={d_lat},{d_lng}"

        road_km, route_minutes, distance_source = await get_road_route_km_duration(  
            p_lat, p_lng, d_lat, d_lng
        )  
        calculated_fare = calculate_kilo_fare(road_km)  

        final_location = (
            f"**Pickup:** {data['location']}\n"
            f"**Drop-off:** {data.get('drop_location', 'N/A')}\n"
            f"**🗺️ Route Map:** {direction_link}"
        )

        hours_label = "Point-to-Point (Kilo Car)"
        points_required = 1.0
        hours_db_value = max(1, math.ceil(route_minutes / 60.0)) if route_minutes is not None else 1  
        fare_display = f"{calculated_fare:,.0f} MMK ({road_km:.1f} km)"  
        fare_db_value = calculated_fare

        data["route_distance_km"] = road_km  
        data["route_duration_minutes"] = route_minutes  
        data["distance_source"] = distance_source  
        data["fare"] = calculated_fare  

        if route_minutes is not None:  
            start_dt = parse_booking_start(data["date"], data["time"])  
            eta_dt = start_dt + timedelta(minutes=round(route_minutes))  
            data["eta_time"] = eta_dt.strftime("%I:%M %p")  
        else:
            data["eta_time"] = None  

    else:
        final_location = data["location"]
        hours_label = f"{data['hours']} Hours"
        points_required = float(data["hours"])
        fare_display = f"{data['fare']:,.0f} MMK"
        fare_db_value = float(data["fare"])
        hours_db_value = int(data["hours"])

    start_dt = parse_booking_start(data["date"], data["time"])  
    booking_mode_label = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  

    if vehicle == "Kilo Car":
        end_label = f"Estimated Arrival: **{data['eta_time']}**" if data.get("eta_time") else "Arrival: **Routing ETA unavailable**"  
    else:
        end_label = f"Scheduled End: **{data.get('end_time', format_end_time(start_dt, hours_db_value * 60))}**"  

    summary = (  
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
    )  

    if vehicle == "Kilo Car":  
        summary += (  
            f"\n📏 Road Distance: **{data.get('route_distance_km', 0):.1f} km**\n"
            f"🛣️ Distance Basis: {data.get('distance_source', 'N/A')}\n"
        )  
        if data.get("route_duration_minutes") is not None:  
            summary += f"⏱ Estimated Drive: **{round(data['route_duration_minutes'])} min**\n"  

    summary += f"\n💰 **Total Fare: {fare_display}**\n\n"  
    if vehicle != "Kilo Car":
        summary += "ℹ️ Please confirm that the pickup time, package, and scheduled end time are correct.\n\n"  

    confirm_kb = [  
        [InlineKeyboardButton("✅ Confirm Booking", callback_data=f"booking_confirm_{booking_id}")],  
        [InlineKeyboardButton("❌ Cancel", callback_data="booking_cancel")],  
    ]  

    data["pending_booking_id"] = booking_id  
    data["pending_final_location"] = final_location  
    data["pending_hours_label"] = hours_label  
    data["pending_points_required"] = points_required  
    data["pending_fare_display"] = fare_display  
    data["pending_fare_db_value"] = fare_db_value  
    data["pending_hours_db_value"] = hours_db_value  

    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(confirm_kb),
        disable_web_page_preview=True
    )
    return CONFIRM_BOOKING  


async def booking_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  
    query = update.callback_query  
    await query.answer("Processing booking…")  

    if not query.data.startswith("booking_confirm_"):  
        return CONFIRM_BOOKING  

    data = context.user_data  
    booking_id = query.data.replace("booking_confirm_", "", 1)  

    if data.get("pending_booking_id") != booking_id:  
        await query.edit_message_text("❌ Booking session expired. Please start a new booking.")  
        return ConversationHandler.END  

    vehicle = data["vehicle"]  
    hours_db_value = int(data.get("pending_hours_db_value", 0))  
    fare_db_value = float(data.get("pending_fare_db_value", 0))  
    hours_label = data["pending_hours_label"]  
    points_required = float(data["pending_points_required"])  
    final_location = data["pending_final_location"]  
    fare_display = data["pending_fare_display"]  
    phone = data["customer_phone"]  

    start_dt = parse_booking_start(data["date"], data["time"])  
    if start_dt < get_yangon_now() - timedelta(minutes=1):  
        await query.edit_message_text(  
            "⚠️ This pickup time has already passed. Please start a new booking and choose a future time.",  
            parse_mode="Markdown"  
        )  
        return ConversationHandler.END  

    schedule_start_dt, schedule_end_dt = get_booking_window(  
        data["date"], data["time"], vehicle, hours_db_value
    )  

    booking = Booking(
        id=booking_id,
        customer_id=query.from_user.id,
        vehicle=vehicle,
        date_str=data["date"],
        time_str=data["time"],
        hours=hours_db_value,
        booking_mode=data.get("booking_mode", "SCHEDULED"),  
        start_at=schedule_start_dt,  
        end_at=schedule_end_dt,  
        location=final_location,
        passengers=int(data["passengers"]),
        fare_mmk=fare_db_value,
        status="AVAILABLE",
        payment_method="DIRECT",
        payment_receipt_file_id=None,
        driver_id=None,
        driver_name=None,
        customer_phone=phone,
        pickup_lat=data.get("pickup_lat"),  
        pickup_lng=data.get("pickup_lng"),  
        drop_lat=data.get("drop_lat"),  
        drop_lng=data.get("drop_lng"),  
        route_distance_km=data.get("route_distance_km"),  
        route_duration_minutes=data.get("route_duration_minutes"),  
        distance_source=data.get("distance_source"),  
    )

    try:
        async with AsyncSessionLocal() as session:
            session.add(booking)
            await session.commit()
    except Exception as e:
        logger.error(f"Booking creation failed: {e}")  
        await query.edit_message_text("❌ We could not create the booking. Please try again.")  
        return ConversationHandler.END  

    mode_label = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  
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

    cancel_kb = [[InlineKeyboardButton("❌ Cancel Booking", callback_data=f"ccancel_{booking_id}")]]  
    await query.edit_message_text(
        confirmed_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(cancel_kb),
        disable_web_page_preview=True
    )

    if DRIVER_GROUP_ID:
        try:
            booking_type_line = "⚡ Book Now (ASAP)" if data.get("booking_mode") == "INSTANT" else "📅 Scheduled"  
            if vehicle == "Kilo Car":  
                timing_line = f"🛣️ Road Distance: {data.get('route_distance_km', 0):.1f} km"  
                if data.get("route_duration_minutes") is not None:  
                    timing_line += f" | ETA {data.get('eta_time', 'N/A')}"  
            else:  
                timing_line = f"🏁 End: {data.get('end_time', 'N/A')}"  

            driver_text = (  
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

    context.user_data.clear()  
    return ConversationHandler.END  


async def booking_cancelled_before_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  
    query = update.callback_query  
    await query.answer("Booking cancelled")  
    await query.edit_message_text("🚫 **Booking cancelled.**\n\nUse /start to make a new booking.", parse_mode="Markdown")  
    context.user_data.clear()  
    return ConversationHandler.END  


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
        await query.edit_message_text(f"{query.message.text}\n\n🚫 **BOOKING CANCELLED**", parse_mode="Markdown")


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

    elif data.startswith("dcancelrej_"):
        parts = data.split("_")
        b_id = parts[1]
        d_id = int(parts[2])
        await context.bot.send_message(chat_id=d_id, text=f"❌ Admin rejected your cancellation for Job #{b_id}. Please complete the trip.")
        await query.edit_message_text(f"{query.message.text}\n\n❌ Rejected.")


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


async def accept_job(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    query = update.callback_query 
    driver_user = query.from_user 
    b_id = query.data.split("_")[1] 

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

        try:  
            new_start, new_end = get_booking_window(  
                booking.date_str, booking.time_str, booking.vehicle, booking.hours
            )  
        except Exception as e:  
            logger.error(f"Could not parse booking schedule {b_id}: {e}")  
            await query.answer("❌ Invalid booking schedule. Please contact admin.", show_alert=True)  
            return  

        conflict_stmt = select(Booking).where(  
            Booking.driver_id == driver.telegram_id,  
            Booking.status.in_(["ASSIGNED", "ON_THE_WAY", "DRIVER_ARRIVED", "TRIP_STARTED"])  
        )  
        existing_jobs = (await session.execute(conflict_stmt)).scalars().all()  

        has_conflict = False  
        conflict_text = None  
        for existing in existing_jobs:  
            try:  
                existing_start, existing_end = get_booking_window(  
                    existing.date_str, existing.time_str, existing.vehicle, existing.hours
                )  
            except Exception:  
                continue  

            buffered_existing_start = existing_start - timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  
            buffered_existing_end = existing_end + timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  
            buffered_new_start = new_start - timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  
            buffered_new_end = new_end + timedelta(minutes=DRIVER_SCHEDULE_BUFFER_MINUTES)  

            if buffered_existing_start < buffered_new_end and buffered_new_start < buffered_existing_end:  
                has_conflict = True  
                conflict_text = (  
                    f"❌ Schedule conflict!\n\n"
                    f"Existing job: {existing.time_str}"
                    f" → {format_end_time(existing_start, int((existing_end - existing_start).total_seconds() / 60))}\n"
                    f"New job: {booking.time_str}"
                    f" → {format_end_time(new_start, int((new_end - new_start).total_seconds() / 60))}\n\n"
                    f"A {DRIVER_SCHEDULE_BUFFER_MINUTES}-minute safety buffer is included."
                )  
                break  

        if has_conflict:  
            await query.answer(conflict_text, show_alert=True)  
            return  

        required_points = 1.0 if booking.vehicle in ["TAXI", "Kilo Car"] else float(booking.hours) 
        if driver.wallet_balance < required_points: 
            await query.answer(f"❌ Insufficient points. Required: {required_points:,.0f}", show_alert=True) 
            return 

        await query.answer("✅ Job Accepted!")

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
                [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] 
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
                [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] 
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
    
    my_persistence = PicklePersistence(filepath="bot_memory.pickle")
    telegram_app = Application.builder().token(BOT_TOKEN).persistence(persistence=my_persistence).build() 
     
    conv_handler = ConversationHandler( 
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start_booking_callback, pattern="^start_booking$")], 
        states={ 
            VEHICLE: [CallbackQueryHandler(vehicle_chosen)], 
            BOOKING_MODE: [CallbackQueryHandler(booking_mode_chosen, pattern="^mode_(instant|scheduled)$")], 
            DATE: [CallbackQueryHandler(date_chosen)],
            TIME: [CallbackQueryHandler(time_chosen)],
            HOURS: [CallbackQueryHandler(hours_chosen)], 
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)], 
            DROP_LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, drop_location_received)],  
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, passengers_received)], 
            C_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, customer_phone_received)],
            CONFIRM_BOOKING: [ 
                CallbackQueryHandler(booking_confirmed, pattern="^booking_confirm_"), 
                CallbackQueryHandler(booking_cancelled_before_confirm, pattern="^booking_cancel$") 
            ] 
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
    
    telegram_app.add_handler(CallbackQueryHandler(check_balance_callback, pattern="^driver_balance$")) 
    telegram_app.add_handler(CallbackQueryHandler(admin_actions, pattern="^(approve_|tapp_|trej_|dcancelapp_|dcancelrej_)")) 
    telegram_app.add_handler(CallbackQueryHandler(accept_job, pattern="^accept_")) 
    telegram_app.add_handler(CallbackQueryHandler(trip_lifecycle, pattern="^(ontheway_|arrived_|starttrip_|endtrip_)")) 
    telegram_app.add_handler(CallbackQueryHandler(customer_cancel, pattern="^ccancel_"))
    telegram_app.add_handler(CallbackQueryHandler(driver_cancel_req, pattern="^dcancelreq_"))
    
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
