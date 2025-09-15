import os
import re
import uuid
import shutil
import zipfile
import hashlib
import requests
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import asyncio
import secrets
import math
import psycopg
import PyPDF2
import openpyxl
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Configuration
BOT_TOKEN = "8349936038:AAHf1YMdV1Vq6wtWy99PxzJAConTMy6so7U"
DATABASE_URL = "postgresql://postgres:root@localhost:5432/telegram_bot_db"
TEMP_DIR = "temp_uploads"
BASE_STORAGE_PATH = r"C:\_BMC Project\Survey Data"
NETWORK_STORAGE_PATH = r"\\synology\ENGINEERS\_BMC Project\Survey Data"
DGPS_DATA_PATH = "DGPS Data"
VIRUSTOTAL_API_KEY = "YOUR_VIRUSTOTAL_API_KEY"  # Optional
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
MAX_FILES_PER_SESSION = 10  # Increased from 4 to allow more flexibility

# Role definitions
ROLE_EMPLOYEE = 0
ROLE_MANAGER = 1
ROLE_ADMIN = 2

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.init_database()
    
    def get_connection(self):
        return psycopg.connect(self.db_url)
    
    def init_database(self):
        """Create necessary tables"""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # Users table with roles and manager relationship
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT UNIQUE NOT NULL,
                        username VARCHAR(255),
                        per_username VARCHAR(255),
                        first_name VARCHAR(255),
                        role INTEGER DEFAULT 0,
                        manager_id BIGINT REFERENCES users(telegram_id),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # User invitations table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_invitations (
                        id SERIAL PRIMARY KEY,
                        invitation_code UUID UNIQUE NOT NULL,
                        manager_id BIGINT REFERENCES users(telegram_id),
                        status VARCHAR(50) DEFAULT 'pending',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP WITH TIME ZONE,
                        used_at TIMESTAMP WITH TIME ZONE
                    )
                """)
                
                # User requests table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_requests (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(telegram_id),
                        manager_id BIGINT REFERENCES users(telegram_id),
                        status VARCHAR(50) DEFAULT 'pending',
                        rejection_reason TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE
                    )
                """)
                
                # Upload sessions table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS upload_sessions (
                        id SERIAL PRIMARY KEY,
                        session_id UUID UNIQUE NOT NULL,
                        user_id BIGINT REFERENCES users(telegram_id),
                        status VARCHAR(50) DEFAULT 'pending',
                        temp_path VARCHAR(500),
                        final_path VARCHAR(500),
                        network_path VARCHAR(500),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP WITH TIME ZONE
                    )
                """)
                
                # Files table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS files (
                        id SERIAL PRIMARY KEY,
                        session_id UUID REFERENCES upload_sessions(session_id),
                        original_name VARCHAR(500),
                        stored_name VARCHAR(500),
                        file_size BIGINT,
                        file_hash VARCHAR(64),
                        validation_status VARCHAR(50),
                        validation_errors TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # History table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS history (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(telegram_id),
                        file_id BIGINT REFERENCES files(id),
                        session_id UUID REFERENCES upload_sessions(session_id),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                conn.commit()
    
    def create_user(self, telegram_id: int, username: str, first_name: str, role: int = ROLE_EMPLOYEE, manager_id: int = None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (telegram_id, username, first_name, role, manager_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        role = EXCLUDED.role,
                        manager_id = EXCLUDED.manager_id
                """, (telegram_id, username, first_name, role, manager_id))
                conn.commit()
    
    def create_invitation(self, manager_id: int) -> str:
        """Return a single reusable invitation code per manager.
        If one already exists and is active, reuse it; otherwise create a new, non-expiring, active code.
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # Try to reuse existing active invitation for this manager
                cur.execute("""
                    SELECT invitation_code
                    FROM user_invitations
                    WHERE manager_id = %s AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (manager_id,))
                existing = cur.fetchone()
                if existing and existing[0]:
                    return str(existing[0])

                # Create a new reusable, non-expiring code
                invitation_code = str(uuid.uuid4())
                cur.execute("""
                    INSERT INTO user_invitations (invitation_code, manager_id, status, expires_at)
                    VALUES (%s, %s, 'active', NULL)
                """, (invitation_code, manager_id))
                conn.commit()
                return invitation_code
    
    def get_user_role(self, telegram_id: int) -> int:
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role FROM users WHERE telegram_id = %s", (telegram_id,))
                result = cur.fetchone()
                return int(result[0]) if result else ROLE_EMPLOYEE
    
    def get_manager_users(self, manager_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT * FROM users 
                    WHERE manager_id = %s
                """, (manager_id,))
                return cur.fetchall()
    
    def get_pending_requests(self, manager_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT ur.*, u.username, u.first_name 
                    FROM user_requests ur
                    JOIN users u ON ur.user_id = u.telegram_id
                    WHERE ur.manager_id = %s AND ur.status = 'pending'
                """, (manager_id,))
                return cur.fetchall()
    
    def handle_user_request(self, request_id: int, status: str, rejection_reason: str = None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                if status == 'approved':
                    cur.execute("""
                        UPDATE user_requests 
                        SET status = 'approved', updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (request_id,))
                else:
                    cur.execute("""
                        UPDATE user_requests 
                        SET status = 'rejected', rejection_reason = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (rejection_reason, request_id))
                conn.commit()
    
    def create_session(self, user_id: int) -> str:
        session_id = str(uuid.uuid4())
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO upload_sessions (session_id, user_id, temp_path)
                    VALUES (%s, %s, %s)
                """, (session_id, user_id, os.path.join(TEMP_DIR, session_id)))
                conn.commit()
        return session_id
    
    def update_session_status(self, session_id: str, status: str, final_path: str = None, network_path: str = None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE upload_sessions 
                    SET status = %s, final_path = %s, network_path = %s, completed_at = CURRENT_TIMESTAMP
                    WHERE session_id = %s
                """, (status, final_path, network_path, session_id))
                conn.commit()
    
    def log_file(self, session_id: str, original_name: str, stored_name: str, 
                 file_size: int, file_hash: str, validation_status: str, errors: str = None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # First insert into files table
                cur.execute("""
                    INSERT INTO files (session_id, original_name, stored_name, file_size, 
                                     file_hash, validation_status, validation_errors)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (session_id, original_name, stored_name, file_size, file_hash, validation_status, errors))
                
                file_id = cur.fetchone()[0]
                
                # Get user_id from session
                cur.execute("""
                    SELECT user_id FROM upload_sessions WHERE session_id = %s
                """, (session_id,))
                user_id = cur.fetchone()[0]
                
                # Insert into history table with BIGINT file_id
                cur.execute("""
                    INSERT INTO history (user_id, file_id, session_id)
                    VALUES (%s, %s, %s)
                """, (user_id, int(file_id), session_id))
                
                conn.commit()

class FileValidator:
    # Removed ALLOWED_EXTENSIONS as we now allow all extensions except video files
    VIDEO_EXTENSIONS = {
        # Video formats
        'mp4', 'avi', 'mov', 'wmv', 'flv', 'mkv', 'webm', 'mpeg', 'mpg', 'm4v'
    }

    DANGEROUS_EXTENSIONS = {
        # Executables
        'exe', 'msi', 'bat', 'cmd', 'ps1', 'vbs', 'js',
        # Scripts
        'py', 'php', 'rb', 'pl', 'sh',
        # Other potentially dangerous
        'dll', 'sys', 'drv', 'bin'
    }

    @staticmethod
    def get_file_type(filename: str) -> str:
        """Get file type category"""
        ext = Path(filename).suffix.lower().lstrip('.')
        if ext in ['csv', 'xlsx', 'xls']:
            return 'spreadsheet'
        elif ext == 'pdf':
            return 'document'
        elif ext in ['jpg', 'jpeg', 'png', 'gif']:
            return 'image'
        elif ext in FileValidator.VIDEO_EXTENSIONS:
            return 'video'
        else:
            return 'other'

    @staticmethod
    def validate_extension(filename: str) -> tuple[bool, str]:
        ext = Path(filename).suffix.lower().lstrip('.')
        if ext in FileValidator.DANGEROUS_EXTENSIONS:
            return False, f"❌ Dangerous file type detected: {ext}"
        if ext in FileValidator.VIDEO_EXTENSIONS:
            return False, f"❌ Video file extension {ext} not allowed."
        return True, ""

    @staticmethod
    def validate_file_size(file_path: str) -> tuple[bool, str]:
        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE:
            return False, f"❌ File too large: {size / (1024*1024):.1f}MB (max: {MAX_FILE_SIZE / (1024*1024):.1f}MB)"
        return True, ""

    @staticmethod
    async def scan_with_virustotal(file_path: str, api_key: str) -> tuple[bool, str]:
        """Scan file with VirusTotal"""
        if not api_key or api_key == "YOUR_VIRUSTOTAL_API_KEY":
            return True, "VirusTotal scan skipped (no API key)"
        
        try:
            # Calculate file hash
            with open(file_path, 'rb') as f:
                file_hash = hashlib.sha256(f.read()).hexdigest()
            
            # Query VirusTotal
            url = f"https://www.virustotal.com/vtapi/v2/file/report"
            params = {'apikey': api_key, 'resource': file_hash}
            
            response = requests.get(url, params=params, timeout=10)
            result = response.json()
            
            if result.get('response_code') == 1:  # File found in VT database
                positives = result.get('positives', 0)
                if positives > 0:
                    return False, f"⚠️ VirusTotal detected {positives} threats in file"
            
            return True, "✅ VirusTotal scan passed"
            
        except Exception as e:
            logger.warning(f"VirusTotal scan failed: {e}")
            return True, "VirusTotal scan failed, proceeding anyway"

    @staticmethod
    def validate_required_files(uploaded_files: List[str]) -> tuple[bool, str]:
        """Validate that required file types are present in the upload"""
        if not uploaded_files:
            return False, "No files uploaded"
        
        # For now, we don't have specific required file types
        # This method can be extended later to check for specific required files
        # For example: PDF, Excel, etc.
        
        return True, "All required files present"

class TelegramBot:
    def __init__(self):
        self.db = DatabaseManager(DATABASE_URL)
        self.user_sessions = {}  # Store active sessions per user
        self.upload_timers = {}  # Store timers for upload grouping
        self.pending_uploads = {}  # Store pending uploads for grouping
        
        # Progress bar characters
        self.PROGRESS_FILLED = "█"
        self.PROGRESS_EMPTY = "░"
        self.PROGRESS_WIDTH = 10  # Width of the progress bar
        
        # Ensure directories exist
        os.makedirs(TEMP_DIR, exist_ok=True)
    
    def formatFileSize(self, bytes):
        """Format file size in human readable format"""
        if bytes == 0:
            return '0 Bytes'
        k = 1024
        sizes = ['Bytes', 'KB', 'MB', 'GB']
        i = math.floor(math.log(bytes) / math.log(k))
        return f"{bytes / math.pow(k, i):.1f} {sizes[i]}"
    
    def escape_markdown_v2(self, text: str) -> str:
        """Escape special characters for MarkdownV2"""
        # List of characters that need to be escaped in MarkdownV2
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    def get_storage_paths(self, user_id: int) -> tuple[str, str]:
        """Generate storage paths based on current date and user name for both local and network storage"""
        now = datetime.now()
        year = str(now.year)
        month = now.strftime("%B")
        date = now.strftime("%d.%m.%Y")
        
        # Get user's name from database
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT first_name FROM users WHERE telegram_id = %s", (user_id,))
                result = cur.fetchone()
                user_name = result[0] if result else "Unknown_User"
        
        # Create path components
        path_components = [year, month, date, DGPS_DATA_PATH, user_name]
        
        # Generate local path
        local_path = os.path.join(BASE_STORAGE_PATH, *path_components)
        os.makedirs(local_path, exist_ok=True)
        
        # Generate network path
        network_path = os.path.join(NETWORK_STORAGE_PATH, *path_components)
        os.makedirs(network_path, exist_ok=True)
        
        return local_path, network_path

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        user = update.effective_user
        
        # Check if this is an invitation link
        if context.args and context.args[0]:
            await self.handle_user_request(update, context)
            return
        
        # Check if user is approved
        with self.db.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT u.*, ur.status as request_status
                    FROM users u
                    LEFT JOIN user_requests ur ON u.telegram_id = ur.user_id
                    WHERE u.telegram_id = %s
                    ORDER BY ur.created_at DESC
                    LIMIT 1
                """, (user.id,))
                result = cur.fetchone()
        
        if not result:
            # New user
            self.db.create_user(user.id, user.username, user.first_name)
            await update.message.reply_text(
                "Welcome! Please ask your manager for an invitation link to start using the bot."
            )
            return
        
        if result['request_status'] == 'pending':
            await update.message.reply_text(
                "⏳ Your registration request is still pending approval.\n"
                "Please wait for your manager to approve your request."
            )
            return
        
        if result['request_status'] == 'rejected':
            await update.message.reply_text(
                "❌ Your registration request was rejected.\n"
                "Please contact your manager for a new invitation link."
            )
            return
        
        # User is approved, show welcome message
        role = result['role']
        
        welcome_text = f"""
Document Upload Bot

Upload your documents and I'll validate them according to company standards!

Supported formats: Any file type except video formats (MP4, AVI, MOV, etc.)
Max files per session: {MAX_FILES_PER_SESSION}
Max file size: {MAX_FILE_SIZE / (1024*1024):.0f}MB

You can upload any number of files from 1 to {MAX_FILES_PER_SESSION} files per session.
        """
        
        # Add role-specific commands
        if role == ROLE_MANAGER:
            welcome_text += "\n\nManager Commands:\n/upload - Upload files\n/manage_users - Manage users"
        elif role == ROLE_ADMIN:
            welcome_text += "\n\nManager Commands:\n/upload - Upload files\n/manage_users - Manage users\n/history - View upload history"
        else:
            welcome_text += "\n\nCommands:\n/upload - Upload files\n/history - View upload history"
        
        await update.message.reply_text(welcome_text)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help information"""
        user = update.effective_user
        
        # Check if user has access
        if not await self.check_user_access(user.id):
            await update.message.reply_text(
                "❌ You don't have access to use this command.\n"
                "Please wait for your manager's approval or contact them for a new invitation link."
            )
            return
        
        help_text = f"""
Validation Rules:

Extensions: All file types allowed except video formats (MP4, AVI, MOV, etc.)
File size: Maximum {MAX_FILE_SIZE / (1024*1024):.0f}MB per file
Files per session: 1 to {MAX_FILES_PER_SESSION} files
Security: Files scanned for threats
Structure: Proper folder organization

Commands:
/start - Welcome message
/upload - Start new upload session
/history - View upload history
/help - Show this help
        """
        
        await update.message.reply_text(help_text)
    
    def generate_progress_bar(self, current: int, total: int) -> str:
        """Generate a visual progress bar"""
        if total == 0:
            return f"[{self.PROGRESS_EMPTY * self.PROGRESS_WIDTH}] 0%"
        
        percentage = (current / total) * 100
        filled_width = int((current / total) * self.PROGRESS_WIDTH)
        empty_width = self.PROGRESS_WIDTH - filled_width
        
        progress_bar = f"[{self.PROGRESS_FILLED * filled_width}{self.PROGRESS_EMPTY * empty_width}]"
        return f"{progress_bar} {percentage:.0f}%"

    async def upload_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start file upload process"""
        user = update.effective_user
        
        # Check if user has access
        if not await self.check_user_access(user.id):
            await update.message.reply_text(
                "❌ You don't have access to use this command.\n"
                "Please wait for your manager's approval or contact them for a new invitation link."
            )
            return
        
        # Rest of the upload_command code...
        user_id = update.effective_user.id
        
        # Create new session
        session_id = self.db.create_session(user_id)
        self.user_sessions[user_id] = {
            'session_id': session_id,
            'files_uploaded': 0,
            'temp_path': os.path.join(TEMP_DIR, session_id),
            'uploaded_files': []
        }
        
        # Create temp directory
        os.makedirs(self.user_sessions[user_id]['temp_path'], exist_ok=True)
        
        keyboard = [
            [InlineKeyboardButton("📁 Upload Files", callback_data="ready_upload")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_session")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Generate initial progress bar
        progress_bar = self.generate_progress_bar(0, MAX_FILES_PER_SESSION)
        
        await update.message.reply_text(
            f"Please upload your files here\n\n"
            f"You can upload 1 to {MAX_FILES_PER_SESSION} files\n\n"
            f"Send all files at once or send one by one, We are ready to receive your files!",
            reply_markup=reply_markup
        )
    
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle uploaded documents"""
        user_id = update.effective_user.id
        
        if user_id not in self.user_sessions:
            await update.message.reply_text("No active session. Use /upload to start.")
            return
        
        session = self.user_sessions[user_id]
        
        if session['files_uploaded'] >= MAX_FILES_PER_SESSION:
            await update.message.reply_text(f"Maximum file limit reached ({MAX_FILES_PER_SESSION}/{MAX_FILES_PER_SESSION}). Cannot upload more files.")
            return
        
        document = update.message.document
        
        try:
            # Use original filename
            original_name = document.file_name
            temp_file_path = os.path.join(session['temp_path'], original_name)
            
            # Download file
            file_obj = await context.bot.get_file(document.file_id)
            await file_obj.download_to_drive(temp_file_path)
            
            # Calculate file hash
            with open(temp_file_path, 'rb') as f:
                file_hash = hashlib.sha256(f.read()).hexdigest()
            
            # Initial validation
            validation_errors = []
            
            # Extension check
            valid, error = FileValidator.validate_extension(original_name)
            if not valid:
                validation_errors.append(error)
            
            # File size check
            valid, error = FileValidator.validate_file_size(temp_file_path)
            if not valid:
                validation_errors.append(error)
            
            # VirusTotal scan
            if not validation_errors:
                valid, message = await FileValidator.scan_with_virustotal(temp_file_path, VIRUSTOTAL_API_KEY)
                if not valid:
                    validation_errors.append(message)
            
            # Log file to database
            validation_status = "failed" if validation_errors else "passed"
            error_text = "; ".join(validation_errors) if validation_errors else None
            
            self.db.log_file(
                session['session_id'], 
                original_name,
                temp_file_path,
                document.file_size,
                file_hash,
                validation_status,
                error_text
            )
            
            if validation_errors:
                # Remove failed file
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                
                error_message = "\n".join(validation_errors)
                await update.message.reply_text(
                    f"File Validation Failed\n\n"
                    f"File: {original_name}\n"
                    f"Errors:\n{error_message}"
                )
            else:
                session['files_uploaded'] += 1
                session['uploaded_files'] = session.get('uploaded_files', []) + [original_name]
                
                # Add to pending uploads
                if user_id not in self.pending_uploads:
                    self.pending_uploads[user_id] = []
                
                self.pending_uploads[user_id].append({
                    'name': original_name,
                    'size': document.file_size,
                    'path': temp_file_path
                })
                
                # Cancel existing timer if any
                if user_id in self.upload_timers:
                    self.upload_timers[user_id].cancel()
                
                # Create new timer
                self.upload_timers[user_id] = asyncio.create_task(
                    self.delayed_upload_notification(user_id, context)
                )
        
        except Exception as e:
            logger.error(f"Error handling document: {e}")
            await update.message.reply_text(
                f"Error processing file\n\n"
                f"Error: {str(e)}\n\n"
                f"Please try uploading the file again."
            )
    
    async def delayed_upload_notification(self, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Send notification after a delay to group multiple uploads"""
        try:
            # Wait for 2 seconds to group multiple uploads
            await asyncio.sleep(2)
            
            if user_id not in self.pending_uploads or not self.pending_uploads[user_id]:
                return
            
            session = self.user_sessions[user_id]
            pending_files = self.pending_uploads[user_id]
            
            # Create keyboard with both buttons if under limit
            keyboard = []
            if session['files_uploaded'] < MAX_FILES_PER_SESSION:
                keyboard.append([
                    InlineKeyboardButton("📤 Add More Files", callback_data="continue_upload"),
                    InlineKeyboardButton("✅ Finalize Upload", callback_data="finalize_upload")
                ])
            else:
                keyboard.append([InlineKeyboardButton("✅ Finalize Upload", callback_data="finalize_upload")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Create message for all pending files
            message = "📁 Files Validated Successfully\n\n"
            message += f"Files uploaded: {session['files_uploaded']}/{MAX_FILES_PER_SESSION}\n\n"
            
            message += "Files in this batch:\n"
            for file in pending_files:
                message += f"- {file['name']} ({self.formatFileSize(file['size'])})\n"
            
            # Clear pending uploads
            self.pending_uploads[user_id] = []
            
            # Send message
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error in delayed notification: {e}")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages"""
        user_id = update.effective_user.id
        
        if user_id not in self.user_sessions:
            await update.message.reply_text("No active session. Use /upload to start.")
            return
        
        session = self.user_sessions[user_id]
        
        if session['files_uploaded'] >= MAX_FILES_PER_SESSION:
            await update.message.reply_text(f"Maximum {MAX_FILES_PER_SESSION} files per session reached.")
            return
        
        try:
            # Get the highest resolution photo
            photo = update.message.photo[-1]
            
            # Use original filename if available, otherwise use photo.jpg
            filename = "photo.jpg"
            if update.message.caption:
                filename = update.message.caption
            
            # Get file information
            file_obj = await context.bot.get_file(photo.file_id)
            
            # Create download path within session temp directory
            temp_file_path = os.path.join(session['temp_path'], filename)
            
            # Download the photo
            await file_obj.download_to_drive(temp_file_path)
            
            # Calculate file hash
            with open(temp_file_path, 'rb') as f:
                file_hash = hashlib.sha256(f.read()).hexdigest()
            
            # Get file size
            file_size = os.path.getsize(temp_file_path)
            
            # Validate file size
            validation_errors = []
            valid, error = FileValidator.validate_file_size(temp_file_path)
            if not valid:
                validation_errors.append(error)
            
            # VirusTotal scan
            if not validation_errors:
                valid, message = await FileValidator.scan_with_virustotal(temp_file_path, VIRUSTOTAL_API_KEY)
                if not valid:
                    validation_errors.append(message)
            
            # Log file to database
            validation_status = "failed" if validation_errors else "passed"
            error_text = "; ".join(validation_errors) if validation_errors else None
            
            self.db.log_file(
                session['session_id'], 
                filename, 
                temp_file_path,
                file_size,
                file_hash,
                validation_status,
                error_text
            )
            
            if validation_errors:
                # Remove failed file
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                
                error_message = "\n".join(validation_errors)
                await update.message.reply_text(
                    f"Image Validation Failed\n\n"
                    f"File: {filename}\n"
                    f"Errors:\n{error_message}"
                )
            else:
                session['files_uploaded'] += 1
                session['uploaded_files'] = session.get('uploaded_files', []) + [filename]
                
                # Add to pending uploads
                if user_id not in self.pending_uploads:
                    self.pending_uploads[user_id] = []
                
                self.pending_uploads[user_id].append({
                    'name': filename,
                    'size': file_size,
                    'path': temp_file_path
                })
                
                # Cancel existing timer if any
                if user_id in self.upload_timers:
                    self.upload_timers[user_id].cancel()
                
                # Create new timer
                self.upload_timers[user_id] = asyncio.create_task(
                    self.delayed_upload_notification(user_id, context)
                )
                
        except Exception as e:
            logger.error(f"Error handling photo: {e}")
            await update.message.reply_text(
                f"Error processing image\n\n"
                f"Error: {str(e)}\n\n"
                f"Please try sending the image again."
            )
    
    async def offer_finalization(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Offer to finalize the upload session"""
        keyboard = [
            [InlineKeyboardButton("✅ Move to Final Location", callback_data="finalize_upload")],
            [InlineKeyboardButton("❌ Cancel Session", callback_data="cancel_session")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🎯 **Ready to finalize your upload?**\n\n"
            "All files have been validated successfully!\n"
            "Click below to move files to the final storage location.",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "manage_users":
            await self.manage_users(update, context)
        elif query.data == "check_requests":
            await self.handle_check_requests(query)
        elif query.data == "invite_user":
            await self.handle_invite_user(query, context)
        elif query.data == "show_users":
            await self.handle_show_users(query)
        elif query.data.startswith("approve_"):
            request_id = int(query.data.split("_")[1])
            await self.handle_approve_request(query, request_id, context)
        elif query.data.startswith("reject_"):
            request_id = int(query.data.split("_")[1])
            await self.handle_reject_request(query, request_id, context)
        elif query.data.startswith("direct_approve_"):
            request_id = int(query.data.split("_")[2])
            await self.handle_approve_request(query, request_id, context)
        elif query.data.startswith("direct_reject_"):
            request_id = int(query.data.split("_")[2])
            await self.handle_reject_request(query, request_id, context)
        elif query.data == "ready_upload":
            await query.edit_message_text(
                "📁 **Ready to receive files!**\n\n"
                "Send all files at once or send one by one, We are ready to receive your files!",
                parse_mode='Markdown'
            )
        elif query.data == "finalize_upload":
            await self.finalize_upload(query, context)
        elif query.data == "cancel_session":
            await self.cancel_session(query, context)
        elif query.data == "continue_upload":
            await query.edit_message_text(
                "📁 **Continue uploading files...**\n\n"
                "Send your next document.",
                parse_mode='Markdown'
            )
    
    def get_unique_filename(self, base_path: str, filename: str) -> str:
        """Generate a unique filename using Windows-style naming (filename(1).ext)"""
        if not os.path.exists(os.path.join(base_path, filename)):
            return filename
        
        name, ext = os.path.splitext(filename)
        counter = 1
        
        while True:
            new_filename = f"{name}({counter}){ext}"
            if not os.path.exists(os.path.join(base_path, new_filename)):
                return new_filename
            counter += 1

    async def finalize_upload(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Move validated files to final locations (both local and network)"""
        user_id = query.from_user.id
        
        if user_id not in self.user_sessions:
            await query.edit_message_text("No active session found.")
            return
        
        session = self.user_sessions[user_id]
        session_id = session['session_id']
        temp_path = session['temp_path']
        
        try:
            # Validate required files before finalizing
            valid, error = FileValidator.validate_required_files(session.get('uploaded_files', []))
            if not valid:
                await query.edit_message_text(
                    f"Cannot Finalize Upload\n{error}\nPlease upload all required file types."
                )
                return
            
            # Get storage paths with user's name
            local_path, network_path = self.get_storage_paths(user_id)
            
            # Move files from temp to both final locations
            moved_files = []
            for file_name in os.listdir(temp_path):
                src = os.path.join(temp_path, file_name)
                
                # Get unique filenames for both locations
                local_filename = self.get_unique_filename(local_path, file_name)
                network_filename = self.get_unique_filename(network_path, file_name)
                
                local_dst = os.path.join(local_path, local_filename)
                network_dst = os.path.join(network_path, network_filename)
                
                if os.path.isfile(src):
                    # Copy to local storage
                    shutil.copy2(src, local_dst)
                    # Copy to network storage
                    shutil.copy2(src, network_dst)
                    # Remove from temp
                    os.remove(src)
                    moved_files.append(file_name)
            
            # Update database
            self.db.update_session_status(
                session_id, 
                "completed", 
                local_path,
                network_path
            )
            
            # Clean up temp directory
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path)
            
            # Remove session from memory
            del self.user_sessions[user_id]
            
            # Create success message without sensitive data
            files_list = "\n".join([f"- {f}" for f in moved_files])
            success_msg = (
                "✅ Upload Completed Successfully\n\n"
                f"Files processed: {len(moved_files)}\n\n"
                f"Files:\n{files_list}"
            )
            
            await query.edit_message_text(
                text=success_msg,
                parse_mode=None  # No Markdown parsing
            )
            
        except Exception as e:
            logger.error(f"Finalization failed: {e}")
            await query.edit_message_text(
                "Upload failed. Please try again.",
                parse_mode=None  # No Markdown parsing
            )
    
    async def cancel_session(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Cancel current upload session"""
        user_id = query.from_user.id
        
        if user_id in self.user_sessions:
            session = self.user_sessions[user_id]
            
            # Clean up temp files
            temp_path = session['temp_path']
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path)
            
            # Update database
            self.db.update_session_status(session['session_id'], "cancelled")
            
            # Remove from memory
            del self.user_sessions[user_id]
        
        await query.edit_message_text(
            "❌ **Session Cancelled**\n\n"
            "All temporary files have been removed.\n"
            "Use /upload to start a new session.",
            parse_mode='Markdown'
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current upload session status"""
        user = update.effective_user
        
        # Check if user has access
        if not await self.check_user_access(user.id):
            await update.message.reply_text(
                "❌ You don't have access to use this command.\n"
                "Please wait for your manager's approval or contact them for a new invitation link."
            )
            return
        
        user_id = update.effective_user.id
        
        if user_id not in self.user_sessions:
            await update.message.reply_text("📋 No active session. Use /upload to start.")
            return
        
        session = self.user_sessions[user_id]
        
        status_msg = f"""
📊 **Current Session Status**

Files uploaded: {session['files_uploaded']}/{MAX_FILES_PER_SESSION}

Use /cancel to cancel this session.
        """
        
        await update.message.reply_text(status_msg, parse_mode='Markdown')
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel command handler"""
        user_id = update.effective_user.id
        
        if user_id not in self.user_sessions:
            await update.message.reply_text("❌ No active session to cancel.")
            return
        
        session = self.user_sessions[user_id]
        
        # Clean up
        temp_path = session['temp_path']
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path)
        
        self.db.update_session_status(session['session_id'], "cancelled")
        del self.user_sessions[user_id]
        
        await update.message.reply_text("❌ Session cancelled and temporary files removed.")

    async def manage_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user management for managers"""
        user_id = update.effective_user.id
        role = self.db.get_user_role(user_id)
        
        if role < ROLE_MANAGER:
            await update.message.reply_text("❌ You don't have permission to manage users.")
            return
        
        keyboard = [
            [InlineKeyboardButton("👥 Invite User", callback_data="invite_user")],
            [InlineKeyboardButton("📋 Check Requests", callback_data="check_requests")],
            [InlineKeyboardButton("👤 Show Users", callback_data="show_users")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👥 **User Management**\n\n"
            "Select an option:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def handle_invite_user(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Handle user invitation"""
        manager_id = query.from_user.id
        
        # Generate invitation code using the database manager method
        invitation_code = self.db.create_invitation(manager_id)
        
        # Create invitation link
        bot = await context.bot.get_me()
        invitation_link = f"https://t.me/{bot.username}?start={invitation_code}"
        
        # Show invitation link
        await query.edit_message_text(
            f"🔗 Here's your team invitation link (reusable):\n\n"
            f"{invitation_link}\n\n"
            "Share this single link with anyone on your team.\n"
            "All users can use this link to request access."
        )

    async def handle_check_requests(self, query: CallbackQuery):
        """Show pending user requests"""
        manager_id = query.from_user.id
        requests = self.db.get_pending_requests(manager_id)
        
        if not requests:
            await query.edit_message_text("No pending requests.")
            return
        
        message = "📋 **Pending Requests**\n\n"
        keyboard = []
        
        for req in requests:
            message += (
                f"User: {req['first_name']} (@{req['username']})\n"
                f"Request ID: {req['id']}\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(f"✅ Approve {req['first_name']}", callback_data=f"approve_{req['id']}"),
                InlineKeyboardButton(f"❌ Reject {req['first_name']}", callback_data=f"reject_{req['id']}")
            ])
        
        keyboard.append([InlineKeyboardButton("Back", callback_data="manage_users")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def handle_show_users(self, query: CallbackQuery):
        """Show users under the manager"""
        manager_id = query.from_user.id
        users = self.db.get_manager_users(manager_id)
        
        if not users:
            await query.edit_message_text("No users under your management.")
            return
        
        message = "👥 **Your Users**\n\n"
        for user in users:
            message += f"• {user['first_name']} (@{user['username']})\n"
        
        keyboard = [[InlineKeyboardButton("Back", callback_data="manage_users")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def handle_user_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user registration request"""
        user = update.effective_user
        invitation_code = context.args[0] if context.args else None
        
        if not invitation_code:
            await update.message.reply_text("Invalid invitation link.")
            return
        
        # Create user request
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                # Get manager_id from invitation
                cur.execute("""
                    SELECT manager_id 
                    FROM user_invitations 
                    WHERE invitation_code = %s AND status = 'active'
                """, (invitation_code,))
                result = cur.fetchone()
                
                if not result:
                    await update.message.reply_text("Invalid invitation link.")
                    return
                
                manager_id = result[0]
                
                # First create the user if they don't exist
                cur.execute("""
                    INSERT INTO users (telegram_id, username, first_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id) DO NOTHING
                """, (user.id, user.username, user.first_name))
                
                # Then create user request
                cur.execute("""
                    INSERT INTO user_requests (user_id, manager_id)
                    VALUES (%s, %s)
                    RETURNING id
                """, (user.id, manager_id))
                request_id = cur.fetchone()[0]
                conn.commit()
        
        # Notify manager about new request with direct approve/reject buttons
        try:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"direct_approve_{request_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"direct_reject_{request_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=manager_id,
                text=(
                    "🔔 New Registration Request\n\n"
                    f"User: {user.first_name} (@{user.username})\n\n"
                    "You can approve or reject this request directly using the buttons below.\n"
                    "Or check all pending requests using /manage_users"
                ),
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error sending manager notification: {e}")
        
        await update.message.reply_text(
            "⏳ Your registration request has been sent to the manager.\n"
            "Please wait for approval. You will be notified once approved.\n\n"
            "Until then, you cannot use the bot's features."
        )

    async def handle_approve_request(self, query: CallbackQuery, request_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Handle approval of user request"""
        manager_id = query.from_user.id
        
        # Get request details
        with self.db.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT ur.*, u.username, u.first_name, u.telegram_id
                    FROM user_requests ur
                    JOIN users u ON ur.user_id = u.telegram_id
                    WHERE ur.id = %s AND ur.manager_id = %s AND ur.status = 'pending'
                """, (request_id, manager_id))
                request = cur.fetchone()
                
                if not request:
                    await query.edit_message_text("Request not found or already processed.")
                    return
                
                # Update user with manager_id and role
                cur.execute("""
                    UPDATE users 
                    SET manager_id = %s, role = 0
                    WHERE telegram_id = %s
                """, (manager_id, request['user_id']))
                
                # Update request status
                cur.execute("""
                    UPDATE user_requests 
                    SET status = 'approved', updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (request_id,))
                
                conn.commit()
        
        # Notify manager
        await query.edit_message_text(
            f"✅ User {request['first_name']} has been approved and added to your team."
        )
        
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=request['user_id'],
                text=(
                    "✅ Your registration has been approved!\n\n"
                    "You can now use the bot. Here are the available commands:\n"
                    "/upload - Start uploading files\n"
                    "/help - Show help information\n\n"
                    "Welcome to the team! 🎉"
                )
            )
        except Exception as e:
            logger.error(f"Error sending approval notification: {e}")

    async def handle_reject_request(self, query: CallbackQuery, request_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Handle rejection of user request"""
        manager_id = query.from_user.id
        
        # Get request details
        with self.db.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT ur.*, u.username, u.first_name, u.telegram_id
                    FROM user_requests ur
                    JOIN users u ON ur.user_id = u.telegram_id
                    WHERE ur.id = %s AND ur.manager_id = %s AND ur.status = 'pending'
                """, (request_id, manager_id))
                request = cur.fetchone()
                
                if not request:
                    await query.edit_message_text("Request not found or already processed.")
                    return
        
        # Ask for rejection reason
        context.user_data['pending_rejection'] = {
            'request_id': request_id,
            'user_id': request['user_id'],
            'first_name': request['first_name']
        }
        
        await query.edit_message_text(
            f"Please provide a reason for rejecting {request['first_name']}'s request.\n"
            "Type your reason in the next message."
        )

    async def handle_rejection_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the rejection reason provided by manager"""
        if 'pending_rejection' not in context.user_data:
            return
        
        rejection_data = context.user_data['pending_rejection']
        reason = update.message.text
        
        # Update request status with reason
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_requests 
                    SET status = 'rejected', rejection_reason = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (reason, rejection_data['request_id']))
                conn.commit()
        
        # Notify manager
        await update.message.reply_text(
            f"✅ Request from {rejection_data['first_name']} has been rejected."
        )
        
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=rejection_data['user_id'],
                text=(
                    "❌ Your registration request has been rejected\n\n"
                    f"Reason: {reason}\n\n"
                    "Please contact your manager for a new invitation link."
                )
            )
        except Exception as e:
            logger.error(f"Error sending rejection notification: {e}")
        
        # Clear pending rejection
        del context.user_data['pending_rejection']

    async def check_user_access(self, user_id: int) -> bool:
        """Check if user has access to use the bot"""
        with self.db.get_connection() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute("""
                    SELECT u.*, ur.status as request_status
                    FROM users u
                    LEFT JOIN user_requests ur ON u.telegram_id = ur.user_id
                    WHERE u.telegram_id = %s
                    ORDER BY ur.created_at DESC
                    LIMIT 1
                """, (user_id,))
                result = cur.fetchone()
                
                if not result:
                    return False
                
                # Check if user has a pending or rejected request
                if result['request_status'] in ['pending', 'rejected']:
                    return False
                
                return True

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show upload history"""
        user_id = update.effective_user.id
        role = self.db.get_user_role(user_id)
        
        if role < 0:  # Not approved user
            await update.message.reply_text(
                "You don't have access to use this command.\n"
                "Please wait for your manager's approval or contact them for a new invitation link."
            )
            return
        
        # Create web app button
        keyboard = [[InlineKeyboardButton(
            "View History",
            web_app=WebAppInfo(url="https://5fbc7f64a6bc.ngrok-free.app/")
        )]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Click the button below to view upload history:",
            reply_markup=reply_markup
        )

def main():
    """Start the bot"""
    bot = TelegramBot()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("upload", bot.upload_command))
    application.add_handler(CommandHandler("status", bot.status_command))
    application.add_handler(CommandHandler("cancel", bot.cancel_command))
    application.add_handler(CommandHandler("manage_users", bot.manage_users))
    application.add_handler(CommandHandler("history", bot.history_command))
    application.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    
    # Add message handler for any text input (including single characters)
    # This will trigger the same response as /start for any keyboard input
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        bot.start  # Use the same function as /start command
    ))
    
    # Start bot
    logger.info("Starting Telegram Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()