import os
import logging
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

HOURLY_RATES = {
    "Sedan": 15000,
    "SUV": 20000,
    "Alphard / VIP": 25000
}

TOPUP_PACKAGES = {
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type != "private":
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("🚗 Book a Car", callback_data="start_booking")],
        [InlineKeyboardButton("👨‍✈️ Driver Register", callback_data="driver_register")],
        [InlineKeyboardButton("💳 Driver Top Up", callback_data="topup_start")],
        [InlineKeyboardButton("💰 Check Balance", callback_data="driver_balance")]
    ]
    await update.message.reply_text(
        "Welcome to Taxi Rental Service! Please choose an option:",
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

        await query.edit_message_text(
            f"👤 **Driver Wallet Status**\n\n"
            f"Name: {driver.name}\n"
            f"Remaining Balance: **{driver.wallet_balance:,.0f} Points**",
            parse_mode="Markdown"
        )

async def check_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Driver).where(Driver.telegram_id == update.effective_user.id))
        driver = res.scalar_one_or_none()
        
        if not driver or not driver.is_approved:
            await update.message.reply_text("❌ You are not an approved driver yet.")
            return

        await update.message.reply_text(
            f"👤 **Driver Wallet Status**\n\n"
            f"Name: {driver.name}\n"
            f"Remaining Balance: **{driver.wallet_balance:,.0f} Points**",
            parse_mode="Markdown"
        )

async def start_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🚕 TAXI (Point-to-Point)", callback_data="TAXI")],
        [InlineKeyboardButton("Sedan (15,000 MMK / hr)", callback_data="Sedan")],
        [InlineKeyboardButton("SUV (20,000 MMK / hr)", callback_data="SUV")],
        [InlineKeyboardButton("Alphard / VIP (25,000 MMK / hr)", callback_data="Alphard / VIP")]
    ]
    await query.edit_message_text("🚘 Select Vehicle Type:", reply_markup=InlineKeyboardMarkup(keyboard))
    return VEHICLE

async def vehicle_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['vehicle'] = query.data
    await query.edit_message_text(f"🚘 Vehicle: **{query.data}**\n\n📅 Enter Date (e.g., 26 Aug 2026):", parse_mode="Markdown")
    return DATE

async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['date'] = update.message.text
    await update.message.reply_text("🕐 Enter Pickup Time (e.g., 10:00 AM):")
    return TIME

async def time_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['time'] = update.message.text
    vehicle = context.user_data.get('vehicle', 'Sedan')
    
    if vehicle == "TAXI":
        location_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
            one_time_keyboard=True, resize_keyboard=True
        )
        await update.message.reply_text("📍 Please click below to share your exact GPS Pickup Location or type your address:", reply_markup=location_keyboard)
        return LOCATION

    rate = HOURLY_RATES.get(vehicle, 15000)
    keyboard = [
        [InlineKeyboardButton(f"1 Hour ({1 * rate:,.0f} MMK)", callback_data="1")],
        [InlineKeyboardButton(f"2 Hours ({2 * rate:,.0f} MMK)", callback_data="2")],
        [InlineKeyboardButton(f"3 Hours ({3 * rate:,.0f} MMK)", callback_data="3")],
        [InlineKeyboardButton(f"6 Hours ({6 * rate:,.0f} MMK)", callback_data="6")],
        [InlineKeyboardButton(f"1 Day / 10 Hours ({10 * rate:,.0f} MMK)", callback_data="10")]
    ]
    await update.message.reply_text(
        f"⏱ Select Rental Package for **{vehicle}**:\n*(Rate: {rate:,.0f} MMK / hour)*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return HOURS

async def hours_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    hours = int(query.data)
    vehicle = context.user_data['vehicle']
    rate = HOURLY_RATES[vehicle]
    total_fare = hours * rate
    
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
    else: 
        context.user_data['location'] = update.message.text 
         
    if context.user_data.get('vehicle') == "TAXI":
        await update.message.reply_text("📍 Please enter your **Drop-off Location** (type address or landmark):", reply_markup=ReplyKeyboardRemove())
        return DROP_LOCATION

    await update.message.reply_text("👥 How many passengers will be riding?", reply_markup=ReplyKeyboardRemove()) 
    return PASSENGERS 

async def drop_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['drop_location'] = update.message.text
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
    if vehicle == 'TAXI':
        final_location = f"**Pickup:** {data['location']}\n**Drop-off:** {data.get('drop_location', 'N/A')}"
        hours_label = "Point-to-Point (TAXI)"
        points_required = 1.0  
        fare_display = "Negotiate directly with Driver"
        fare_db_value = 0.0
        hours_db_value = 0
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
     
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(), disable_web_page_preview=True) 
     
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
                wallet_balance=0.0,  
                is_approved=False,
                phone=data['driver_phone'] 
            ) 
            session.add(driver) 
        else: 
            driver.name = data['driver_name'] 
            driver.phone = data['driver_phone']
        await session.commit() 
         
    await update.message.reply_text("✅ Registration details submitted! Please wait for admin approval.") 
     
    if ADMIN_GROUP_ID: 
        try: 
            text = ( 
                f"👨‍✈️ **NEW DRIVER REGISTRATION**\n\n" 
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
    await query.edit_message_text(f"💳 Send payment for **{pkg['points']} Points ({pkg['price']:,} MMK)** and upload screenshot.") 
    return TOPUP_RECEIPT 
 
async def topup_receipt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: 
    if not update.message.photo: 
        await update.message.reply_text("Please upload screenshot.") 
        return TOPUP_RECEIPT 
    file_id = update.message.photo[-1].file_id 
    user = update.message.from_user 
    points = context.user_data.get('topup_points', 0) 
    price = context.user_data.get('topup_price', 0) 
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
             
        required_points = 1.0 if booking.vehicle == "TAXI" else float(booking.hours) 
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
        hours_label = "Point-to-Point (TAXI)" if booking.vehicle == "TAXI" else f"{booking.hours} Hours" 
        fare_display = "Negotiate directly with customer" if booking.vehicle == "TAXI" else f"{booking.fare_mmk:,.0f} MMK"
         
        await query.edit_message_text(text=f"🔒 **JOB #{b_id} ACCEPTED**\nDriver: {driver.name}", parse_mode="Markdown") 
        await query.answer("✅ Job accepted!") 
 
        await context.bot.send_message(
            chat_id=driver_user.id, 
            text=f"📋 **ACCEPTED TRIP (#{b_id})**\nVehicle: {booking.vehicle}\nLocation:\n{booking.location}\n📞 **Customer Phone:** `{customer_phone}`\nFare: **{fare_display}**", 
            parse_mode="Markdown", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Driver Arrived", callback_data=f"arrived_{b_id}")]])
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
        if action == "arrived": 
            booking.status = "DRIVER_ARRIVED" 
            await session.commit() 
            await query.edit_message_text(text=f"📍 **JOB #{b_id}**\nDriver Arrived", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start Trip", callback_data=f"starttrip_{b_id}")]])) 
            await context.bot.send_message(chat_id=booking.customer_id, text="📍 Driver has arrived.") 
        elif action == "starttrip": 
            booking.status = "TRIP_STARTED" 
            await session.commit() 
            await query.edit_message_text(text=f"▶️ **JOB #{b_id}**\nTrip Started", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏁 End Trip", callback_data=f"endtrip_{b_id}")]])) 
            await context.bot.send_message(chat_id=booking.customer_id, text="▶️ Trip started.") 
        elif action == "endtrip": 
            booking.status = "TRIP_COMPLETED" 
            await session.commit() 
            await query.edit_message_text(text=f"🏁 **JOB #{b_id}**\nCompleted") 
            await context.bot.send_message(chat_id=booking.customer_id, text="🏁 Trip completed.") 

@app.on_event("startup") 
async def startup_event(): 
    global telegram_app 
    await init_db() 
    telegram_app = Application.builder().token(BOT_TOKEN).build() 
     
    conv_handler = ConversationHandler( 
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start_booking_callback, pattern="^start_booking$")], 
        states={ 
            VEHICLE: [CallbackQueryHandler(vehicle_chosen)], 
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)], 
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received)], 
            HOURS: [CallbackQueryHandler(hours_chosen)], 
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)], 
            DROP_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, drop_location_received)],
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
    telegram_app.add_handler(CallbackQueryHandler(check_balance_callback, pattern="^driver_balance$")) 
    telegram_app.add_handler(CallbackQueryHandler(admin_actions, pattern="^(approve_|tapp_|trej_)")) 
    telegram_app.add_handler(CallbackQueryHandler(accept_job, pattern="^accept_")) 
    telegram_app.add_handler(CallbackQueryHandler(trip_lifecycle, pattern="^(arrived_|starttrip_|endtrip_)")) 
 
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
