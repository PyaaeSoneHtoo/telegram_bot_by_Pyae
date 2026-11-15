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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8727004701:AAFnQTPneMVlT6pyGC_dys5KHxSQ_VX6OIM")
API_ID = int(os.getenv("API_ID", "33821639")) if os.getenv("API_ID") else 33821639
API_HASH = os.getenv("API_HASH", "22533aeb1ea63aab9329e0f47f280118")
PHONE = os.getenv("PHONE", "+95943094338")

# COOKIES SETUP
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")
cookies_env = os.getenv("YOUTUBE_COOKIES")
if cookies_env:
    try:
        import base64
        try:
            decoded = base64.b64decode(cookies_env).decode("utf-8")
            if "Cookie File" in decoded or ".google.com" in decoded or ".youtube.com" in decoded:
                cookies_env = decoded
        except Exception:
            pass
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookies_env.strip() + "\n")
        logger.info(f"Loaded YouTube cookies from YOUTUBE_COOKIES env var ({len(cookies_env)} bytes).")
    except Exception as e:
        logger.error(f"Failed to write cookies from env: {e}")

# NODEJS RUNTIME DETECTION (for yt-dlp JS n-challenge solving)
NODE_EXE = shutil.which("node") or shutil.which("nodejs")
if NODE_EXE:
    logger.info(f"Found Node.js JavaScript runtime: {NODE_EXE}")
else:
    logger.info("Node.js not found in system PATH.")

# FFMPEG PATHS (dynamic cross-platform detection)
FFMPEG_EXE = shutil.which("ffmpeg") or (r"C:\ffmpeg\bin\ffmpeg.exe" if os.path.exists(r"C:\ffmpeg\bin\ffmpeg.exe") else "ffmpeg")
FFMPEG_DIR = os.path.dirname(FFMPEG_EXE) if FFMPEG_EXE and os.path.dirname(FFMPEG_EXE) and os.path.isabs(FFMPEG_EXE) else None

# Local Downloads Folder
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
    if total <= 0:
        return "[░░░░░░░░░░░░░░░] 0.0%"
    filled = min(length, max(0, int(length * current // total)))
    bar = "█" * filled + "░" * (length - filled)
    percent = min(100.0, max(0.0, (current / total) * 100))
    return f"[{bar}] {percent:.1f}%"

async def fast_upload(file_path, progress_callback=None):
    """Parallel upload for large files with graceful fallback"""
    try:
        file_size = os.path.getsize(file_path)
        chunk_size = 512 * 1024
        total_parts = math.ceil(file_size / chunk_size)
        file_id = random.getrandbits(63)
        
        semaphore = asyncio.Semaphore(15)
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
    except Exception as e:
        logger.warning(f"fast_upload failed, falling back to standard upload: {e}")
        return await tg_client.upload_file(file_path, progress_callback=progress_callback)

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

def get_extraction_strategies():
    """Returns fallback extraction/download strategies for yt-dlp"""
    strategies = []
    has_cookies = os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0

    # Strategy 1: Android client (most reliable on datacenter IPs without SABR block)
    strategies.append({
        'name': 'android_client',
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
    })

    # Strategy 2: If cookies exist, try authenticated desktop/mweb
    if has_cookies:
        strategies.append({
            'name': 'cookies_web',
            'cookiefile': COOKIE_FILE,
            'extractor_args': {'youtube': {'player_client': ['web', 'mweb', 'tv']}},
        })
        strategies.append({
            'name': 'cookies_default',
            'cookiefile': COOKIE_FILE,
        })

    # Strategy 3: Default web client fallback
    strategies.append({
        'name': 'default_web',
    })

    return strategies

def apply_common_ydl_opts(opts):
    """Appends ffmpeg and nodejs runtimes to options dictionary"""
    if FFMPEG_DIR:
        opts['ffmpeg_location'] = FFMPEG_DIR
    if NODE_EXE:
        opts['js_runtimes'] = {'node': None}
    return opts

async def download_yt(event, url, mode='video'):
    # Create unique request folder
    req_id = hex(random.getrandbits(32))[2:]
    req_dir = os.path.join(DOWNLOAD_BASE, req_id)
    os.makedirs(req_dir, exist_ok=True)

    status_msg = await event.reply(f"⏳ Analyzing YouTube {mode}...")
    loop = asyncio.get_event_loop()

    # Step 1: Info extraction with multi-strategy cascade
    strategies = get_extraction_strategies()
    info = None
    last_extract_err = None
    winning_strategy = None

    for strat in strategies:
        strat_name = strat['name']
        ydl_opts_base = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        apply_common_ydl_opts(ydl_opts_base)
        if 'cookiefile' in strat:
            ydl_opts_base['cookiefile'] = strat['cookiefile']
        if 'extractor_args' in strat:
            ydl_opts_base['extractor_args'] = strat['extractor_args']

        try:
            logger.info(f"Trying extraction strategy '{strat_name}' for {url}...")
            extracted = await loop.run_in_executor(
                None, lambda o=ydl_opts_base: yt_dlp.YoutubeDL(o).extract_info(url, download=False)
            )
            if extracted:
                info = extracted
                winning_strategy = strat
                logger.info(f"Extraction succeeded with strategy '{strat_name}'.")
                break
        except Exception as e:
            last_extract_err = e
            logger.warning(f"Strategy '{strat_name}' extraction failed: {e}")

    if not info:
        err_text = str(last_extract_err) if last_extract_err else "Unable to extract video information."
        logger.error(f"All extraction strategies failed for {url}: {err_text}")
        await status_msg.edit(f"❌ Error: {err_text}")
        if os.path.exists(req_dir):
            shutil.rmtree(req_dir, ignore_errors=True)
        return

    try:
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
            if not entry:
                continue

            title = entry.get('title', 'Unknown')
            video_url = entry.get('webpage_url') or entry.get('url')
            if not video_url:
                continue

            local_thumb = None

            # Progress callback for yt-dlp (throttled to avoid Telegram FloodWait)
            last_edit_time = [0]
            def ydl_progress_hook(d):
                if d.get('status') == 'downloading':
                    now = loop.time() if loop.is_running() else 0
                    if now - last_edit_time[0] >= 2.0:
                        last_edit_time[0] = now
                        try:
                            downloaded_bt = d.get('downloaded_bytes', 0)
                            total_bt = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                            if total_bt:
                                bar = get_progress_bar(downloaded_bt, total_bt)
                                status_text = f"⏳ Downloading ({i}/{total}): **{title}**\n{bar}"
                                asyncio.run_coroutine_threadsafe(status_msg.edit(status_text), loop)
                        except Exception:
                            pass

            await status_msg.edit(f"⏳ Downloading ({i}/{total}): **{title}**\n[░░░░░░░░░░░░░░░] 0.0%")

            # Ordered download strategies (winning strategy first)
            dl_strategies = [winning_strategy] if winning_strategy else []
            for s in strategies:
                if s not in dl_strategies:
                    dl_strategies.append(s)

            download_success = False
            last_dl_err = None

            for d_strat in dl_strategies:
                d_name = d_strat['name']
                current_ydl_opts = {
                    'format': 'bestvideo+bestaudio/best' if mode == 'video' else 'bestaudio/best',
                    'outtmpl': os.path.join(req_dir, f'item_{i}_%(title)s.%(ext)s'),
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'progress_hooks': [ydl_progress_hook],
                    'writethumbnail': True,
                }
                apply_common_ydl_opts(current_ydl_opts)
                if 'cookiefile' in d_strat:
                    current_ydl_opts['cookiefile'] = d_strat['cookiefile']
                if 'extractor_args' in d_strat:
                    current_ydl_opts['extractor_args'] = d_strat['extractor_args']

                if mode == 'music':
                    current_ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '320',
                    }]

                try:
                    logger.info(f"Downloading item {i} using strategy '{d_name}'...")
                    await loop.run_in_executor(
                        None, lambda o=current_ydl_opts: yt_dlp.YoutubeDL(o).download([video_url])
                    )
                    download_success = True
                    break
                except Exception as e:
                    last_dl_err = e
                    logger.warning(f"Download strategy '{d_name}' failed for {title}: {e}")

            if not download_success:
                await event.respond(f"❌ Failed to download: {title}\nReason: {last_dl_err}")
                continue

            # Locate the file and thumbnail
            downloaded_file = None
            for file_in_dir in os.listdir(req_dir):
                if file_in_dir.startswith(f'item_{i}_'):
                    full_p = os.path.join(req_dir, file_in_dir)
                    if file_in_dir.lower().endswith(('.jpg', '.png', '.webp', '.jpeg')):
                        local_thumb = full_p
                    else:
                        downloaded_file = full_p

            if not downloaded_file or not os.path.exists(downloaded_file):
                await event.respond(f"❌ Download completed but output file not found: {title}")
                continue

            # Process (re-encode)
            if mode == 'video':
                await status_msg.edit(f"🚀 Processing ({i}/{total}): **{title}**\n*Optimizing for Telegram...*")
                processed_path = await reencode_video_gpu(downloaded_file)
            else:
                processed_path = downloaded_file

            # Upload with progress bar (throttled)
            last_upload_edit = [0]
            async def upload_progress(current_bt, total_bt):
                now = loop.time()
                if now - last_upload_edit[0] >= 2.0 or current_bt >= total_bt:
                    last_upload_edit[0] = now
                    bar = get_progress_bar(current_bt, total_bt)
                    try:
                        await status_msg.edit(f"📤 Uploading ({i}/{total}): **{title}**\n{bar}")
                    except Exception:
                        pass

            await status_msg.edit(f"📤 Uploading ({i}/{total}): **{title}**\n[░░░░░░░░░░░░░░░] 0.0%")
            uploaded_file = await fast_upload(processed_path, progress_callback=upload_progress)

            # Send file to user
            caption = f"🎥 **{title}**" if mode == 'video' else f"🎵 **{title}**"
            await tg_client.send_file(
                event.chat_id,
                uploaded_file,
                caption=caption,
                thumb=local_thumb,
                supports_streaming=True if mode == 'video' else False
            )

            # Cleanup for this item
            try:
                files_to_remove = [downloaded_file, processed_path, local_thumb]
                for f_rm in set(files_to_remove):
                    if f_rm and os.path.exists(f_rm):
                        os.remove(f_rm)
                logger.info(f"Cleaned up files for item {i}: {title}")
            except Exception as e:
                logger.error(f"Cleanup error for {title}: {e}")

        await status_msg.edit(f"✅ **Finished!**\nProcessed {total} items successfully.")

    except Exception as e:
        logger.error(f"Download Error: {e}", exc_info=True)
        await status_msg.edit(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(req_dir):
            shutil.rmtree(req_dir, ignore_errors=True)

@tg_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply(
        "👋 **Welcome to VideoPipeline Music & Video Bot!** (@VideoPipelineMusicBot)\n\n"
        "Send me any YouTube link or use commands below:\n\n"
        "• `/mp4 <youtube_link>` - Download high-quality MP4 Video\n"
        "• `/mp3 <youtube_link>` - Download high-quality 320kbps MP3 Audio\n"
        "• `/help` - Show instructions\n\n"
        "💡 **Tip**: You can also just paste or forward any YouTube link directly to download it as MP4!"
    )

@tg_client.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    await event.reply(
        "🚀 **VideoPipeline Music & Video Bot Help**\n\n"
        "Commands:\n"
        "• `/mp4 <link>` - Download high-quality Video (Fast stream copy / Re-encode)\n"
        "• `/mp3 <link>` - Download high-quality Audio (320kbps MP3)\n"
        "• `/help` - Show this message\n\n"
        "💡 **Tip**: Send any YouTube link directly to download as video!"
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
