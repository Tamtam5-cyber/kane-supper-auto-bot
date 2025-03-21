from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
from pyrogram import Client
from pyrogram.types import User
from config import API_ID, API_HASH, BOT_TOKEN, SOURCE_IDS, TARGET_IDS, BLACKLIST, WHITELIST

# Khởi tạo Pyrogram Client
app = Client("userbot", api_id=API_ID, api_hash=API_HASH)

# Kiểm tra danh sách đen/trắng
def is_allowed(chat_id):
    if chat_id in BLACKLIST:
        return False
    if WHITELIST and chat_id not in WHITELIST:
        return False
    return True

# Chuyển tiếp tin nhắn từ nguồn đến mục tiêu
@app.on_message()
async def forward_messages(client, message):
    if message.chat.id in SOURCE_IDS and is_allowed(message.chat.id):
        for target in TARGET_IDS:
            try:
                await message.forward(target)
            except Exception as e:
                print(f"Lỗi chuyển tiếp đến {target}: {e}")

# Lấy danh sách thành viên trong nhóm/kênh
async def get_group_members(chat_id):
    members = []
    async with app:
        async for member in app.get_chat_members(chat_id):
            if isinstance(member.user, User):
                members.append(f"{member.user.first_name} ({member.user.id})")
    return members

# Xử lý lệnh lấy danh sách thành viên
def list_members(update: Update, context: CallbackContext):
    chat_id = update.message.text.split()[-1]
    try:
        chat_id = int(chat_id)
    except ValueError:
        update.message.reply_text("❌ Vui lòng nhập ID nhóm/kênh hợp lệ.")
        return

    update.message.reply_text("🔍 Đang lấy danh sách thành viên...")
    members = app.run(get_group_members(chat_id))
    
    if members:
        update.message.reply_text("\n".join(members[:50]))  # Giới hạn 50 dòng để tránh spam
    else:
        update.message.reply_text("⚠️ Không tìm thấy thành viên hoặc bạn không có quyền truy cập.")

# Menu chính
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📤 Quản lý chuyển tiếp", callback_data="manage_forward")],
        [InlineKeyboardButton("🚫 Danh sách đen", callback_data="blacklist")],
        [InlineKeyboardButton("✅ Danh sách trắng", callback_data="whitelist")],
        [InlineKeyboardButton("👥 Thành viên nhóm", callback_data="group_members")],
        [InlineKeyboardButton("⚙️ Cài đặt", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Xử lý menu
def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    if query.data == "blacklist":
        query.edit_message_text(f"🚫 Danh sách đen:\n{BLACKLIST}\n\nGõ `/add_blacklist ID` để thêm, `/remove_blacklist ID` để xóa.")
    elif query.data == "whitelist":
        query.edit_message_text(f"✅ Danh sách trắng:\n{WHITELIST}\n\nGõ `/add_whitelist ID` để thêm, `/remove_whitelist ID` để xóa.")
    elif query.data == "group_members":
        query.edit_message_text("📌 Gõ `/list_members ID_NHÓM` để xem danh sách thành viên.")

# Chạy bot
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", lambda update, context: update.message.reply_text("📌 Chọn một chức năng:", reply_markup=main_menu())))
    dp.add_handler(CommandHandler("list_members", list_members))
    dp.add_handler(CallbackQueryHandler(button_handler))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
