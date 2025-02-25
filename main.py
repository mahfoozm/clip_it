from flask import Flask, jsonify
import atexit
import requests
import subprocess
import time
import os

app = Flask(__name__)

HA_URL = "https://clipittang.duckdns.org:8123"
HA_TOKEN = os.getenv("HA_TOKEN")

SEGMENTS_DIR = "/home/mohammadmahfooz/clips/"

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

ffmpeg_command = [
    "ffmpeg",
    "-thread_queue_size",
    "512",  # Increase queue for video input
    "-f",
    "v4l2",
    "-input_format",
    "mjpeg",  # Request MJPEG from the webcam
    "-video_size",
    "1920x1080",  # 1080p resolution
    "-framerate",
    "30",  # 30 fps
    "-i",
    "/dev/video0",  # Video input device
    "-thread_queue_size",
    "512",  # Increase queue for audio input
    "-f",
    "pulse",
    "-i",
    "alsa_input.usb-EMEET_HD_Webcam_eMeet_C960_A241108000315080-02.analog-stereo",  # Audio input via PulseAudio
    "-map",
    "0:v",  # Map video stream
    "-map",
    "1:a",  # Map audio stream
    "-filter:a",
    "volume=7.5",  # Boost audio volume
    "-c:v",
    "libx264",  # Use software encoder
    "-preset",
    "ultrafast",  # Low-latency preset
    "-b:v",
    "4M",  # Set video bitrate (4 Mbps; adjust as needed)
    "-c:a",
    "aac",  # Encode audio using AAC
    "-b:a",
    "128k",  # Set audio bitrate
    "-f",
    "segment",  # Segment muxer
    "-segment_time",
    "5",  # 5-second segments
    "-segment_wrap",
    "12",  # Cycle through 12 segments (~1 minute total)
    os.path.join(SEGMENTS_DIR, "segment_%03d.mp4"),
]


# Start ffmpeg as a background process
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


def merge_segments():
    """
    Merges the last 12 finalized segments (each 5 seconds) into a single clip.
    Only segments not modified within the last 5 seconds are included.
    """
    threshold = 5  # seconds
    now = time.time()

    segments = sorted(os.listdir(SEGMENTS_DIR))
    finalized_segments = []
    for seg in segments:
        if seg.startswith("segment_") and seg.endswith(".mp4"):
            seg_path = os.path.join(SEGMENTS_DIR, seg)
            if now - os.path.getmtime(seg_path) > threshold:
                finalized_segments.append(seg)

    if not finalized_segments:
        raise Exception("No finalized segments available for merging.")

    # For a 1-minute clip, use the last 12 segments (5 sec each)
    segments_to_merge = finalized_segments[-12:]

    list_file = os.path.join(SEGMENTS_DIR, "segments_list.txt")
    with open(list_file, "w") as f:
        for seg in segments_to_merge:
            seg_path = os.path.join(SEGMENTS_DIR, seg)
            f.write(f"file '{seg_path}'\n")

    # Generate a unique output filename using a timestamp
    output_video = os.path.join(SEGMENTS_DIR, f"clip_{int(time.time())}.mp4")
    merge_command = [
        "ffmpeg",
        "-y",  # Overwrite without prompting.
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        output_video,
    ]
    subprocess.run(merge_command, check=True)
    return output_video


def upload_to_google_photos(video_file):
    """
    Uploads the given video file to Google Photos.
    """
    ensure_valid_token()  # Refresh token if expired

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
        merged_video = merge_segments()
        upload_result = upload_to_google_photos(merged_video)
        return jsonify(
            {
                "status": "success",
                "uploaded_clip": os.path.basename(merged_video),
                "upload_result": upload_result,
            }
        )
    except Exception as e:
        print("Error:", e)
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
