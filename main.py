from flask import Flask, jsonify, request
from datetime import datetime

import atexit
import requests
import subprocess
import time
import os
import shutil

app = Flask(__name__)

HA_URL = "https://clipittang.duckdns.org:18123"
HA_TOKEN = os.getenv("HA_TOKEN")

# ramdisk
EPHEMERAL_SEGMENTS_DIR = "/dev/shm/clip_segments/"
os.makedirs(EPHEMERAL_SEGMENTS_DIR, exist_ok=True)

FINAL_CLIPS_DIR = "/home/mohammadmahfooz/clips/"
os.makedirs(FINAL_CLIPS_DIR, exist_ok=True)

GOOGLE_PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
GOOGLE_PHOTOS_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

ACCESS_TOKEN = None
TOKEN_EXPIRY = 0

SEGMENT_TIME = 2  # seconds per segment
TOTAL_BUFFER_TIME = 310  # total buffer time in seconds
SEGMENT_WRAP = TOTAL_BUFFER_TIME // SEGMENT_TIME  # number of segments to keep

def cleanup_segments_dir():
    """
    Deletes all files in the EPHEMERAL_SEGMENTS_DIR to remove old segments on app startup.
    """
    for filename in os.listdir(EPHEMERAL_SEGMENTS_DIR):
        file_path = os.path.join(EPHEMERAL_SEGMENTS_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}: {e}")

def refresh_google_photos_token():
    """
    Uses the stored refresh token to obtain a new access token.
    """
    global ACCESS_TOKEN, TOKEN_EXPIRY
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    response = requests.post(token_url, data=payload)
    response.raise_for_status()
    token_data = response.json()
    ACCESS_TOKEN = token_data["access_token"]
    expires_in = token_data["expires_in"]  # seconds
    TOKEN_EXPIRY = time.time() + expires_in
    print("Refreshed Google Photos access token.")

def ensure_valid_token():
    """
    Checks if the current token is valid; if not, refreshes it.
    """
    if ACCESS_TOKEN is None or time.time() >= TOKEN_EXPIRY:
        refresh_google_photos_token()

# fetch a fresh token when the app starts
refresh_google_photos_token()

# clean up any existing segments from previous runs
cleanup_segments_dir()

ffmpeg_command = [
    "ffmpeg",
    "-thread_queue_size", "512",
    "-f", "v4l2",
    "-input_format", "mjpeg",
    "-video_size", "1920x1080",
    "-framerate", "30",
    "-i", "/dev/video0",
    "-thread_queue_size", "512",
    "-f", "pulse",
    "-i", "alsa_input.usb-EMEET_HD_Webcam_eMeet_C960_A241108000315080-02.analog-stereo",
    "-map", "0:v",
    "-map", "1:a",
    "-filter:a", "volume=10.0",
    "-c:v", "libx264",
    "-g", "60",  # Added to set keyframe interval to 2 seconds
    "-preset", "ultrafast",
    "-b:v", "4M",
    "-c:a", "aac",
    "-b:a", "128k",
    "-f", "segment",
    "-segment_time", str(SEGMENT_TIME),
    "-segment_wrap", str(SEGMENT_WRAP),
    "-reset_timestamps", "1",
    os.path.join(EPHEMERAL_SEGMENTS_DIR, "segment_%03d.mp4"),
]

# start FFmpeg in background
ffmpeg_process = subprocess.Popen(ffmpeg_command)

def cleanup_ffmpeg():
    ffmpeg_process.terminate()
    ffmpeg_process.wait()

atexit.register(cleanup_ffmpeg)

def turn_off_switch(entity_id):
    """
    Turns off the specified input_boolean entity in Home Assistant.
    """
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{HA_URL}/api/services/input_boolean/turn_off"
    data = {"entity_id": entity_id}
    response = requests.post(url, json=data, headers=headers)
    return response.ok

def merge_segments_in_ram(num_segments):
    """
    Merges the specified number of finalized segments from /dev/shm.
    A segment is "finalized" if its modification time is older than 'threshold' seconds
    (i.e., it has presumably stopped being written to by FFmpeg).

    If no segments are finalized, raises an exception.
    Otherwise, merges up to the requested number of available segments.

    Returns the path to the merged file in /dev/shm.
    """
    threshold = 1  # seconds
    now = time.time()

    # get all segments, sort by modification time
    all_segments = [f for f in os.listdir(EPHEMERAL_SEGMENTS_DIR) 
                   if f.startswith("segment_") and f.endswith(".mp4")]
    sorted_segments = sorted(all_segments, 
                           key=lambda f: os.path.getmtime(os.path.join(EPHEMERAL_SEGMENTS_DIR, f)))
    finalized_segments = [f for f in sorted_segments 
                         if now - os.path.getmtime(os.path.join(EPHEMERAL_SEGMENTS_DIR, f)) > threshold]

    if not finalized_segments:
        raise Exception("No finalized segments available.")

    # take the last num_segments segments
    segments_to_merge = finalized_segments[-num_segments:]

    # Build a concat list file
    list_file = os.path.join(EPHEMERAL_SEGMENTS_DIR, "segments_list.txt")
    with open(list_file, "w") as f:
        for seg in segments_to_merge:
            seg_path = os.path.join(EPHEMERAL_SEGMENTS_DIR, seg)
            f.write(f"file '{seg_path}'\n")

    # generate a merged filename in RAM
    merged_in_ram = os.path.join(EPHEMERAL_SEGMENTS_DIR, f"merged_{int(time.time())}.mp4")

    merge_command = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        merged_in_ram,
    ]
    subprocess.run(merge_command, check=True)

    return merged_in_ram

def upload_to_google_photos(video_file, duration):
    """
    Upload the given video file to Google Photos with a duration-specific description.
    """
    ensure_valid_token()

    with open(video_file, "rb") as f:
        video_bytes = f.read()

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-File-Name": os.path.basename(video_file),
        "X-Goog-Upload-Protocol": "raw",
    }
    upload_response = requests.post(GOOGLE_PHOTOS_UPLOAD_URL, headers=headers, data=video_bytes)
    upload_response.raise_for_status()
    upload_token = upload_response.text

    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    description = f"duration: {duration}s, captured: {timestamp_str}"

    create_body = {
        "newMediaItems": [
            {
                "description": description,
                "simpleMediaItem": {"uploadToken": upload_token},
            }
        ]
    }
    create_headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-type": "application/json",
    }
    create_response = requests.post(GOOGLE_PHOTOS_CREATE_URL, headers=create_headers, json=create_body)
    create_response.raise_for_status()
    return create_response.json()

@app.route("/trigger", methods=["POST"])
def trigger_action():
    """
    Handles POST requests to merge segments based on requested duration,
    save the clip, upload it, and turn off the corresponding switch.
    """
    data = request.json
    duration = data.get("duration")
    entity_id = data.get("entity_id")

    if not duration or not entity_id:
        return jsonify({"status": "error", "error": "Missing duration or entity_id"}), 400

    try:
        duration = int(duration)
        if duration not in [10, 30, 60, 300]:
            raise ValueError("Invalid duration")

        num_segments = duration // SEGMENT_TIME
        merged_in_ram = merge_segments_in_ram(num_segments)

        final_filename = f"clip_{int(time.time())}_{duration}s.mp4"
        final_path = os.path.join(FINAL_CLIPS_DIR, final_filename)
        shutil.copy2(merged_in_ram, final_path)

        upload_result = upload_to_google_photos(merged_in_ram, duration)
        os.remove(merged_in_ram)
        turn_off_switch(entity_id)

        return jsonify(
            {
                "status": "success",
                "uploaded_clip": final_filename,
                "saved_local_path": final_path,
                "upload_result": upload_result,
            }
        )
    except Exception as e:
        print("Error:", e)
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)