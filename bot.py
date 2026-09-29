import os
import asyncio
import logging
import math
import random
import subprocess
import shutil
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events, errors
from telethon.tl.types import InputFileBig
from telethon.tl.functions.upload import SaveBigFilePartRequest
import yt_dlp

# Load environment variables
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID")) if os.getenv("API_ID") else None
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")

# FFMPEG PATHS (dynamic detection with Windows fallback)
FFMPEG_DIR = r"C:\ffmpeg\bin"
FFMPEG_EXE = shutil.which("ffmpeg") or os.path.join(FFMPEG_DIR, "ffmpeg.exe")

# Local Downloads Folder (Inside Bot folder)
DOWNLOAD_BASE = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

# Initialize Telethon Client
session_name = os.path.join(BASE_DIR, f"bot_session_{PHONE}")
tg_client = TelegramClient(session_name, API_ID, API_HASH)

def check_ffmpeg():
    """Diagnostic check for FFmpeg and AMF"""
    try:
        res = subprocess.run([FFMPEG_EXE, '-version'], capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"FFmpeg not found at {FFMPEG_EXE}!")
            return False, False
        
        res = subprocess.run([FFMPEG_EXE, '-encoders'], capture_output=True, text=True)
        amf_available = 'h264_amf' in res.stdout
        return True, amf_available
    except Exception as e:
        logger.error(f"Error checking FFmpeg: {e}")
        return False, False

ffmpeg_exists, amf_exists = check_ffmpeg()

def get_progress_bar(current, total, length=15):
    """Generate a text-based progress bar"""
    filled = int(length * current // total)
    bar = "█" * filled + "░" * (length - filled)
    percent = (current / total) * 100
    return f"[{bar}] {percent:.1f}%"

async def fast_upload(file_path, progress_callback=None):
    """Parallel upload for large files"""
    file_size = os.path.getsize(file_path)
    chunk_size = 512 * 1024
    total_parts = math.ceil(file_size / chunk_size)
    file_id = random.getrandbits(63)
    
    semaphore = asyncio.Semaphore(20)
    parts_uploaded = [0]
    
    async def upload_part(part_index, chunk_data):
        async with semaphore:
            try:
                await tg_client(SaveBigFilePartRequest(
                    file_id=file_id,
                    file_part=part_index,
                    file_total_parts=total_parts,
                    bytes=chunk_data
                ))
                parts_uploaded[0] += 1
                if progress_callback:
                    await progress_callback(parts_uploaded[0] * chunk_size, file_size)
            except Exception as e:
                logger.error(f"Error uploading part {part_index}: {e}")
                raise

    tasks = []
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            chunk = f.read(chunk_size)
            tasks.append(upload_part(i, chunk))

    await asyncio.gather(*tasks)

    return InputFileBig(
        id=file_id,
        parts=total_parts,
        name=os.path.basename(file_path)
    )

async def reencode_video_gpu(input_path):
    """Re-encode video using AMD GPU (amf) or CPU fallback"""
    output_path = os.path.splitext(input_path)[0] + "_gpu.mp4"
    logger.info(f"Starting re-encoding: {input_path} -> {output_path}")
    
    codec = 'h264_amf' if amf_exists else 'libx264'
    
    cmd = [
        FFMPEG_EXE, '-y',
        '-i', input_path,
        '-c:v', codec,
    ]
    
    if amf_exists:
        cmd.extend(['-rc', 'vbr_latency', '-quality', 'quality'])
    else:
        cmd.extend(['-preset', 'veryfast', '-crf', '23'])
        
    cmd.extend([
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k',
        output_path
    ])
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        
        if process.returncode != 0:
            if codec == 'h264_amf':
                logger.warning("AMF failed, falling back to CPU...")
                cmd[4] = 'libx264'
                cmd[5:7] = ['-preset', 'veryfast', '-crf', '23']
                process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await process.communicate()
                if process.returncode == 0: return output_path
            return input_path
            
        return output_path
    except Exception as e:
        logger.error(f"Re-encoding Exception: {e}")
        return input_path

async def download_yt(event, url, mode='video'):
    # Create unique request folder
    req_id = hex(random.getrandbits(32))[2:]
    req_dir = os.path.join(DOWNLOAD_BASE, req_id)
    os.makedirs(req_dir, exist_ok=True)

    status_msg = await event.reply(f"⏳ Analyzing YouTube {mode}...")
    
    # Generic options for extraction/discovery
    ydl_opts_base = {
        'ffmpeg_location': FFMPEG_DIR,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_base) as ydl:
            loop = asyncio.get_event_loop()
            # Extract info first without downloading
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            
            if 'entries' in info:
                entries = list(info['entries'])
                playlist_title = info.get('title', 'Playlist')
            else:
                entries = [info]
                playlist_title = None
            
            total = len(entries)
            if total > 1:
                await status_msg.edit(f"📂 **{playlist_title}**\nFound {total} items. Processing sequentially...")

            for i, entry in enumerate(entries, 1):
                if not entry: continue
                
                title = entry.get('title', 'Unknown')
                video_url = entry.get('webpage_url') or entry.get('url')
                if not video_url: continue

                # Get thumbnail URL
                thumb_url = entry.get('thumbnail')
                thumb_path = os.path.join(req_dir, f"thumb_{i}.jpg")
                
                # Progress callback for yt-dlp
                def ydl_progress_hook(d):
                    if d['status'] == 'downloading':
                        try:
                            p = d.get('_percent_str', '0%').replace('%','')
                            downloaded = d.get('downloaded_bytes', 0)
                            total_bt = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                            if total_bt:
                                bar = get_progress_bar(downloaded, total_bt)
                                status_text = f"⏳ Downloading {i}/{total}: **{title}**\n{bar}"
                                asyncio.run_coroutine_threadsafe(status_msg.edit(status_text), loop)
                        except Exception: pass

                # Specific options for this individual download
                current_ydl_opts = {
                    'format': 'bestvideo+bestaudio/best' if mode == 'video' else 'bestaudio/best',
                    'outtmpl': os.path.join(req_dir, f'item_{i}_%(title)s.%(ext)s'),
                    'noplaylist': True,
                    'ffmpeg_location': FFMPEG_DIR,
                    'quiet': True,
                    'no_warnings': True,
                    'progress_hooks': [ydl_progress_hook],
                    'writethumbnail': True, # We'll handle thumbnail download manually or via ydl
                }
                
                if mode == 'music':
                    current_ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '320',
                    }]

                await status_msg.edit(f"⏳ Downloading {i}/{total}: **{title}**\n[░░░░░░░░░░░░░░░] 0.0%")
                
                # Download the single entry
                with yt_dlp.YoutubeDL(current_ydl_opts) as ydl_down:
                    await loop.run_in_executor(None, lambda: ydl_down.download([video_url]))
                
                # Locate the file and thumbnail
                downloaded_file = None
                local_thumb = None
                for file_in_dir in os.listdir(req_dir):
                    if file_in_dir.startswith(f'item_{i}_'):
                        full_p = os.path.join(req_dir, file_in_dir)
                        if file_in_dir.endswith(('.jpg', '.png', '.webp', '.jpeg')):
                            local_thumb = full_p
                        else:
                            downloaded_file = full_p
                
                if not downloaded_file or not os.path.exists(downloaded_file):
                    await event.respond(f"❌ Failed to download or find: {title}")
                    continue

                # Process (re-encode)
                if mode == 'video':
                    await status_msg.edit(f"🚀 GPU Processing ({i}/{total}): **{title}**\n*Optimizing for Telegram...*")
                    processed_path = await reencode_video_gpu(downloaded_file)
                else:
                    processed_path = downloaded_file

                # Upload with progress bar
                async def upload_progress(current_bt, total_bt):
                    bar = get_progress_bar(current_bt, total_bt)
                    await status_msg.edit(f"📤 Uploading ({i}/{total}): **{title}**\n{bar}")

                await status_msg.edit(f"📤 Uploading ({i}/{total}): **{title}**\n[░░░░░░░░░░░░░░░] 0.0%")
                uploaded_file = await fast_upload(processed_path, progress_callback=upload_progress)
                
                # Send
                caption = f"🎥 **{title}**" if mode == 'video' else f"🎵 **{title}**"
                await tg_client.send_file(
                    event.chat_id, 
                    uploaded_file, 
                    caption=caption, 
                    thumb=local_thumb,
                    supports_streaming=True if mode == 'video' else False
                )

                # IMMEDIATE CLEANUP for this item
                try:
                    files_to_remove = [downloaded_file, processed_path, local_thumb]
                    for f_rm in files_to_remove:
                        if f_rm and os.path.exists(f_rm):
                            os.remove(f_rm)
                    logger.info(f"Cleaned up files for item {i}: {title}")
                except Exception as e:
                    logger.error(f"Cleanup error for {title}: {e}")

        await status_msg.edit(f"✅ **Finished!**\nProcessed {total} items successfully.")

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await status_msg.edit(f"❌ Error: {str(e)}")
    finally:
        # Final cleanup of the unique request directory
        if os.path.exists(req_dir):
            shutil.rmtree(req_dir, ignore_errors=True)

@tg_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await help_handler(event)

@tg_client.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    await event.reply(
        "🚀 **YouTube Downloader Bot Help**\n\n"
        "Commands:\n"
        "• `/mp4 <link>` - Download high-quality Video (AMD GPU accelerated)\n"
        "• `/mp3 <link>` - Download high-quality Audio (320kbps)\n"
        "• `/help` - Show this message\n\n"
        "💡 **Tip**: You can also just send a YouTube link directly, and I will download it as an MP4 video!"
    )

@tg_client.on(events.NewMessage(pattern='/mp4'))
async def mp4_cmd_handler(event):
    parts = event.text.split(' ', 1)
    if len(parts) > 1:
        await download_yt(event, parts[1], mode='video')
    else:
        await event.reply("❌ Usage: `/mp4 <youtube_link>`")

@tg_client.on(events.NewMessage(pattern='/mp3'))
async def mp3_cmd_handler(event):
    parts = event.text.split(' ', 1)
    if len(parts) > 1:
        await download_yt(event, parts[1], mode='music')
    else:
        await event.reply("❌ Usage: `/mp3 <youtube_link>`")

@tg_client.on(events.NewMessage)
async def general_handler(event):
    if event.text.startswith('/'): return
    
    text = event.text
    if "youtube.com" in text or "youtu.be" in text:
        url = None
        for word in text.split():
            if "youtube.com" in word or "youtu.be" in word:
                url = word
                break
        if url:
            await download_yt(event, url, mode='video')

if __name__ == '__main__':
    print("YouTube Downloader Bot is starting...")
    tg_client.start(bot_token=BOT_TOKEN)
    print("Bot is running. Send a YouTube link to the bot chat!")
    tg_client.run_until_disconnected()
