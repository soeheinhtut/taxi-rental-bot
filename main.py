import os
import logging
import calendar
import math
from datetime import datetime
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

TOPUP_PACKAGES = {
    "pkg_1": {"points": 1, "price": 1 * MMK_PER_POINT},       
    "pkg_10": {"points": 10, "price": 10 * MMK_PER_POINT},
    "pkg_50": {"points": 50, "price": 50 * MMK_PER_POINT},
    "pkg_100": {"points": 100, "price": 100 * MMK_PER_POINT},
    "pkg_1000": {"points": 1000, "price": 1000 * MMK_PER_POINT},
}

VEHICLE, DATE, TIME, HOURS, LOCATION, DROP_LOCATION, PASSENGERS, C_PHONE = range(8)
D_NAME, D_PHONE, D_VEHICLE, D_PLATE = range(8, 12)
TOPUP_PKG, TOPUP_RECEIPT = range(12, 14)

app = FastAPI()
telegram_app = None

def calculate_distance(lat1, lon1, lat2, lon2):  
    R = 6371.0  
    dlat = math.radians(lat2 - lat1)  
    dlon = math.radians(lon2 - lon1)  
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2  
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))  
    return R * c  

def get_calendar_keyboard(year, month):
    keyboard = []
    keyboard.append([InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="ignore")])
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])
    
    month_calendar = calendar.monthcalendar(year, month)
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"date_{year}_{month}_{day}"))
        keyboard.append(row)
        
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    
    keyboard.append([
        InlineKeyboardButton("⏪", callback_data=f"cal_{prev_year}_{prev_month}"),
        InlineKeyboardButton("⏩", callback_data=f"cal_{next_year}_{next_month}")
    ])
    return InlineKeyboardMarkup(keyboard)

def format_picker_time(hour24, minute):  # <== Updated 18Sep26
    return datetime.strptime(f"{hour24:02d}:{minute:02d}", "%H:%M").strftime("%I:%M %p")  # <== Updated 18Sep26


def normalize_picker_time(hour24, minute):  # <== Updated 18Sep26
    total_minutes = hour24 * 60 + minute  # <== Updated 18Sep26
    total_minutes = max(7 * 60, min(22 * 60, total_minutes))  # <== Updated 18Sep26
    total_minutes = (total_minutes // 15) * 15  # <== Updated 18Sep26
    return total_minutes // 60, total_minutes % 60  # <== Updated 18Sep26


def get_time_picker_keyboard(hour24=7, minute=0):  # <== Updated 18Sep26
    hour24, minute = normalize_picker_time(hour24, minute)  # <== Updated 18Sep26
    current_time = format_picker_time(hour24, minute)  # <== Updated 18Sep26

    prev_hour = hour24 - 1 if hour24 > 7 else 7  # <== Updated 18Sep26
    next_hour = hour24 + 1 if hour24 < 22 else 22  # <== Updated 18Sep26

    prev_min_hour, prev_minute = normalize_picker_time(hour24, minute - 15)  # <== Updated 18Sep26
    next_min_hour, next_minute = normalize_picker_time(hour24, minute + 15)  # <== Updated 18Sep26

    hour_up_label = "⬆️ Hour"  # <== Updated 18Sep26
    hour_down_label = "⬇️ Hour"  # <== Updated 18Sep26
    minute_up_label = "⬆️ 15 Min"  # <== Updated 18Sep26
    minute_down_label = "⬇️ 15 Min"  # <== Updated 18Sep26

    keyboard = [  # <== Updated 18Sep26
        [  # <== Updated 18Sep26
            InlineKeyboardButton(hour_up_label, callback_data=f"time_hour_{next_hour}"),  # <== Updated 18Sep26
            InlineKeyboardButton(minute_up_label, callback_data=f"time_min_{next_min_hour}_{next_minute}")  # <== Updated 18Sep26
        ],  # <== Updated 18Sep26
        [  # <== Updated 18Sep26
            InlineKeyboardButton(f"🕐 {current_time}", callback_data="time_noop"),  # <== Updated 18Sep26
            InlineKeyboardButton("✅ Select", callback_data="time_confirm")  # <== Updated 18Sep26
        ],  # <== Updated 18Sep26
        [  # <== Updated 18Sep26
            InlineKeyboardButton(hour_down_label, callback_data=f"time_hour_{prev_hour}"),  # <== Updated 18Sep26
            InlineKeyboardButton(minute_down_label, callback_data=f"time_min_{prev_min_hour}_{prev_minute}")  # <== Updated 18Sep26
        ],  # <== Updated 18Sep26
        [  # <== Updated 18Sep26
            InlineKeyboardButton("ℹ️ 07:00 AM – 10:00 PM • 15-minute steps", callback_data="time_noop")  # <== Updated 18Sep26
        ]  # <== Updated 18Sep26
    ]  # <== Updated 18Sep26
    return InlineKeyboardMarkup(keyboard)  # <== Updated 18Sep26

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
    context.user_data['vehicle'] = query.data
    
    now = datetime.now()
    reply_markup = get_calendar_keyboard(now.year, now.month)
    
    await query.edit_message_text(
        f"🚘 Vehicle: **{query.data}**\n\n📅 Select Date:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return DATE

async def date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ignore":
        return DATE

    if data.startswith("cal_"):
        _, year, month = data.split("_")
        reply_markup = get_calendar_keyboard(int(year), int(month))
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return DATE

    if data.startswith("date_"):
        _, year, month, day = data.split("_")
        selected_date = f"{year}-{int(month):02d}-{int(day):02d}"
        context.user_data['date'] = selected_date

        context.user_data['time_hour'] = 7  # <== Updated 18Sep26
        context.user_data['time_minute'] = 0  # <== Updated 18Sep26
        reply_markup = get_time_picker_keyboard(7, 0)  # <== Updated 18Sep26
        await query.edit_message_text(
            f"📅 Date: **{selected_date}**\n\n🕐 **Select Pickup Time**\n\nUse the ⬆️ / ⬇️ buttons to scroll the hour and 15-minute steps:",  # <== Updated 18Sep26
            reply_markup=reply_markup,  # <== Updated 18Sep26
            parse_mode="Markdown"  # <== Updated 18Sep26
        )
        return TIME

async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # <== Updated 18Sep26
    query = update.callback_query  # <== Updated 18Sep26
    data = query.data  # <== Updated 18Sep26

    hour24 = int(context.user_data.get('time_hour', 7))  # <== Updated 18Sep26
    minute = int(context.user_data.get('time_minute', 0))  # <== Updated 18Sep26

    if data == "time_noop":  # <== Updated 18Sep26
        await query.answer()  # <== Updated 18Sep26
        return TIME  # <== Updated 18Sep26

    if data.startswith("time_hour_"):  # <== Updated 18Sep26
        await query.answer()  # <== Updated 18Sep26
        hour24 = int(data.split("_")[2])  # <== Updated 18Sep26
        hour24, minute = normalize_picker_time(hour24, minute)  # <== Updated 18Sep26
        context.user_data['time_hour'] = hour24  # <== Updated 18Sep26
        context.user_data['time_minute'] = minute  # <== Updated 18Sep26
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute))  # <== Updated 18Sep26
        return TIME  # <== Updated 18Sep26

    if data.startswith("time_min_"):  # <== Updated 18Sep26
        await query.answer()  # <== Updated 18Sep26
        _, _, new_hour, new_minute = data.split("_")  # <== Updated 18Sep26
        hour24 = int(new_hour)  # <== Updated 18Sep26
        minute = int(new_minute)  # <== Updated 18Sep26
        hour24, minute = normalize_picker_time(hour24, minute)  # <== Updated 18Sep26
        context.user_data['time_hour'] = hour24  # <== Updated 18Sep26
        context.user_data['time_minute'] = minute  # <== Updated 18Sep26
        await query.edit_message_reply_markup(reply_markup=get_time_picker_keyboard(hour24, minute))  # <== Updated 18Sep26
        return TIME  # <== Updated 18Sep26

    if data == "time_confirm":  # <== Updated 18Sep26
        await query.answer("✅ Time selected")  # <== Updated 18Sep26
        selected_time = format_picker_time(hour24, minute)  # <== Updated 18Sep26
        context.user_data['time'] = selected_time  # <== Updated 18Sep26
        vehicle = context.user_data.get('vehicle', 'Sedan')  # <== Updated 18Sep26

        if vehicle in ["TAXI", "Kilo Car"]:  # <== Updated 18Sep26
            location_keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
                one_time_keyboard=True, resize_keyboard=True
            )
            await query.edit_message_text(
                f"🕐 Time: **{selected_time}**\n\n📍 Please share your exact GPS Pickup Location or type your address:",  # <== Updated 18Sep26
                parse_mode="Markdown"
            )
            await query.message.reply_text("Click button to send GPS location:", reply_markup=location_keyboard)
            return LOCATION

        packages = PACKAGE_RATES.get(vehicle, {3: 75000, 5: 100000, 8: 160000})  # <== Updated 18Sep26
        keyboard = [
            [InlineKeyboardButton(f"3 Hours ({packages[3]:,.0f} MMK)", callback_data="3")],
            [InlineKeyboardButton(f"5 Hours ({packages[5]:,.0f} MMK)", callback_data="5")],
            [InlineKeyboardButton(f"8 Hours ({packages[8]:,.0f} MMK)", callback_data="8")]
        ]

        await query.edit_message_text(
            f"🕐 Time: **{selected_time}**\n\n⏱ Select Rental Package for **{vehicle}**:\n*(Note: Available Within Yangon City)*",  # <== Updated 18Sep26
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return HOURS

    await query.answer()  # <== Updated 18Sep26
    return TIME  # <== Updated 18Sep26

async def hours_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    hours = int(query.data)
    vehicle = context.user_data['vehicle']
    
    total_fare = PACKAGE_RATES[vehicle][hours] 
    
    context.user_data['hours'] = hours
    context.user_data['fare'] = total_fare
    
    location_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
        one_time_keyboard=True, resize_keyboard=True
    )
    
    await query.edit_message_text(
        f"⏱ **Package Selected:** {hours} Hours\n💰 **Total Fare:** {total_fare:,.0f} MMK\n\n"
        f"📍 Please share your exact GPS pickup location or type your address:",
        parse_mode="Markdown"
    )
    await query.message.reply_text("Click button to send GPS location:", reply_markup=location_keyboard)
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if loc:
        context.user_data['location'] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}" 
        context.user_data['pickup_lat'] = loc.latitude  
        context.user_data['pickup_lng'] = loc.longitude  
    else: 
        context.user_data['location'] = update.message.text 
         
    if context.user_data.get('vehicle') in ["TAXI", "Kilo Car"]:  
        await update.message.reply_text( 
            "📍 Please click the 📎 (paperclip) icon, choose **Location**, select your **Drop-off point** on the map, and send it.", 
            reply_markup=ReplyKeyboardRemove() 
        ) 
        return DROP_LOCATION

    await update.message.reply_text("👥 How many passengers will be riding?", reply_markup=ReplyKeyboardRemove()) 
    return PASSENGERS 

async def drop_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location  
    if loc:  
        context.user_data['drop_location'] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"  
        context.user_data['drop_lat'] = loc.latitude  
        context.user_data['drop_lng'] = loc.longitude  
    else:  
        await update.message.reply_text("❌ Text input is disabled. Please use 📎 (paperclip) -> Location to choose on the map.") 
        return DROP_LOCATION 
        
    await update.message.reply_text("👥 How many passengers will be riding?", reply_markup=ReplyKeyboardRemove())
    return PASSENGERS

async def passengers_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    context.user_data['passengers'] = update.message.text 
     
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
         
    context.user_data['customer_phone'] = phone 
    data = context.user_data 
    booking_id = f"RNT-{datetime.now().strftime('%Y%m%d')}-{int(datetime.now().timestamp()) % 10000}" 
     
    vehicle = data['vehicle']
    if vehicle in ['TAXI', 'Kilo Car']:
        p_lat = data.get('pickup_lat')
        p_lng = data.get('pickup_lng')
        d_lat = data.get('drop_lat')
        d_lng = data.get('drop_lng')
        
        direction_link = "N/A" 
        if p_lat and p_lng and d_lat and d_lng: 
            direction_link = f"https://www.google.com/maps/dir/?api=1&origin={p_lat},{p_lng}&destination={d_lat},{d_lng}" 

        final_location = f"**Pickup:** {data['location']}\n**Drop-off:** {data.get('drop_location', 'N/A')}\n**🗺️ Route Map:** {direction_link}" 
        
        hours_label = "Point-to-Point (Kilo Car)"
        points_required = 1.0  
        hours_db_value = 0
        
        if p_lat and p_lng and d_lat and d_lng:
            dist_km = calculate_distance(p_lat, p_lng, d_lat, d_lng)
            if dist_km <= 5.0:
                calculated_fare = 8500.0 
            else:
                extra_km = math.ceil(dist_km - 5.0)
                calculated_fare = 8500.0 + (extra_km * 1100.0) 
            fare_display = f"{calculated_fare:,.0f} MMK ({dist_km:.1f} km)"
            fare_db_value = calculated_fare
        else:
            fare_display = "8,500 MMK (Base 5km Rate)" 
            fare_db_value = 8500.0 
    else:
        final_location = data['location']
        hours_label = f"{data['hours']} Hours"
        points_required = float(data['hours'])
        fare_display = f"{data['fare']:,.0f} MMK"
        fare_db_value = float(data['fare'])
        hours_db_value = int(data['hours'])

    summary = ( 
        f"✅ **BOOKING CONFIRMED**\n\n" 
        f"🆔 Booking ID: `{booking_id}`\n" 
        f"🚙 Vehicle: {vehicle}\n" 
        f"📅 Date: {data['date']}\n" 
        f"🕐 Time: {data['time']}\n" 
        f"⏱ Package: {hours_label}\n" 
        f"📍 Location:\n{final_location}\n" 
        f"👥 Passengers: {data['passengers']}\n" 
        f"📞 Contact: `{phone}`\n\n" 
        f"💰 **Total Fare: {fare_display}**" 
    ) 
     
    cancel_kb = [[InlineKeyboardButton("❌ Cancel Booking", callback_data=f"ccancel_{booking_id}")]] # <== Updated 18Sep26
    await update.message.reply_text(
        summary, 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(cancel_kb), # <== Updated 18Sep26
        disable_web_page_preview=True
    ) 
     
    async with AsyncSessionLocal() as session: 
        booking = Booking( 
            id=booking_id, 
            customer_id=update.message.from_user.id, 
            vehicle=vehicle, 
            date_str=data['date'], 
            time_str=data['time'], 
            hours=hours_db_value, 
            location=final_location, 
            passengers=int(data['passengers']), 
            fare_mmk=fare_db_value, 
            status="AVAILABLE", 
            payment_method="DIRECT", 
            payment_receipt_file_id=None,
            customer_phone=phone 
        ) 
        session.add(booking) 
        await session.commit() 
         
    if DRIVER_GROUP_ID: 
        try: 
            driver_text = ( 
                f"🚗 **NEW JOB AVAILABLE**\n\n" 
                f"🆔 `{booking.id}`\n" 
                f"📅 {booking.date_str} | 🕐 {booking.time_str}\n" 
                f"⏱ Package: {hours_label} | 👥 {booking.passengers} Pax\n" 
                f"🚙 Vehicle: {booking.vehicle}\n"
                f"📍 **Location:**\n{final_location}\n\n" 
                f"💰 Fare: **{fare_display}**\n" 
                f"➕ Commission Deduction: **{points_required:,.0f} Points**" 
            ) 
            kb = [[InlineKeyboardButton("✅ ACCEPT JOB", callback_data=f"accept_{booking.id}")]] 
            await context.bot.send_message(chat_id=DRIVER_GROUP_ID, text=driver_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True) 
        except Exception as e: 
            logger.error(f"Failed to send to DRIVER_GROUP_ID: {e}") 
             
    return ConversationHandler.END

# <== Updated 18Sep26: Customer Cancel Function Added Below
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
     
    async with AsyncSessionLocal() as session: 
        driver = (await session.execute(select(Driver).where(Driver.telegram_id == driver_user.id))).scalar_one_or_none() 
        if not driver or not driver.is_approved: 
            await query.answer("❌ You are not an approved driver!", show_alert=True) 
            return 
 
        booking = (await session.execute(select(Booking).where(Booking.id == b_id).with_for_update())).scalar_one_or_none() 
        if not booking or booking.status != "AVAILABLE": 
            await query.answer("❌ Job no longer available!", show_alert=True) 
            return 

        conflict_stmt = select(Booking).where( 
            Booking.driver_id == driver.telegram_id, 
            Booking.date_str == booking.date_str, 
            Booking.time_str == booking.time_str, 
            Booking.status.in_(["ASSIGNED", "ON_THE_WAY", "DRIVER_ARRIVED", "TRIP_STARTED"]) 
        ) 
        has_conflict = (await session.execute(conflict_stmt)).scalar_one_or_none() 

        if has_conflict: 
            await query.answer(f"❌ Schedule conflict! You already have a job on {booking.date_str} at {booking.time_str}.", show_alert=True) 
            return 
             
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
 
        customer_phone = getattr(booking, 'customer_phone', None) or 'N/A'
        driver_phone = getattr(driver, 'phone', None) or 'N/A'
        fare_display = f"{booking.fare_mmk:,.0f} MMK" if booking.fare_mmk > 0 else "Kilo Car Rate"
         
        await query.edit_message_text(text=f"🔒 **JOB #{b_id} ACCEPTED**\nDriver: {driver.name}", parse_mode="Markdown") 
        await query.answer("✅ Job accepted!") 
 
        kb = [
            [InlineKeyboardButton("🏎️ On The Way", callback_data=f"ontheway_{b_id}")],
            [InlineKeyboardButton("❌ Cancel Job", callback_data=f"dcancelreq_{b_id}")] # <== Updated 18Sep26
        ]
        await context.bot.send_message(
            chat_id=driver_user.id, 
            text=f"📋 **ACCEPTED TRIP (#{b_id})**\nVehicle: {booking.vehicle}\nLocation:\n{booking.location}\n📞 **Customer Phone:** `{customer_phone}`\nFare: **{fare_display}**", 
            parse_mode="Markdown", 
            reply_markup=InlineKeyboardMarkup(kb)
        ) 
 
        await context.bot.send_message(chat_id=booking.customer_id, text=f"🚖 **DRIVER ASSIGNED!**\nName: {driver.name}\nPhone: `{driver_phone}`", parse_mode="Markdown")

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
            DATE: [CallbackQueryHandler(date_chosen)],
            TIME: [CallbackQueryHandler(time_chosen)],
            HOURS: [CallbackQueryHandler(hours_chosen)], 
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)], 
            DROP_LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, drop_location_received)],  
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, passengers_received)], 
            C_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, customer_phone_received)] 
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
