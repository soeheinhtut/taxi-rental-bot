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

MMK_PER_POINT = 1000  # 1 Point = 1,000 MMK

# Hourly rates per vehicle type
HOURLY_RATES = {
    "Sedan": 15000,
    "SUV": 20000,
    "Alphard / VIP": 25000
}

# Top-Up Packages
TOPUP_PACKAGES = {
    "pkg_10": {"points": 10, "price": 10 * MMK_PER_POINT},
    "pkg_50": {"points": 50, "price": 50 * MMK_PER_POINT},
    "pkg_100": {"points": 100, "price": 100 * MMK_PER_POINT},
    "pkg_1000": {"points": 1000, "price": 1000 * MMK_PER_POINT},
}

# Conversation States
VEHICLE, DATE, TIME, HOURS, LOCATION, PASSENGERS, C_PHONE = range(7)
D_NAME, D_PHONE, D_VEHICLE, D_PLATE = range(7, 11)
TOPUP_PKG, TOPUP_RECEIPT = range(11, 13)

app = FastAPI()
telegram_app = None

# --- START & MAIN MENU ---
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

# --- CHECK BALANCE FUNCTION ---
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
            f"Remaining Balance: **{driver.wallet_balance:,.0f} Points**\n"
            f"*(1 Hour = 1 Point deduction | 1 Day = 10 Points deduction)*",
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
            f"Remaining Balance: **{driver.wallet_balance:,.0f} Points**\n"
            f"*(1 Hour = 1 Point deduction | 1 Day = 10 Points deduction)*",
            parse_mode="Markdown"
        )

# --- CUSTOMER BOOKING FLOW ---
async def start_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
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
    
    await query.edit_message_text(f"🚘 Vehicle: **{query.data}**\n\n📅 Enter Rental Date (e.g., 26 Aug 2026):", parse_mode="Markdown")
    return DATE

async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['date'] = update.message.text
    await update.message.reply_text("🕐 Enter Pickup Time (e.g., 10:00 AM):")
    return TIME

async def time_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['time'] = update.message.text
    vehicle = context.user_data.get('vehicle', 'Sedan')
    rate = HOURLY_RATES.get(vehicle, 15000)
    
    keyboard = [
        [InlineKeyboardButton(f"1 Hour ({1 * rate:,.0f} MMK)", callback_data="1")],
        [InlineKeyboardButton(f"2 Hours ({2 * rate:,.0f} MMK)", callback_data="2")],
        [InlineKeyboardButton(f"3 Hours ({3 * rate:,.0f} MMK)", callback_data="3")],
        [InlineKeyboardButton(f"6 Hours ({6 * rate:,.0f} MMK)", callback_data="6")],
        [InlineKeyboardButton(f"1 Day / 10 Hours ({10 * rate:,.0f} MMK)", callback_data="10")]
    ]
    
    await update.message.reply_text(
        f"⏱ Select Rental Package for **{vehicle}**:\n"
        f"*(Rate: {rate:,.0f} MMK / hour)*\n\n"
        f"💡 *(Price and Trip plan details can negotiate directly with the Driver)*",
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
    
    hours_label = "1 Day (10 Hours)" if hours == 10 else f"{hours} Hours"
    
    location_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    
    await query.edit_message_text(
        f"⏱ **Package Selected:** {hours_label}\n"
        f"💰 **Total Fare:** {total_fare:,.0f} MMK\n\n"
        f"📍 Please click below to share your exact GPS pickup location or type your address:",
        parse_mode="Markdown"
    )
    await query.message.reply_text(
        "Click button to send GPS location:",
        reply_markup=location_keyboard
    )
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if loc:
        context.user_data['location'] = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"
    else:
        context.user_data['location'] = update.message.text
        
    await update.message.reply_text("👥 How many passengers will be riding?", reply_markup=ReplyKeyboardRemove())
    return PASSENGERS

async def passengers_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['passengers'] = update.message.text
    
    phone_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Share Contact Phone", request_contact=True)]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    await update.message.reply_text(
        "📞 Please enter or share your **Phone Contact Number** so the driver can reach you:",
        reply_markup=phone_keyboard
    )
    return C_PHONE

async def customer_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    phone = contact.phone_number if contact else update.message.text
        
    context.user_data['customer_phone'] = phone
    data = context.user_data
    booking_id = f"RNT-{datetime.now().strftime('%Y%m%d')}-{int(datetime.now().timestamp()) % 10000}"
    
    hours_label = "1 Day (10 Hours)" if data['hours'] == 10 else f"{data['hours']} Hours"
    points_required = float(data['hours'])
    
    summary = (
        f"✅ **BOOKING CONFIRMED**\n\n"
        f"🆔 Booking ID: `{booking_id}`\n"
        f"🚙 Vehicle: {data['vehicle']}\n"
        f"📅 Date: {data['date']}\n"
        f"🕐 Time: {data['time']}\n"
        f"⏱ Package: {hours_label}\n"
        f"📍 Location: [Pickup Location]({data['location']})\n"
        f"👥 Passengers: {data['passengers']}\n"
        f"📞 Contact: `{phone}`\n\n"
        f"💰 **Total Fare: {data['fare']:,.0f} MMK (Pay directly to driver)**\n\n"
        f"Searching for an available driver. You will be notified once a driver accepts your trip!"
    )
    
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(), disable_web_page_preview=True)
    
    async with AsyncSessionLocal() as session:
        booking = Booking(
            id=booking_id,
            customer_id=update.message.from_user.id,
            vehicle=data['vehicle'],
            date_str=data['date'],
            time_str=data['time'],
            hours=data['hours'],
            location=data['location'],
            passengers=int(data['passengers']),
            fare_mmk=data['fare'],
            status="AVAILABLE",
            payment_method="DIRECT",
            payment_receipt_file_id=None
        )
        if hasattr(booking, 'customer_phone'):
            booking.customer_phone = phone
        session.add(booking)
        await session.commit()
        
    if DRIVER_GROUP_ID:
        try:
            driver_text = (
                f"🚗 **NEW RENTAL JOB AVAILABLE**\n\n"
                f"🆔 `{booking.id}`\n"
                f"📅 {booking.date_str} | 🕐 {booking.time_str}\n"
                f"⏱ Package: {hours_label} | 👥 {booking.passengers} Pax\n"
                f"🚙 Vehicle: {booking.vehicle}\n"
                f"💰 Fare: **{booking.fare_mmk:,.0f} MMK** (Collect from customer)\n"
                f"➕ Commission Deduction: **{points_required:,.0f} Points**"
            )
            kb = [[InlineKeyboardButton("✅ ACCEPT JOB", callback_data=f"accept_{booking.id}")]]
            await context.bot.send_message(chat_id=DRIVER_GROUP_ID, text=driver_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logger.error(f"Failed to send to DRIVER_GROUP_ID: {e}")
            
    return ConversationHandler.END

# --- DRIVER REGISTRATION FLOW ---
async def driver_register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 Please enter your **Full Name**:")
    return D_NAME

async def driver_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['driver_name'] = update.message.text
    
    phone_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Share Phone Number", request_contact=True)]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    await update.message.reply_text("📞 Please enter or share your **Phone Contact Number**:", reply_markup=phone_keyboard)
    return D_PHONE

async def driver_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    context.user_data['driver_phone'] = contact.phone_number if contact else update.message.text
    
    await update.message.reply_text("🚗 Please enter your **Vehicle Brand and Model** (e.g., Toyota Crown / Sedan):", reply_markup=ReplyKeyboardRemove())
    return D_VEHICLE

async def driver_vehicle_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['driver_vehicle'] = update.message.text
    await update.message.reply_text("🔢 Please enter your **Car Plate Number** (e.g., 2A-1234):")
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
                is_approved=False
            )
            if hasattr(driver, 'phone'):
                driver.phone = data['driver_phone']
            session.add(driver)
        else:
            driver.name = data['driver_name']
            if hasattr(driver, 'phone'):
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
                f"🔢 Plate Number: `{plate_number}`\n"
                f"🆔 Telegram ID: `{user.id}`"
            )
            keyboard = [[InlineKeyboardButton("✅ Approve Driver", callback_data=f"approve_driver_{user.id}")]]
            await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Failed to send driver registration: {e}")
            
    return ConversationHandler.END

# --- DRIVER PACKAGE TOP UP FLOW ---
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
        [InlineKeyboardButton("10 Points (10,000 MMK)", callback_data="pkg_10"),
         InlineKeyboardButton("50 Points (50,000 MMK)", callback_data="pkg_50")],
        [InlineKeyboardButton("100 Points (100,000 MMK)", callback_data="pkg_100"),
         InlineKeyboardButton("1,000 Points (1,000,000 MMK)", callback_data="pkg_1000")]
    ]
    await query.edit_message_text(
        f"💳 **Driver Wallet Top-Up**\n\n"
        f"Current Balance: {driver.wallet_balance:,.0f} Points\n"
        f"Exchange Rule: `1 Point = {MMK_PER_POINT:,} MMK`\n\n"
        f"📦 **Please select a top-up package:**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TOPUP_PKG

async def topup_package_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    pkg_key = query.data
    if pkg_key not in TOPUP_PACKAGES:
        await query.edit_message_text("❌ Invalid package selected.")
        return ConversationHandler.END
        
    pkg = TOPUP_PACKAGES[pkg_key]
    context.user_data['topup_points'] = pkg["points"]
    context.user_data['topup_price'] = pkg["price"]
    
    await query.edit_message_text(
        f"💳 **Top-Up Package Selected: {pkg['points']} Points**\n\n"
        f"Total Price to Pay: **{pkg['price']:,} MMK**\n\n"
        f"Please transfer money to:\n"
        f"• KBZPay / WavePay: `09-912345678`\n\n"
        f"📸 **After paying, please upload a screenshot of your payment receipt.**",
        parse_mode="Markdown"
    )
    return TOPUP_RECEIPT

async def topup_receipt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Please upload a photo image of your payment receipt.")
        return TOPUP_RECEIPT
        
    photo_file = await update.message.photo[-1].get_file()
    file_id = photo_file.file_id
    user = update.message.from_user
    
    points = context.user_data.get('topup_points', 0)
    price = context.user_data.get('topup_price', 0)

    await update.message.reply_text("✅ Receipt uploaded successfully! Pending Admin Approval.", parse_mode="Markdown")

    if ADMIN_GROUP_ID:
        try:
            caption = (
                f"💰 **DRIVER TOP-UP REQUEST**\n\n"
                f"👤 Driver ID: `{user.id}`\n"
                f"🏷 Username: @{user.username if user.username else 'None'}\n"
                f"📦 Package: **{points} Points**\n"
                f"💵 Amount Paid: **{price:,.0f} MMK**"
            )
            keyboard = [
                [InlineKeyboardButton(f"✅ Approve (+{points} Pts)", callback_data=f"tapp_{user.id}_{points}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"trej_{user.id}")]
            ]
            await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=file_id, caption=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Failed to send topup receipt: {e}")
            
    return ConversationHandler.END

# --- ADMIN ACTIONS ---
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("approve_driver_"):
        d_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Driver).where(Driver.telegram_id == d_id))
            driver = res.scalar_one_or_none()
            if driver:
                driver.is_approved = True
                await session.commit()
        
        await query.edit_message_text(text=f"{query.message.text}\n\n✅ DRIVER APPROVED")
        
        # Robust invite link generation with fallback
        invite_link_str = None
        if DRIVER_GROUP_ID != 0:
            try:
                # Primary method: 1-time unique invite link
                link_obj = await context.bot.create_chat_invite_link(
                    chat_id=DRIVER_GROUP_ID, 
                    member_limit=1
                )
                invite_link_str = link_obj.invite_link
            except Exception as e:
                logger.error(f"create_chat_invite_link failed: {e}")
                try:
                    # Fallback method: export existing primary invite link
                    invite_link_str = await context.bot.export_chat_invite_link(chat_id=DRIVER_GROUP_ID)
                except Exception as e2:
                    logger.error(f"export_chat_invite_link failed: {e2}")

        if invite_link_str:
            join_msg = (
                f"🎉 **Your driver account has been approved!**\n\n"
                f"👉 Click the link below to join the Driver Dispatch Group:\n"
                f"{invite_link_str}"
            )
        else:
            join_msg = (
                f"🎉 **Your driver account has been approved!**\n\n"
                f"Please contact the administrator to get added to the Driver Dispatch Group."
            )

        try:
            await context.bot.send_message(chat_id=d_id, text=join_msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send approval message to driver {d_id}: {e}")

    elif data.startswith("tapp_"):
        parts = data.split("_")
        d_id = int(parts[1])
        points_to_add = float(parts[2])
        
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Driver).where(Driver.telegram_id == d_id))
            driver = res.scalar_one_or_none()
            if driver:
                driver.wallet_balance += points_to_add
                tx = WalletTransaction(driver_telegram_id=d_id, amount=points_to_add, type="TOP_UP")
                session.add(tx)
                await session.commit()
                new_balance = driver.wallet_balance
                
        await query.edit_message_caption(caption=query.message.caption + f"\n\n✅ **APPROVED (+{points_to_add:,.0f} POINTS)**", parse_mode="Markdown")
        await context.bot.send_message(
            chat_id=d_id, 
            text=f"✅ **Top-up Approved!**\nAdmin added **{points_to_add:,.0f} Points** to your wallet.\nNew Balance: **{new_balance:,.0f} Points**", 
            parse_mode="Markdown"
        )

    elif data.startswith("trej_"):
        d_id = int(data.split("_")[1])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **TOP-UP REJECTED**", parse_mode="Markdown")
        await context.bot.send_message(chat_id=d_id, text="❌ Your wallet top-up request was rejected by the admin.")

# --- ACCEPT JOB ---
async def accept_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    driver_user = query.from_user
    b_id = query.data.split("_")[1]
    
    async with AsyncSessionLocal() as session:
        d_res = await session.execute(select(Driver).where(Driver.telegram_id == driver_user.id))
        driver = d_res.scalar_one_or_none()
        
        if not driver or not driver.is_approved:
            await query.answer("❌ You are not an approved driver yet!", show_alert=True)
            return

        b_res = await session.execute(select(Booking).where(Booking.id == b_id).with_for_update())
        booking = b_res.scalar_one_or_none()
        
        if not booking or booking.status != "AVAILABLE":
            await query.answer("❌ Sorry, this job is no longer available!", show_alert=True)
            return
            
        required_points = float(booking.hours)
        
        if driver.wallet_balance < required_points:
            await query.answer(f"❌ Insufficient points ({driver.wallet_balance:,.0f}). Required: {required_points:,.0f} Points. Top up needed!", show_alert=True)
            return

        driver.wallet_balance -= required_points
        booking.status = "ASSIGNED"
        booking.driver_id = driver.telegram_id
        booking.driver_name = driver.name
        
        tx = WalletTransaction(
            driver_telegram_id=driver.telegram_id,
            amount=-required_points,
            type="COMMISSION",
            booking_id=booking.id
        )
        session.add(tx)
        await session.commit()

        customer_phone = getattr(booking, 'customer_phone', 'N/A')
        driver_phone = getattr(driver, 'phone', 'N/A')
        booking_hours = booking.hours
        hours_label = "1 Day (10 Hours)" if booking_hours == 10 else f"{booking_hours} Hours"
        
        # 1. Clear details from Dispatch Group
        await query.edit_message_text(
            text=f"🔒 **JOB #{b_id} ACCEPTED**\n\n"
                 f"Driver: {driver.name} (@{driver_user.username if driver_user.username else 'NoUsername'})\n"
                 f"Status: Job is locked and no longer available.",
            parse_mode="Markdown"
        )
        await query.answer("✅ Job accepted successfully!")

        # 2. Send complete Trip details & Customer contact privately to the Driver
        driver_trip_msg = (
            f"📋 **ACCEPTED TRIP DETAILS (#{b_id})**\n\n"
            f"📅 Date: {booking.date_str} | 🕐 Time: {booking.time_str}\n"
            f"⏱ Package: {hours_label}\n"
            f"🚙 Vehicle Type: {booking.vehicle}\n"
            f"👥 Passengers: {booking.passengers}\n"
            f"📍 Location: [Open Pickup Location]({booking.location})\n"
            f"📞 **Customer Phone:** `{customer_phone}`\n"
            f"💰 Fare to Collect: **{booking.fare_mmk:,.0f} MMK**\n\n"
            f"Please call the customer to confirm the trip details."
        )
        kb_driver = [[InlineKeyboardButton("📍 Driver Arrived", callback_data=f"arrived_{b_id}")]]
        await context.bot.send_message(
            chat_id=driver_user.id, 
            text=driver_trip_msg, 
            parse_mode="Markdown", 
            reply_markup=InlineKeyboardMarkup(kb_driver),
            disable_web_page_preview=True
        )

        # 3. Send Driver info & Driver contact privately to the Customer
        customer_msg = (
            f"🚖 **DRIVER ASSIGNED!**\n\n"
            f"Your driver is on the way for Booking `{b_id}`.\n\n"
            f"👨‍✈️ **Driver Name:** {driver.name}\n"
            f"📞 **Driver Phone:** `{driver_phone}`\n"
            f"🏷 **Telegram:** @{driver_user.username if driver_user.username else 'N/A'}\n\n"
            f"You can contact your driver directly."
        )
        await context.bot.send_message(chat_id=booking.customer_id, text=customer_msg, parse_mode="Markdown")

async def trip_lifecycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, b_id = query.data.split("_")
    
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Booking).where(Booking.id == b_id))
        booking = res.scalar_one_or_none()
        if not booking:
            return
            
        if action == "arrived":
            booking.status = "DRIVER_ARRIVED"
            await session.commit()
            kb = [[InlineKeyboardButton("▶️ Start Trip", callback_data=f"starttrip_{b_id}")]]
            await query.edit_message_text(text=f"📍 **JOB #{b_id}**\nStatus: Driver Arrived", reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.send_message(chat_id=booking.customer_id, text=f"📍 Your driver has arrived at the pickup location.")

        elif action == "starttrip":
            booking.status = "TRIP_STARTED"
            await session.commit()
            kb = [[InlineKeyboardButton("🏁 End Trip", callback_data=f"endtrip_{b_id}")]]
            await query.edit_message_text(text=f"▶️ **JOB #{b_id}**\nStatus: Trip Started", reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.send_message(chat_id=booking.customer_id, text=f"▶️ Your trip has started. Have a safe journey!")

        elif action == "endtrip":
            booking.status = "TRIP_COMPLETED"
            await session.commit()
            await query.edit_message_text(text=f"🏁 **JOB #{b_id}**\nStatus: Completed Successfully!")
            await context.bot.send_message(chat_id=booking.customer_id, text=f"🏁 **Rental Completed!** Thank you for riding with us!")

# --- APP STARTUP ---
@app.on_event("startup")
async def startup_event():
    global telegram_app
    try:
        await init_db()
        logger.info("Database connected successfully!")
    except Exception as e:
        logger.error(f"Database Connection Failed: {e}")
    
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # Customer Booking Conversation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start_booking_callback, pattern="^start_booking$")],
        states={
            VEHICLE: [CallbackQueryHandler(vehicle_chosen)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received)],
            HOURS: [CallbackQueryHandler(hours_chosen)],
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)],
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, passengers_received)],
            C_PHONE: [MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, customer_phone_received)]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    # Driver Registration Flow
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

    # Driver Package Top-Up Flow
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
