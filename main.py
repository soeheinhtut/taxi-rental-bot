import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
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

# 1 Point per job commission & Minimum required balance
DRIVER_COMMISSION_POINTS = 1.0
MIN_WALLET_POINTS = 1.0
MMK_PER_POINT = 1000  # 1 Point = 1,000 MMK

# Top-Up Packages
TOPUP_PACKAGES = {
    "pkg_10": {"points": 10, "price": 10 * MMK_PER_POINT},
    "pkg_50": {"points": 50, "price": 50 * MMK_PER_POINT},
    "pkg_100": {"points": 100, "price": 100 * MMK_PER_POINT},
    "pkg_1000": {"points": 1000, "price": 1000 * MMK_PER_POINT},
}

# Conversation States
VEHICLE, DATE, TIME, HOURS, LOCATION, PASSENGERS, PAYMENT_RECEIPT = range(7)
D_NAME, D_VEHICLE, D_PLATE = range(7, 10)
TOPUP_PKG, TOPUP_RECEIPT = range(10, 12)

PACKAGES = {
    "Sedan": {"fare": 45000},
    "SUV": {"fare": 60000},
    "Alphard / VIP": {"fare": 100000}
}

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
            f"*(1 Job = {DRIVER_COMMISSION_POINTS} Point deduction)*",
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
            f"*(1 Job = {DRIVER_COMMISSION_POINTS} Point deduction)*",
            parse_mode="Markdown"
        )

# --- CUSTOMER BOOKING FLOW ---
async def start_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Sedan (45,000 MMK)", callback_data="Sedan")],
        [InlineKeyboardButton("SUV (60,000 MMK)", callback_data="SUV")],
        [InlineKeyboardButton("Alphard / VIP (100,000 MMK)", callback_data="Alphard / VIP")]
    ]
    await query.edit_message_text("🚘 Select Vehicle Type:", reply_markup=InlineKeyboardMarkup(keyboard))
    return VEHICLE

async def vehicle_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['vehicle'] = query.data
    context.user_data['fare'] = PACKAGES[query.data]['fare']
    
    await query.edit_message_text("📅 Enter Rental Date (e.g., 26 Aug 2026):")
    return DATE

async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['date'] = update.message.text
    await update.message.reply_text("🕐 Enter Pickup Time (e.g., 10:00):")
    return TIME

async def time_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['time'] = update.message.text
    await update.message.reply_text("⏱ Select Total Hours Needed:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("3 Hours", callback_data="3"), InlineKeyboardButton("4 Hours", callback_data="4")],
        [InlineKeyboardButton("6 Hours", callback_data="6"), InlineKeyboardButton("10 Hours", callback_data="10")]
    ]))
    return HOURS

async def hours_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['hours'] = int(query.data)
    
    location_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share GPS Location", request_location=True)]],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    await query.message.reply_text(
        "📍 Please click below to share your exact GPS pickup location:",
        reply_markup=location_keyboard
    )
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if loc:
        context.user_data['location'] = f"{loc.latitude},{loc.longitude}"
    else:
        context.user_data['location'] = update.message.text

    await update.message.reply_text("👥 How many passengers will be riding?", reply_markup=ReplyKeyboardRemove())
    return PASSENGERS

async def passengers_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['passengers'] = update.message.text
    data = context.user_data
    summary = (
        f"🧾 **BOOKING SUMMARY**\n\n"
        f"🚙 Vehicle: {data['vehicle']}\n"
        f"📅 Date: {data['date']}\n"
        f"🕐 Time: {data['time']}\n"
        f"⏱ Hours: {data['hours']} Hours\n"
        f"📍 Location: `{data['location']}`\n"
        f"👥 Passengers: {data['passengers']}\n"
        f"💰 Total Fare: {data['fare']:,} MMK\n\n"
        f"Please choose your payment method:"
    )
    keyboard = [[InlineKeyboardButton("KBZPay / WavePay", callback_data="pay_kbzwave")]]
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return PAYMENT_RECEIPT

async def payment_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['payment_method'] = "KBZPay/WavePay"
    
    await query.edit_message_text(
        "💳 **Payment Instructions**\n\n"
        "Please transfer total amount to:\n"
        "• KBZPay / WavePay: `09-912345678`\n\n"
        "📸 **After transferring, please upload a screenshot of your payment receipt.**",
        parse_mode="Markdown"
    )
    return PAYMENT_RECEIPT

async def receipt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Please upload a photo image of your payment receipt.")
        return PAYMENT_RECEIPT
        
    photo_file = await update.message.photo[-1].get_file()
    file_id = photo_file.file_id
    booking_id = f"RNT-{datetime.now().strftime('%Y%m%d')}-{int(datetime.now().timestamp()) % 10000}"
    data = context.user_data
    
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
            status="PAYMENT_REVIEW",
            payment_method=data['payment_method'],
            payment_receipt_file_id=file_id
        )
        session.add(booking)
        await session.commit()
        
    await update.message.reply_text(f"✅ Receipt uploaded successfully! Your Booking ID is **{booking_id}**. Pending Admin Approval.", parse_mode="Markdown")
    
    if ADMIN_GROUP_ID:
        try:
            admin_text = (
                f"💳 **PAYMENT REVIEW**\n\n"
                f"Booking: `{booking_id}`\n"
                f"Method: {data['payment_method']}\n"
                f"Amount: {data['fare']:,} MMK\n"
                f"Customer ID: {update.message.from_user.id}"
            )
            keyboard = [
                [InlineKeyboardButton("✅ Approve Payment", callback_data=f"approve_pay_{booking_id}"),
                 InlineKeyboardButton("❌ Reject Payment", callback_data=f"reject_pay_{booking_id}")]
            ]
            await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Failed to send to ADMIN_GROUP_ID: {e}")
        
    return ConversationHandler.END

# --- DRIVER REGISTRATION FLOW ---
async def driver_register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 Please enter your **Full Name**:")
    return D_NAME

async def driver_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['driver_name'] = update.message.text
    await update.message.reply_text("🚗 Please enter your **Vehicle Brand and Model** (e.g., Toyota Crown / Sedan):")
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
            driver = Driver(telegram_id=user.id, name=data['driver_name'], username=user.username, wallet_balance=0.0, is_approved=False)
            session.add(driver)
        else:
            driver.name = data['driver_name']
        await session.commit()
        
    await update.message.reply_text("✅ Registration details submitted! Please wait for admin approval.")
    
    if ADMIN_GROUP_ID:
        try:
            text = (
                f"👨‍✈️ **NEW DRIVER REGISTRATION**\n\n"
                f"👤 Name: {data['driver_name']}\n"
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
        try:
            invite_link = await context.bot.create_chat_invite_link(chat_id=DRIVER_GROUP_ID, member_limit=1)
            join_msg = f"🎉 Your driver account is approved!\n\nPlease join the Driver Group here: {invite_link.invite_link}"
        except Exception:
            join_msg = "🎉 Your driver account is approved!"
        await context.bot.send_message(chat_id=d_id, text=join_msg)

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

    elif data.startswith("approve_pay_"):
        b_id = data.split("_")[2]
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Booking).where(Booking.id == b_id))
            booking = res.scalar_one_or_none()
            if booking and booking.status == "PAYMENT_REVIEW":
                booking.status = "AVAILABLE"
                await session.commit()
                
                if DRIVER_GROUP_ID:
                    try:
                        driver_text = (
                            f"🚗 **NEW HOURLY RENTAL**\n\n"
                            f"🆔 `{booking.id}`\n"
                            f"📅 {booking.date_str} | 🕐 {booking.time_str}\n"
                            f"⏱ {booking.hours} Hours | 👥 {booking.passengers} Pax\n"
                            f"🚙 Vehicle: {booking.vehicle}\n"
                            f"💰 Fare: {booking.fare_mmk:,.0f} MMK\n"
                            f"➕ Commission Deduction: **{DRIVER_COMMISSION_POINTS} Point**"
                        )
                        kb = [[InlineKeyboardButton("✅ ACCEPT JOB", callback_data=f"accept_{booking.id}")]]
                        await context.bot.send_message(chat_id=DRIVER_GROUP_ID, text=driver_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
                    except Exception:
                        pass
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **PAYMENT APPROVED**", parse_mode="Markdown")

    elif data.startswith("reject_pay_"):
        b_id = data.split("_")[2]
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Booking).where(Booking.id == b_id))
            booking = res.scalar_one_or_none()
            if booking:
                booking.status = "CANCELLED"
                await session.commit()
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **PAYMENT REJECTED**", parse_mode="Markdown")

# --- ACCEPT JOB (DEDUCTS POINT & SENDS LOCATION + DETAILS PRIVATELY) ---
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
            
        if driver.wallet_balance < MIN_WALLET_POINTS:
            await query.answer(f"❌ Insufficient points ({driver.wallet_balance:,.0f}). Minimum required: {MIN_WALLET_POINTS} Point. Top up needed!", show_alert=True)
            return

        b_res = await session.execute(select(Booking).where(Booking.id == b_id).with_for_update())
        booking = b_res.scalar_one_or_none()
        
        if not booking or booking.status != "AVAILABLE":
            await query.answer("❌ Sorry, this job is no longer available!", show_alert=True)
            return
            
        # Deduct point & assign driver
        driver.wallet_balance -= DRIVER_COMMISSION_POINTS
        booking.status = "ASSIGNED"
        booking.driver_id = driver.telegram_id
        booking.driver_name = driver.name
        
        tx = WalletTransaction(
            driver_telegram_id=driver.telegram_id,
            amount=-DRIVER_COMMISSION_POINTS,
            type="COMMISSION",
            booking_id=booking.id
        )
        session.add(tx)
        await session.commit()
        
        customer_id = booking.customer_id
        location = booking.location
        fare = booking.fare_mmk
        vehicle = booking.vehicle
        date_str = booking.date_str
        time_str = booking.time_str
        hours = booking.hours
        passengers = booking.passengers

    # Check if location contains latitude/longitude coordinates
    maps_link = ""
    is_coords = False
    lat, lng = 0.0, 0.0
    try:
        parts = [float(p.strip()) for p in location.split(",")]
        if len(parts) == 2:
            lat, lng = parts[0], parts[1]
            maps_link = f"\n🗺 **Google Maps Link:** https://www.google.com/maps/search/?api=1&query={lat},{lng}\n"
            is_coords = True
    except (ValueError, TypeError):
        pass

    driver_private_text = (
        f"🎯 **JOB ASSIGNED: #{b_id}**\n\n"
        f"🚙 Vehicle: {vehicle}\n"
        f"📅 Date & Time: {date_str} at {time_str} ({hours} Hours)\n"
        f"👥 Passengers: {passengers}\n"
        f"📍 Pickup Location: `{location}`{maps_link}\n"
        f"💰 Fare to collect: {fare:,.0f} MMK\n\n"
        f"💬 **Customer Contact:**\n"
        f"You can contact the customer directly using the button below."
    )
    
    driver_kb = [
        [InlineKeyboardButton("💬 Chat with Customer", url=f"tg://user?id={customer_id}")],
        [InlineKeyboardButton("📍 Driver Arrived", callback_data=f"arrived_{b_id}")]
    ]
    
    # Send private message to driver
    try:
        await context.bot.send_message(
            chat_id=driver_user.id, 
            text=driver_private_text, 
            parse_mode="Markdown", 
            reply_markup=InlineKeyboardMarkup(driver_kb)
        )
        # Send interactive map pin if GPS coordinates were provided
        if is_coords:
            await context.bot.send_location(
                chat_id=driver_user.id,
                latitude=lat,
                longitude=lng
            )
    except Exception as e:
        logger.error(f"Failed to send private info to driver: {e}")
        bot_info = await context.bot.get_me()
        await query.answer(
            f"⚠️ Could not send details to direct PM!\n\nPlease open @{bot_info.username} and press /start first.",
            show_alert=True
        )
        return

    await query.answer("✅ Job accepted successfully!")
    
    # Update public group message safely
    await query.edit_message_text(
        text=f"✅ **JOB #{b_id} TAKEN**\nDriver: @{driver_user.username if driver_user.username else driver_user.first_name}\nRemaining Points: {driver.wallet_balance:,.0f}", 
        parse_mode="Markdown"
    )

    # Notify customer privately
    try:
        await context.bot.send_message(
            chat_id=customer_id, 
            text=f"🚖 **Driver Found!**\nYour driver (@{driver_user.username if driver_user.username else driver_user.first_name}) has accepted your booking and will contact you or head to your location.", 
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to notify customer: {e}")

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
            await context.bot.send_message(chat_id=booking.customer_id, text=f"📍 Your driver has arrived.")

        elif action == "starttrip":
            booking.status = "TRIP_STARTED"
            await session.commit()
            kb = [[InlineKeyboardButton("🏁 End Trip", callback_data=f"endtrip_{b_id}")]]
            await query.edit_message_text(text=f"▶️ **JOB #{b_id}**\nStatus: Trip Started", reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.send_message(chat_id=booking.customer_id, text=f"▶️ Your trip has started.")

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
            PAYMENT_RECEIPT: [CallbackQueryHandler(payment_method_chosen, pattern="^pay_kbzwave$"), MessageHandler(filters.PHOTO, receipt_received)]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    # Driver Registration Flow
    driver_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(driver_register_start, pattern="^driver_register$")],
        states={
            D_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_name_received)],
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
    telegram_app.add_handler(CallbackQueryHandler(admin_actions, pattern="^(approve_|tapp_|trej_|approve_pay_|reject_pay_)"))
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
