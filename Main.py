import asyncio
import os
import psycopg2
import pandas as pd
import zipfile
import shutil
import random
import time
from telethon.sync import TelegramClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Cấu hình bot từ biến môi trường
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")  # URL của PostgreSQL trên Railway

# Kết nối với PostgreSQL
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    # Bảng người dùng
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            phone TEXT,
            is_logged_in BOOLEAN DEFAULT FALSE,
            is_admin BOOLEAN DEFAULT FALSE
        )
    """)
    # Bảng cấu hình người dùng
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_configs (
            chat_id BIGINT PRIMARY KEY,
            delay FLOAT DEFAULT 30.0,
            replay BOOLEAN DEFAULT TRUE,
            delay_replay FLOAT DEFAULT 60.0,
            is_spamming BOOLEAN DEFAULT FALSE,
            forwarding BOOLEAN DEFAULT FALSE,
            source_chat BIGINT,
            dest_chats TEXT
        )
    """)
    # Bảng số đã spam
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS spammed_numbers (
            chat_id BIGINT,
            phone TEXT,
            PRIMARY KEY (chat_id, phone)
        )
    """)
    # Bảng lưu OTP
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS otp_verifications (
            chat_id BIGINT PRIMARY KEY,
            phone TEXT,
            otp_code TEXT,
            expiry_time FLOAT
        )
    """)
    # Thêm admin mặc định nếu chưa có
    cursor.execute("SELECT 1 FROM users WHERE is_admin = TRUE")
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (chat_id, phone, is_logged_in, is_admin) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (0, "default_admin", False, True)
        )
    conn.commit()
    return conn

# Lưu số đã spam
def save_spammed_number(chat_id, phone, conn):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO spammed_numbers (chat_id, phone) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (chat_id, phone)
    )
    conn.commit()

# Kiểm tra số đã spam
def is_number_spammed(chat_id, phone, conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM spammed_numbers WHERE chat_id = %s AND phone = %s",
        (chat_id, phone)
    )
    return cursor.fetchone() is not None

# Lấy hoặc tạo cấu hình người dùng
def get_user_config(chat_id, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_configs WHERE chat_id = %s", (chat_id,))
    result = cursor.fetchone()
    if not result:
        cursor.execute(
            "INSERT INTO user_configs (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,)
        )
        conn.commit()
        return {
            "delay": 30.0,
            "replay": True,
            "delay_replay": 60.0,
            "is_spamming": False,
            "forwarding": False,
            "source_chat": None,
            "dest_chats": []
        }
    return {
        "delay": result[1],
        "replay": result[2],
        "delay_replay": result[3],
        "is_spamming": result[4],
        "forwarding": result[5],
        "source_chat": result[6],
        "dest_chats": result[7].split(",") if result[7] else []
    }

# Lưu cấu hình người dùng
def save_user_config(chat_id, config, conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO user_configs (chat_id, delay, replay, delay_replay, is_spamming, forwarding, source_chat, dest_chats)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
        delay = EXCLUDED.delay,
        replay = EXCLUDED.replay,
        delay_replay = EXCLUDED.delay_replay,
        is_spamming = EXCLUDED.is_spamming,
        forwarding = EXCLUDED.forwarding,
        source_chat = EXCLUDED.source_chat,
        dest_chats = EXCLUDED.dest_chats
        """,
        (
            chat_id,
            config["delay"],
            config["replay"],
            config["delay_replay"],
            config["is_spamming"],
            config["forwarding"],
            config["source_chat"],
            ",".join(map(str, config["dest_chats"])) if config["dest_chats"] else None
        )
    )
    conn.commit()

# Kiểm tra đăng nhập
def is_user_logged_in(chat_id, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT is_logged_in FROM users WHERE chat_id = %s", (chat_id,))
    result = cursor.fetchone()
    return result and result[0]

# Kiểm tra quyền admin
def is_user_admin(chat_id, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT is_admin FROM users WHERE chat_id = %s", (chat_id,))
    result = cursor.fetchone()
    return result and result[0]

# Lấy số điện thoại của người dùng
def get_user_phone(chat_id, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM users WHERE chat_id = %s", (chat_id,))
    result = cursor.fetchone()
    return result[0] if result else None

# Tạo mã OTP
def generate_otp():
    return str(random.randint(100000, 999999))

# Lưu mã OTP
def save_otp(chat_id, phone, otp_code, conn):
    expiry_time = time.time() + 300  # Hết hạn sau 5 phút
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO otp_verifications (chat_id, phone, otp_code, expiry_time) VALUES (%s, %s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET phone = %s, otp_code = %s, expiry_time = %s",
        (chat_id, phone, otp_code, expiry_time, phone, otp_code, expiry_time)
    )
    conn.commit()

# Xác thực OTP
def verify_otp(chat_id, otp_code, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT phone, otp_code, expiry_time FROM otp_verifications WHERE chat_id = %s", (chat_id,))
    result = cursor.fetchone()
    if not result:
        return None, "Không tìm thấy mã OTP. Vui lòng đăng nhập lại bằng /dangnhap phone_number"
    
    phone, stored_otp, expiry_time = result
    if time.time() > expiry_time:
        cursor.execute("DELETE FROM otp_verifications WHERE chat_id = %s", (chat_id,))
        conn.commit()
        return None, "Mã OTP đã hết hạn. Vui lòng đăng nhập lại bằng /dangnhap phone_number"
    
    if otp_code != stored_otp:
        return None, "Mã OTP không đúng!"
    
    cursor.execute("DELETE FROM otp_verifications WHERE chat_id = %s", (chat_id,))
    conn.commit()
    return phone, None

# Khởi tạo database
conn = init_db()

# Quản lý Telethon clients
telethon_clients = {}

# Danh sách các nhóm mà bot đã tham gia (giả lập)
joined_groups = [-100123456789]  # Thay bằng ID nhóm thực tế

# Hàm khởi động bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_logged_in(chat_id, conn):
        await update.message.reply_text(
            "Chào mừng đến với bot của bạn! Sử dụng:\n"
            "/dangnhap phone_number - Đăng nhập bằng số điện thoại và OTP\n"
            "/themsession - Đăng nhập bằng file session.zip\n"
            "/xacthuc otp_code - Xác thực OTP\n"
            "/forward - Thiết lập chuyển tiếp\n"
            "/startspam - Bắt đầu spam vào nhóm\n"
            "/spamcontacts - Spam vào danh bạ\n"
            "/spamfile - Spam từ file số điện thoại\n"
            "/listusers - Xem danh sách người dùng (admin)\n"
            "/makeadmin - Cấp quyền admin (admin)\n"
            "/removeuser - Xóa người dùng (admin)"
        )
    else:
        await update.message.reply_text(
            "Vui lòng đăng nhập để sử dụng bot:\n"
            "/dangnhap phone_number - Đăng nhập bằng số điện thoại và OTP\n"
            "/themsession - Đăng nhập bằng file session.zip"
        )

# Hình thức 1: Đăng nhập bằng số điện thoại và OTP
async def dangnhap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_logged_in(chat_id, conn):
        await update.message.reply_text("Bạn đã đăng nhập rồi!")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Vui lòng nhập: /dangnhap phone_number")
        return
    
    phone = context.args[0]
    otp_code = generate_otp()
    save_otp(chat_id, phone, otp_code, conn)
    
    # Gửi mã OTP qua Telegram (có thể thay bằng SMS nếu có API)
    await update.message.reply_text(f"Mã OTP của bạn là: {otp_code}\nVui lòng xác thực bằng lệnh /xacthuc otp_code trong vòng 5 phút.")

# Xác thực OTP
async def xacthuc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_logged_in(chat_id, conn):
        await update.message.reply_text("Bạn đã đăng nhập rồi!")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Vui lòng nhập: /xacthuc otp_code")
        return
    
    otp_code = context.args[0]
    phone, error = verify_otp(chat_id, otp_code, conn)
    if error:
        await update.message.reply_text(error)
        return
    
    # Tạo session mới bằng Telethon
    session_dir = f"sessions/{chat_id}"
    os.makedirs(session_dir, exist_ok=True)
    session_file = f"{session_dir}/session"
    
    client = TelegramClient(session_file, int(API_ID), API_HASH)
    try:
        await client.start(phone=phone)
        telethon_clients[chat_id] = client
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (chat_id, phone, is_logged_in) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET phone = %s, is_logged_in = %s",
            (chat_id, phone, True, phone, True)
        )
        conn.commit()
        await update.message.reply_text("Đăng nhập thành công! Bạn có thể sử dụng bot.")
    except Exception as e:
        await update.message.reply_text(f"Đăng nhập thất bại: {str(e)}")

# Hình thức 2: Đăng nhập bằng file session.zip
async def themsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_user_logged_in(chat_id, conn):
        await update.message.reply_text("Bạn đã đăng nhập rồi!")
        return
    
    context.user_data["awaiting_session"] = True
    await update.message.reply_text("Vui lòng gửi file session.zip để đăng nhập.")

# Hàm xử lý file session
async def handle_session_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.user_data.get("awaiting_session"):
        return
    
    context.user_data["awaiting_session"] = False
    
    # Tạo thư mục lưu session cho người dùng
    session_dir = f"sessions/{chat_id}"
    os.makedirs(session_dir, exist_ok=True)
    
    # Tải và giải nén file session.zip
    file = await update.message.document.get_file()
    zip_path = f"{session_dir}/session.zip"
    await file.download_to_drive(zip_path)
    
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(session_dir)
    
    # Tìm file session (giả sử file session có đuôi .session)
    session_file = None
    for file_name in os.listdir(session_dir):
        if file_name.endswith(".session"):
            session_file = os.path.join(session_dir, file_name)
            break
    
    if not session_file:
        await update.message.reply_text("Không tìm thấy file session trong session.zip!")
        return
    
    # Khởi tạo Telethon client
    client = TelegramClient(session_file, int(API_ID), API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await update.message.reply_text("Session không hợp lệ! Vui lòng thử lại.")
            return
        
        phone = (await client.get_me()).phone
        telethon_clients[chat_id] = client
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (chat_id, phone, is_logged_in) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET phone = %s, is_logged_in = %s",
            (chat_id, phone, True, phone, True)
        )
        conn.commit()
        await update.message.reply_text("Đăng nhập thành công! Bạn có thể sử dụng bot.")
    except Exception as e:
        await update.message.reply_text(f"Đăng nhập thất bại: {str(e)}")

# Hàm hiển thị danh sách người dùng (chỉ admin)
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_admin(chat_id, conn):
        await update.message.reply_text("Chỉ admin mới có thể sử dụng lệnh này!")
        return
    
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, phone, is_admin FROM users")
    users = cursor.fetchall()
    
    if not users:
        await update.message.reply_text("Không có người dùng nào.")
        return
    
    user_list = "Danh sách người dùng:\n"
    for user in users:
        user_list += f"Chat ID: {user[0]}, Phone: {user[1]}, Admin: {'Yes' if user[2] else 'No'}\n"
    await update.message.reply_text(user_list)

# Hàm cấp quyền admin (chỉ admin)
async def make_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_admin(chat_id, conn):
        await update.message.reply_text("Chỉ admin mới có thể sử dụng lệnh này!")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Vui lòng nhập: /makeadmin chat_id")
        return
    
    target_chat_id = int(context.args[0])
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_admin = TRUE WHERE chat_id = %s", (target_chat_id,))
    if cursor.rowcount == 0:
        await update.message.reply_text("Không tìm thấy người dùng!")
    else:
        conn.commit()
        await update.message.reply_text(f"Đã cấp quyền admin cho chat ID {target_chat_id}")

# Hàm xóa người dùng (chỉ admin)
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_admin(chat_id, conn):
        await update.message.reply_text("Chỉ admin mới có thể sử dụng lệnh này!")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Vui lòng nhập: /removeuser chat_id")
        return
    
    target_chat_id = int(context.args[0])
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE chat_id = %s", (target_chat_id,))
    cursor.execute("DELETE FROM user_configs WHERE chat_id = %s", (target_chat_id,))
    cursor.execute("DELETE FROM spammed_numbers WHERE chat_id = %s", (target_chat_id,))
    if cursor.rowcount == 0:
        await update.message.reply_text("Không tìm thấy người dùng!")
    else:
        conn.commit()
        # Xóa thư mục session của người dùng
        session_dir = f"sessions/{target_chat_id}"
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir)
        if target_chat_id in telethon_clients:
            del telethon_clients[target_chat_id]
        await update.message.reply_text(f"Đã xóa người dùng với chat ID {target_chat_id}")

# Hàm hiển thị cấu hình hiện tại
async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id, conn)
    
    keyboard = [
        [InlineKeyboardButton("Stop Spam", callback_data="stop_spam")],
        [InlineKeyboardButton("DELAY", callback_data="set_delay")],
        [InlineKeyboardButton("REPLAY", callback_data="toggle_replay")],
        [InlineKeyboardButton("DELAY REPLAY", callback_data="set_delay_replay")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    config_text = (
        f"Your Config\n"
        f"DELAY: {config['delay']} seconds\n"
        f"Replay: {'YES' if config['replay'] else 'NO'}\n"
        f"Delay for replay: {config['delay_replay']} seconds\n\n"
        f"Note:\n"
        f"- Set DELAY: Customize the delay between each message.\n"
        f"- Replay: Automatically repeat the cycle when set to YES.\n"
        f"- Set DELAY Replay: Control the delay between each replay cycle"
    )
    await update.message.reply_text(config_text, reply_markup=reply_markup)

# Hàm thiết lập chuyển tiếp
async def forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_logged_in(chat_id, conn):
        await update.message.reply_text("Vui lòng đăng nhập trước! Sử dụng /dangnhap phone_number hoặc /themsession")
        return
    
    config = get_user_config(chat_id, conn)
    config["forwarding"] = True
    config["source_chat"] = -100123456789  # Thay bằng ID nguồn thực tế
    config["dest_chats"] = [-100987654321]  # Thay bằng ID đích thực tế
    save_user_config(chat_id, config, conn)
    await update.message.reply_text("Đã thiết lập chuyển tiếp. Bot sẽ chuyển tiếp tin nhắn từ nguồn đến đích.")

# Hàm xử lý tin nhắn mới để chuyển tiếp
async def handle_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id, conn)
    if not config["forwarding"]:
        return
    
    message = update.message
    if message.chat_id == config["source_chat"]:
        for dest_chat in config["dest_chats"]:
            await context.bot.forward_message(
                chat_id=dest_chat,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            await asyncio.sleep(config["delay"])

# Hàm đọc số điện thoại từ file CSV
def read_phone_numbers_from_file(file_path):
    try:
        df = pd.read_csv(file_path)
        if "phone" not in df.columns:
            return []
        return df["phone"].astype(str).tolist()
    except Exception as e:
        print(f"Error reading file: {e}")
        return []

# Hàm lấy số điện thoại từ danh bạ Telegram
async def get_contacts(chat_id):
    if chat_id not in telethon_clients:
        phone = get_user_phone(chat_id, conn)
        session_file = f"sessions/{chat_id}/session"
        telethon_clients[chat_id] = TelegramClient(session_file, int(API_ID), API_HASH)
        await telethon_clients[chat_id].start(phone=phone)
    
    client = telethon_clients[chat_id]
    contacts = await client.get_contacts()
    phone_numbers = [contact.phone_number for contact in contacts if contact.phone_number]
    return phone_numbers

# Hàm spam vào danh bạ
async def spam_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_user_logged_in(chat_id, conn):
        await update.message.reply_text("Vui lòng đăng nhập trước! Sử dụng /dangnhap phone_number hoặc /themsession")
        return
    
    config = get_user_config(chat_id, conn)
    if config["is_spamming"]:
        await update.message.reply_text("Bot đang spam rồi! Sử dụng nút Stop Spam để dừng.")
        return
    
    config["is_spamming"] = True
    save_user_config(chat_id, config, conn)
    await update.message.reply_text("Đang lấy danh bạ và bắt đầu spam...")
    
    # Lấy số điện thoại từ danh bạ
    phone_numbers = await get_contacts(chat_id)
    
    # Hiển thị cấu hình và nút điều khiển
    await show_config(update, context)
    
    # Vòng lặp spam
    while config["is_spamming"]:
        for phone in phone_numbers:
            config = get_user_config(chat_id, conn)
            if not config["is_spamming"]:
                break
            if is_number_spammed(chat_id, phone, conn):
                continue  # Bỏ qua số đã spam
            
            try:
                # Tìm user ID từ số điện thoại
                client = telethon_clients[chat_id]
                contact = await client.get_entity(phone)
                await context.bot.send_message(chat_id=contact.id, text="Đây là tin nhắn spam!")
                save_spammed_number(chat_id, phone, conn)  # Đán
