import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DRIVER_GROUP_ID = os.getenv("DRIVER_GROUP_ID")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
WALLET_GROUP_ID = os.getenv("WALLET_GROUP_ID")

CAR_TYPE, PACKAGE, LOCATION, PHONE = range(4)

PACKAGES = {
    "pkg_3h": {"label": "3 Hours (City)", "price": "45,000 MMK"},
    "pkg_6h": {"label": "6 Hours (Half Day)", "price": "80,000 MMK"},
    "pkg_10h": {"label": "10 Hours (Full Day)", "price": "120,000 MMK"},
    "pkg_24h": {"label": "24 Hours (1 Day)", "price": "250,000 MMK"}
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("Sedan", callback_data="Sedan"),
         InlineKeyboardButton("SUV", callback_data="SUV")],
        [InlineKeyboardButton("Alphard / VIP", callback_data="Alphard")]
    ]
    await update.message.reply_text(
        "Welcome! Choose a vehicle type:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CAR_TYPE

async def car_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['car_type'] = query.data

    keyboard = [
        [InlineKeyboardButton("3 Hours - 45,000 MMK", callback_data="pkg_3h")],
        [InlineKeyboardButton("6 Hours - 80,000 MMK", callback_data="pkg_6h")],
        [InlineKeyboardButton("10 Hours - 120,000 MMK", callback_data="pkg_10h")],
        [InlineKeyboardButton("24 Hours - 250,000 MMK", callback_data="pkg_24h")]
    ]
    await query.edit_message_text(
        text=f"Selected Vehicle: {query.data}\nSelect an Hourly Package:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PACKAGE

async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pkg_key = query.data
    context.user_data['package'] = PACKAGES[pkg_key]['label']
    context.user_data['price'] = PACKAGES[pkg_key]['price']

    await query.edit_message_text(
        text=f"Package: {PACKAGES[pkg_key]['label']} ({PACKAGES[pkg_key]['price']})\n\nPlease enter your Pickup Location:"
    )
    return LOCATION

async def location_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['location'] = update.message.text
    await update.message.reply_text("Please enter your Phone Number:")
    return PHONE

async def phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['phone'] = update.message.text
    user = update.message.from_user
    
    conn = sqlite3.connect('bookings.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO bookings (user_id, car_type, hourly_package, price, pickup_location, phone) VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, context.user_data['car_type'], context.user_data['package'], context.user_data['price'], context.user_data['location'], context.user_data['phone'])
    )
    booking_id = c.lastrowid
    conn.commit()
    conn.close()

    # Passenger response
    await update.message.reply_text(f"✅ Booking submitted! ID: #{booking_id}\nFinding a driver...")

    # Notify Admin Group
    if ADMIN_GROUP_ID:
        admin_text = (
            f"🔔 **NEW BOOKING #{booking_id}**\n"
            f"User: @{user.username if user.username else 'NoUsername'}\n"
            f"Phone: {context.user_data['phone']}\n"
            f"Car: {context.user_data['car_type']}\n"
            f"Package: {context.user_data['package']}\n"
            f"Pickup: {context.user_data['location']}"
        )
        await context.bot.send_message(chat_id=int(ADMIN_GROUP_ID), text=admin_text, parse_mode="Markdown")

    # Broadcast to Driver Dispatch Group
    if DRIVER_GROUP_ID:
        driver_text = (
            f"🚕 **NEW TRIP REQUEST #{booking_id}**\n\n"
            f"• Vehicle: {context.user_data['car_type']}\n"
            f"• Package: {context.user_data['package']}\n"
            f"• Fare: {context.user_data['price']}\n"
            f"• Location: {context.user_data['location']}"
        )
        keyboard = [[InlineKeyboardButton("✅ Accept Job", callback_data=f"accept_{booking_id}")]]
        await context.bot.send_message(
            chat_id=int(DRIVER_GROUP_ID),
            text=driver_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return ConversationHandler.END

async def accept_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    driver = query.from_user
    booking_id = query.data.split("_")[1]

    conn = sqlite3.connect('bookings.db')
    c = conn.cursor()
    c.execute("SELECT user_id, status, car_type, phone FROM bookings WHERE id = ?", (booking_id,))
    booking = c.fetchone()

    if not booking:
        await query.answer("Booking not found.", show_alert=True)
        conn.close()
        return

    customer_id, status, car_type, phone = booking

    if status != 'PENDING':
        await query.answer("This job was already taken by another driver!", show_alert=True)
        conn.close()
        return

    driver_handle = f"@{driver.username}" if driver.username else driver.first_name
    c.execute(
        "UPDATE bookings SET status = 'ACCEPTED', driver_name = ? WHERE id = ?",
        (driver_handle, booking_id)
    )
    conn.commit()
    conn.close()

    await query.answer("Job accepted successfully!")
    
    # Update Driver Dispatch Group
    await query.edit_message_text(
        text=f"✅ **JOB #{booking_id} TAKEN**\nDriver: {driver_handle}",
        parse_mode="Markdown"
    )

    # Notify Customer
    await context.bot.send_message(
        chat_id=customer_id,
        text=f"🚖 **Driver Found!**\nYour booking #{booking_id} was accepted by driver {driver_handle}. They will call you soon."
    )

    # Notify Admin Group
    if ADMIN_GROUP_ID:
        await context.bot.send_message(
            chat_id=int(ADMIN_GROUP_ID),
            text=f"ℹ️ Booking #{booking_id} accepted by driver {driver_handle}. Customer Phone: {phone}"
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Booking cancelled.")
    return ConversationHandler.END

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("book", start)],
        states={
            CAR_TYPE: [CallbackQueryHandler(car_selected)],
            PACKAGE: [CallbackQueryHandler(package_selected)],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, location_received)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(accept_booking, pattern="^accept_"))
    
    app.run_polling()

import os
import threading
from flask import Flask

# Add mini web server for Render Free Tier
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Taxi Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Start web server in background
    threading.Thread(target=run_web, daemon=True).start()

    # Start Telegram Bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("book", start)],
        states={
            CAR_TYPE: [CallbackQueryHandler(car_selected)],
            PACKAGE: [CallbackQueryHandler(package_selected)],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, location_received)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(accept_booking, pattern="^accept_"))
    
    app.run_polling()
