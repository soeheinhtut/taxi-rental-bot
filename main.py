import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request
app = FastAPI()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from sqlalchemy import select
from database import AsyncSessionLocal, Booking, Driver, WalletTransaction, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DRIVER_GROUP_ID = int(os.getenv("DRIVER_GROUP_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
RUN_MODE = os.getenv("RUN_MODE", "polling")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
DRIVER_COMMISSION_MMK = float(os.getenv("DRIVER_COMMISSION_MMK", "2000"))
MIN_WALLET_MMK = float(os.getenv("MIN_WALLET_MMK", "2000"))

# Conversation States
VEHICLE, DATE, TIME, HOURS, LOCATION, PASSENGERS, PAYMENT_RECEIPT = range(7)

PACKAGES = {
    "Sedan": {"fare": 45000},
    "SUV": {"fare": 60000},
    "Alphard / VIP": {"fare": 100000}
}

# --- CUSTOMER BOOKING FLOW ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type != "private":
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("🚗 Book a Car", callback_data="start_booking")],
        [InlineKeyboardButton("👨‍✈️ Driver Register", callback_data="driver_register")]
    ]
    await update.message.reply_text(
        "Welcome to Taxi Rental Service! Please choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

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
    
    # Request GPS Location using native Telegram button
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
        context.user_data['location'] = f"{loc.latitude}, {loc.longitude}"
    else:
        context.user_data['location'] = update.message.text

    await update.message.reply_text(
        "👥 How many passengers will be riding?",
        reply_markup=ReplyKeyboardRemove()
    )
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
        f"📍 Location: {data['location']}\n"
        f"👥 Passengers: {data['passengers']}\n"
        f"💰 Total Fare: {data['fare']:,} MMK\n\n"
        f"Please choose your payment method:"
    )
    keyboard = [
        [InlineKeyboardButton("KBZPay / WavePay", callback_data="pay_kbzwave")]
    ]
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return PAYMENT_RECEIPT

async def payment_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['payment_method'] = "KBZPay/WavePay"
    
    await query.edit_message_text(
        "💳 **Payment Instructions**\n\n"
        "Please transfer total amount to:\n"
        "• KBZPay / WavePay: `09-912345678` (Demo Account)\n\n"
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
    context.user_data['receipt_file_id'] = file_id
    
    # Save Booking to DB with status PAYMENT_REVIEW
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
    
    # Notify Admin for Payment Approval
    if ADMIN_CHAT_ID:
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
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=file_id,
            caption=admin_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    return ConversationHandler.END

# --- DRIVER REGISTRATION ---
async def driver_register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Driver).where(Driver.telegram_id == user.id))
        driver = res.scalar_one_or_none()
        if not driver:
            driver = Driver(telegram_id=user.id, name=user.first_name, username=user.username, wallet_balance=0.0, is_approved=False)
            session.add(driver)
            await session.commit()
            
    await query.edit_message_text("📝 Registration request sent to admin. Please wait for approval.")
    
    if ADMIN_CHAT_ID:
        text = (
            f"👨‍✈️ **DRIVER REGISTRATION**\n\n"
            f"Name: {user.first_name}\n"
            f"Username: @{user.username if user.username else 'None'}\n"
            f"Telegram ID: `{user.id}`"
        )
        keyboard = [[InlineKeyboardButton("✅ Approve Driver", callback_data=f"approve_driver_{user.id}")]]
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

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
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **DRIVER APPROVED**", parse_mode="Markdown")
        await context.bot.send_message(chat_id=d_id, text="🎉 Your driver account has been approved by admin! You can now accept trips.")

    elif data.startswith("approve_pay_"):
        b_id = data.split("_")[2]
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Booking).where(Booking.id == b_id))
            booking = res.scalar_one_or_none()
            if booking and booking.status == "PAYMENT_REVIEW":
                booking.status = "AVAILABLE"
                await session.commit()
                
                # Broadcast to Driver Dispatch Group
                if DRIVER_GROUP_ID:
                    driver_text = (
                        f"🚗 **NEW HOURLY RENTAL**\n\n"
                        f"🆔 `{booking.id}`\n\n"
                        f"📅 {booking.date_str}\n"
                        f"🕐 {booking.time_str}\n"
                        f"⏱ {booking.hours} Hours\n\n"
                        f"📍 GPS: {booking.location}\n"
                        f"👥 Passengers: {booking.passengers}\n"
                        f"🚙 Vehicle: {booking.vehicle}\n\n"
                        f"💰 Fare: {booking.fare_mmk:,.0f} MMK\n"
                        f"➕ Commission Deduction: {DRIVER_COMMISSION_MMK:,.0f} MMK"
                    )
                    kb = [[InlineKeyboardButton("✅ ACCEPT JOB", callback_data=f"accept_{booking.id}")]]
                    await context.bot.send_message(chat_id=DRIVER_GROUP_ID, text=driver_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
                    
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **PAYMENT APPROVED & BROADCASTED**", parse_mode="Markdown")

    elif data.startswith("reject_pay_"):
        b_id = data.split("_")[2]
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Booking).where(Booking.id == b_id))
            booking = res.scalar_one_or_none()
            if booking:
                booking.status = "CANCELLED"
                await session.commit()
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **PAYMENT REJECTED**", parse_mode="Markdown")

# --- ATOMIC JOB LOCKING & WALLET CHECK ---
async def accept_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    driver_user = query.from_user
    b_id = query.data.split("_")[1]
    
    async with AsyncSessionLocal() as session:
        # Check if driver is registered and approved
        d_res = await session.execute(select(Driver).where(Driver.telegram_id == driver_user.id))
        driver = d_res.scalar_one_or_none()
        
        if not driver or not driver.is_approved:
            await query.answer("❌ You are not an approved driver yet!", show_alert=True)
            return
            
        if driver.wallet_balance < MIN_WALLET_MMK:
            await query.answer(f"❌ Insufficient wallet balance ({driver.wallet_balance:,.0f} MMK). Minimum required: {MIN_WALLET_MMK:,.0f} MMK. Top up required!", show_alert=True)
            return

        # Atomic Row Lock using PostgreSQL SELECT ... FOR UPDATE
        b_res = await session.execute(select(Booking).where(Booking.id == b_id).with_for_update())
        booking = b_res.scalar_one_or_none()
        
        if not booking or booking.status != "AVAILABLE":
            await query.answer("❌ Sorry, this job was already taken or is no longer available!", show_alert=True)
            return
            
        # Deduct Commission from Driver Wallet
        driver.wallet_balance -= DRIVER_COMMISSION_MMK
        
        # Lock Booking Assignment
        booking.status = "ASSIGNED"
        booking.driver_id = driver.telegram_id
        booking.driver_name = driver.name
        
        # Log Wallet Transaction
        tx = WalletTransaction(
            driver_telegram_id=driver.telegram_id,
            amount=-DRIVER_COMMISSION_MMK,
            type="COMMISSION",
            booking_id=booking.id
        )
        session.add(tx)
        await session.commit()
        
    driver_handle = f"@{driver_user.username}" if driver_user.username else driver_user.name
    await query.answer("✅ Job accepted successfully!")
    
    # Update Dispatch Group message
    await query.edit_message_text(
        text=f"✅ **JOB #{b_id} TAKEN**\nDriver: {driver_handle}\nWallet Balance: {driver.wallet_balance:,.0f} MMK",
        parse_mode="Markdown"
    )
    
    # Notify Customer with Lifecycle Actions
    kb = [[InlineKeyboardButton("📍 Driver Arrived", callback_data=f"arrived_{b_id}")]]
    await context.bot.send_message(
        chat_id=booking.customer_id,
        text=f"🚖 **Driver Found!**\nYour booking `{b_id}` was accepted by driver {driver_handle}.\n\nWhen the driver arrives, they will update status.",
        parse_mode="Markdown"
    )
    
    # Send control options to driver in private message
    await context.bot.send_message(
        chat_id=driver_user.id,
        text=f"📋 **Trip Management Dashboard for #{b_id}**",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# --- TRIP LIFECYCLE & OVERTIME ---
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
            await context.bot.send_message(chat_id=booking.customer_id, text=f"📍 Your driver has arrived at the pickup location for booking `{b_id}`.", parse_mode="Markdown")

        elif action == "starttrip":
            booking.status = "TRIP_STARTED"
            await session.commit()
            kb = [[InlineKeyboardButton("🏁 End Trip", callback_data=f"endtrip_{b_id}")]]
            await query.edit_message_text(text=f"▶️ **JOB #{b_id}**\nStatus: Trip Started", reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.send_message(chat_id=booking.customer_id, text=f"▶️ Your rental trip `{b_id}` has officially started.", parse_mode="Markdown")

        elif action == "endtrip":
            booking.status = "TRIP_COMPLETED"
            await session.commit()
            
            # Simple overtime mock simulation check (e.g., 5,000 MMK extra charge per overtime hour if applicable)
            overtime_hours = 0 
            extra_charge = overtime_hours * 5000
            total_payable = booking.fare_mmk + extra_charge
            
            await query.edit_message_text(text=f"🏁 **JOB #{b_id}**\nStatus: Completed Successfully!")
            await context.bot.send_message(
                chat_id=booking.customer_id,
                text=f"🏁 **Rental Completed!**\nBooking: `{b_id}`\nBase Fare: {booking.fare_mmk:,.0f} MMK\nOvertime Charges: {extra_charge:,.0f} MMK\nTotal Due: {total_payable:,.0f} MMK\n\nThank you for riding with us!",
                parse_mode="Markdown"
            )

# --- ADMIN WALLET TOP UP COMMAND ---
async def wallet_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    try:
        args = context.args
        target_telegram_id = int(args[0])
        amount = float(args[1])
        
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Driver).where(Driver.telegram_id == target_telegram_id))
            driver = res.scalar_one_or_none()
            if driver:
                driver.wallet_balance += amount
                tx = WalletTransaction(driver_telegram_id=driver.telegram_id, amount=amount, type="TOP_UP")
                session.add(tx)
                await session.commit()
                await update.message.reply_text(f"✅ Successfully added {amount:,.0f} MMK to driver {driver.name}. New balance: {driver.wallet_balance:,.0f} MMK")
            else:
                await update.message.reply_text("❌ Driver not found in database.")
    except Exception as e:
        await update.message.reply_text(f"Usage error: /wallet_add <DRIVER_TELEGRAM_ID> <AMOUNT>\nError: {e}")

# --- WEBHOOK & APP SETUP ---
app_fastapi = FastAPI()
telegram_app = None

@app_fastapi.on_event("startup")
async def startup_event():
    global telegram_app
    await init_db()
    
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # Register Conversations & Handlers
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            VEHICLE: [CallbackQueryHandler(vehicle_chosen)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_received)],
            HOURS: [CallbackQueryHandler(hours_chosen)],
            LOCATION: [MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, location_received)],
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, passengers_received)],
            PAYMENT_RECEIPT: [
                CallbackQueryHandler(payment_method_chosen, pattern="^pay_kbzwave$"),
                MessageHandler(filters.PHOTO, receipt_received)
            ]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    
    telegram_app.add_handler(conv_handler)
    telegram_app.add_handler(CallbackQueryHandler(driver_register_callback, pattern="^driver_register$"))
    telegram_app.add_handler(CallbackQueryHandler(admin_actions, pattern="^(approve_|reject_)"))
    telegram_app.add_handler(CallbackQueryHandler(accept_job, pattern="^accept_"))
    telegram_app.add_handler(CallbackQueryHandler(trip_lifecycle, pattern="^(arrived_|starttrip_|endtrip_)"))
    telegram_app.add_handler(CommandHandler("wallet_add", wallet_add_command))

    if RUN_MODE == "webhook":
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram", secret_token=WEBHOOK_SECRET)
    else:
        # Polling mode for local test run
        import asyncio
        asyncio.create_task(telegram_app.run_polling())

@app_fastapi.post("/telegram")
async def webhook_endpoint(request: Request):
    if RUN_MODE == "webhook":
        from telegram import Update
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    return {"status": "ok"}

@app_fastapi.get("/")
def home():
    return {"status": "Bot is active!"}
