from flask import Flask, jsonify
import atexit
import requests
import subprocess
import time
import os
import shutil

app = Flask(__name__)

HA_URL = "https://clipittang.duckdns.org:8123"
HA_TOKEN = os.getenv("HA_TOKEN")

# Use a RAM-backed directory for segments
EPHEMERAL_SEGMENTS_DIR = "/dev/shm/clip_segments/"
os.makedirs(EPHEMERAL_SEGMENTS_DIR, exist_ok=True)

# Final disk directory for merged clips (one write per trigger)
FINAL_CLIPS_DIR = "/home/mohammadmahfooz/clips/"
os.makedirs(FINAL_CLIPS_DIR, exist_ok=True)

GOOGLE_PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
GOOGLE_PHOTOS_CREATE_URL = (
    "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

# Global variables for token management
ACCESS_TOKEN = None
TOKEN_EXPIRY = 0


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


# Fetch a fresh token when the app starts
refresh_google_photos_token()

# ffmpeg command to record 5-second segments in RAM, overwriting after 12 (~1 minute)
ffmpeg_command = [
    "ffmpeg",
    "-thread_queue_size",
    "512",
    "-f",
    "v4l2",
    "-input_format",
    "mjpeg",
    "-video_size",
    "1920x1080",
    "-framerate",
    "30",
    "-i",
    "/dev/video0",
    "-thread_queue_size",
    "512",
    "-f",
    "pulse",
    "-i",
    "alsa_input.usb-EMEET_HD_Webcam_eMeet_C960_A241108000315080-02.analog-stereo",
    "-map",
    "0:v",
    "-map",
    "1:a",
    "-filter:a",
    "volume=7.5",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-b:v",
    "4M",
    "-c:a",
    "aac",
    "-b:a",
    "128k",
    "-f",
    "segment",
    "-segment_time",
    "5",
    "-segment_wrap",
    "12",
    "-reset_timestamps",
    "1",
    os.path.join(EPHEMERAL_SEGMENTS_DIR, "segment_%03d.mp4"),
]

# Start ffmpeg in the background
ffmpeg_process = subprocess.Popen(ffmpeg_command)


def cleanup_ffmpeg():
    ffmpeg_process.terminate()
    ffmpeg_process.wait()


atexit.register(cleanup_ffmpeg)


def turn_off_switch():
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{HA_URL}/api/services/input_boolean/turn_off"
    data = {"entity_id": "input_boolean.flask_switch"}
    response = requests.post(url, json=data, headers=headers)
    return response.ok


def merge_segments_in_ram():
    """
    Merges up to 12 finalized segments from /dev/shm.
    A segment is "finalized" if its modification time is older than 'threshold' seconds
    (i.e., it has presumably stopped being written to by ffmpeg).

    If no segments are finalized, raises an exception.
    Otherwise, merges however many (up to 12) are available.

    Returns the path to the merged file in /dev/shm.
    """
    threshold = 1
    now = time.time()

    segments = sorted(os.listdir(EPHEMERAL_SEGMENTS_DIR))
    finalized_segments = []

    # Filter only finalized segments (older than threshold)
    for seg in segments:
        if seg.startswith("segment_") and seg.endswith(".mp4"):
            seg_path = os.path.join(EPHEMERAL_SEGMENTS_DIR, seg)
            if now - os.path.getmtime(seg_path) > threshold:
                finalized_segments.append(seg)

    # If no segments are finalized, raise an exception
    if not finalized_segments:
        raise Exception("No finalized segments available.")

    # Take up to the last 12
    segments_to_merge = finalized_segments[-12:]

    # Build a concat list file
    list_file = os.path.join(EPHEMERAL_SEGMENTS_DIR, "segments_list.txt")
    with open(list_file, "w") as f:
        for seg in segments_to_merge:
            seg_path = os.path.join(EPHEMERAL_SEGMENTS_DIR, seg)
            f.write(f"file '{seg_path}'\n")

    # Generate a merged filename in RAM
    merged_in_ram = os.path.join(
        EPHEMERAL_SEGMENTS_DIR, f"merged_{int(time.time())}.mp4"
    )

    # Merge the segments
    merge_command = [
        "ffmpeg",
        "-y",  # Overwrite without prompting
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        merged_in_ram,
    ]
    subprocess.run(merge_command, check=True)

    return merged_in_ram


def upload_to_google_photos(video_file):
    """
    Upload the given video file to Google Photos.
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
    upload_response = requests.post(
        GOOGLE_PHOTOS_UPLOAD_URL, headers=headers, data=video_bytes
    )
    upload_response.raise_for_status()
    upload_token = upload_response.text

    create_body = {
        "newMediaItems": [
            {
                "description": "Last minute capture",
                "simpleMediaItem": {"uploadToken": upload_token},
            }
        ]
    }
    create_headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-type": "application/json",
    }
    create_response = requests.post(
        GOOGLE_PHOTOS_CREATE_URL, headers=create_headers, json=create_body
    )
    create_response.raise_for_status()
    return create_response.json()


@app.route("/trigger", methods=["POST"])
def trigger_action():
    try:
        # Optionally, turn off the switch in Home Assistant
        turn_off_switch()

        # Merge from RAM
        merged_in_ram = merge_segments_in_ram()

        # Copy the merged file from RAM to SD card (single write)
        final_filename = f"clip_{int(time.time())}.mp4"
        final_path = os.path.join(FINAL_CLIPS_DIR, final_filename)
        shutil.copy2(merged_in_ram, final_path)

        # Upload from the final_path on disk or from merged_in_ram in RAM (both have identical content)
        # We'll just upload from merged_in_ram (still in memory)
        upload_result = upload_to_google_photos(merged_in_ram)

        # Remove the merged file from RAM to free memory
        os.remove(merged_in_ram)

        return jsonify(
            {
                "status": "success",
                "uploaded_clip": os.path.basename(final_path),
                "saved_local_path": final_path,
                "upload_result": upload_result,
            }
        )
    except Exception as e:
        print("Error:", e)
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
