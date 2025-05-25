import os
import uuid
import shutil
import requests
import logging
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
import json
from pydantic import BaseModel
from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeAudioClip,
)
from upload_google import upload_video

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create necessary directories
TEMP_DIR = "temp"
OUTPUT_DIR = "output"

# Get absolute paths
TEMP_DIR = os.path.abspath(TEMP_DIR)
OUTPUT_DIR = os.path.abspath(OUTPUT_DIR)

# Create directories
logger.info(f"Creating directories: TEMP_DIR={TEMP_DIR}, OUTPUT_DIR={OUTPUT_DIR}")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
logger.info(f"Directories created successfully")

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MergeRequest(BaseModel):
    video_urls: List[str]
    background_audio_url: Optional[str] = None
    background_volume: Optional[float] = 0.5
    upload_to_drive: Optional[bool] = False


def download_file(url, output_path):
    """Download a file from URL to the specified path with validation"""
    try:
        logger.info(f"Downloading file from {url} to {output_path}")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()  # Raise exception for HTTP errors
        
        content_length = response.headers.get('content-length')
        if content_length is None:
            logger.warning(f"Content-Length header missing for {url}")
        else:
            logger.info(f"Expected file size: {content_length} bytes")
        
        with open(output_path, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:  # Filter out keep-alive chunks
                    f.write(chunk)
                    downloaded_size += len(chunk)
        
        # Verify file was downloaded and has content
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            actual_size = os.path.getsize(output_path)
            logger.info(f"Downloaded file size: {actual_size} bytes")
            
            # Verify file size matches content-length if available
            if content_length and int(content_length) > 0:
                if abs(int(content_length) - actual_size) > 100:  # Allow small difference
                    logger.warning(f"File size mismatch: expected {content_length}, got {actual_size}")
            
            # Basic validation for video files
            if output_path.endswith(('.mp4', '.avi', '.mov', '.mkv')):
                try:
                    import subprocess
                    # Use ffprobe to check if file is a valid video
                    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', 
                           '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', output_path]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if 'video' not in result.stdout.strip():
                        logger.error(f"Downloaded file is not a valid video: {output_path}")
                        return False
                except Exception as e:
                    logger.warning(f"Could not validate video file {output_path}: {str(e)}")
            
            return True
        else:
            logger.error(f"Downloaded file is empty or does not exist: {output_path}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading file from {url}: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error downloading file from {url}: {str(e)}")
        return False


def cleanup_files(file_paths):
    """Clean up temporary files with robust error handling"""
    logger.info(f"Cleaning up files: {file_paths}")
    for file_path in file_paths:
        if os.path.exists(file_path):
            # Try multiple times in case of locked files
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    if os.path.isdir(file_path):
                        logger.info(
                            f"Removing directory: {file_path} (attempt {attempt+1}/{max_attempts})"
                        )
                        # Use ignore_errors to handle permission issues
                        shutil.rmtree(file_path, ignore_errors=True)
                    else:
                        logger.info(
                            f"Removing file: {file_path} (attempt {attempt+1}/{max_attempts})"
                        )
                        # Check if file is still in use
                        try:
                            # Try to open the file to see if it's locked
                            with open(file_path, "a"):
                                pass
                        except IOError:
                            logger.warning(f"File appears to be locked: {file_path}")
                            # Wait a moment before retrying
                            import time

                            time.sleep(1)
                            continue

                        # Try to remove the file
                        os.remove(file_path)

                    # If we get here without exception, removal was successful
                    logger.info(f"Successfully removed: {file_path}")
                    break
                except Exception as e:
                    logger.error(
                        f"Error removing {file_path} (attempt {attempt+1}/{max_attempts}): {str(e)}"
                    )
                    if attempt < max_attempts - 1:
                        # Wait before retrying
                        import time

                        time.sleep(1)
                    else:
                        logger.error(
                            f"Failed to remove {file_path} after {max_attempts} attempts"
                        )
        else:
            logger.info(f"File or directory does not exist, skipping: {file_path}")


def cleanup_all_temp_output():
    """Clean up all files in temp and output directories with robust error handling"""
    logger.info("Cleaning up all files in temp and output directories")

    # Get all files and directories to clean up
    items_to_clean = []

    # Add temp directory items
    if os.path.exists(TEMP_DIR):
        for item in os.listdir(TEMP_DIR):
            items_to_clean.append(os.path.join(TEMP_DIR, item))

    # Add output directory items
    if os.path.exists(OUTPUT_DIR):
        for item in os.listdir(OUTPUT_DIR):
            items_to_clean.append(os.path.join(OUTPUT_DIR, item))

    # Use our improved cleanup_files function to handle the cleanup
    if items_to_clean:
        logger.info(f"Found {len(items_to_clean)} items to clean up")
        cleanup_files(items_to_clean)
    else:
        logger.info("No files found to clean up")

    # Verify cleanup was successful
    temp_files_remaining = (
        os.listdir(TEMP_DIR)
        if os.path.exists(TEMP_DIR) and os.path.isdir(TEMP_DIR)
        else []
    )
    output_files_remaining = (
        os.listdir(OUTPUT_DIR)
        if os.path.exists(OUTPUT_DIR) and os.path.isdir(OUTPUT_DIR)
        else []
    )

    if not temp_files_remaining and not output_files_remaining:
        logger.info("All directories successfully cleaned")
    else:
        logger.warning(
            f"Some files could not be removed: {len(temp_files_remaining)} in temp, {len(output_files_remaining)} in output"
        )

    # Ensure directories exist for future use
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


@app.post("/merge")
async def merge_videos(
    background_tasks: BackgroundTasks,
    merge_request: str = Form(...),
    narration_file: UploadFile = File(None),
    upload_to_drive: str = Form(None),
):
    logger.info("Merge endpoint accessed")
    # Parse the merge request JSON
    logger.info(f"Received merge_request: {merge_request}")
    logger.info(f"Received upload_to_drive: {upload_to_drive}")
    request_data = json.loads(merge_request)
    merge_data = MergeRequest(**request_data)

    # Override upload_to_drive from form data if provided
    if upload_to_drive is not None:
        # Convert string 'true'/'false' to boolean
        merge_data.upload_to_drive = upload_to_drive.lower() == "true"
        logger.info(
            f"Override upload_to_drive from form data: {merge_data.upload_to_drive}"
        )

    logger.info(f"Parsed merge data: {merge_data}")

    # Create a unique ID for this request
    request_id = str(uuid.uuid4())
    logger.info(f"Generated request ID: {request_id}")
    request_temp_dir = os.path.join(TEMP_DIR, request_id)
    logger.info(f"Creating temp directory: {request_temp_dir}")
    os.makedirs(request_temp_dir, exist_ok=True)

    # List to track files for cleanup - only include temporary directory
    files_to_cleanup = [request_temp_dir]

    try:
        # Download videos
        video_paths = []
        for i, video_url in enumerate(merge_data.video_urls):
            video_path = os.path.join(request_temp_dir, f"video_{i}.mp4")
            logger.info(f"Downloading video {i+1}/{len(merge_data.video_urls)} from {video_url}")
            if download_file(video_url, video_path):
                video_paths.append(video_path)
            else:
                logger.error(f"Failed to download video from {video_url}")
                return {"error": f"Failed to download video {i+1} from {video_url}"}

        if not video_paths:
            logger.error("No videos were successfully downloaded")
            return {"error": "No videos were successfully downloaded"}
        
        # Verify all video files exist and have content
        for i, path in enumerate(video_paths):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                logger.error(f"Video file {path} is missing or empty")
                return {"error": f"Video file {i+1} is missing or empty"}

        # Load video clips with error handling
        video_clips = []
        for i, path in enumerate(video_paths):
            try:
                logger.info(f"Loading video clip {i+1} from {path}")
                clip = VideoFileClip(path)
                # Basic validation of the clip
                if clip.duration <= 0 or clip.size[0] <= 0 or clip.size[1] <= 0:
                    logger.error(f"Invalid video dimensions or duration in {path}")
                    return {"error": f"Video {i+1} has invalid dimensions or duration"}
                video_clips.append(clip)
            except Exception as e:
                logger.error(f"Error loading video {i+1} from {path}: {str(e)}")
                return {"error": f"Error loading video {i+1}: {str(e)}"}

        # Concatenate videos with error handling
        try:
            if not video_clips:
                logger.error("No valid video clips to concatenate")
                return {"error": "No valid video clips to concatenate"}
                
            logger.info(f"Concatenating {len(video_clips)} video clips")
            final_clip = concatenate_videoclips(video_clips)
            
            # Validate the final clip
            if final_clip.duration <= 0 or final_clip.size[0] <= 0 or final_clip.size[1] <= 0:
                logger.error("Concatenated clip has invalid dimensions or duration")
                return {"error": "Failed to create valid concatenated video"}
                
            logger.info(f"Successfully created concatenated clip with duration {final_clip.duration}s")
        except Exception as e:
            logger.error(f"Error concatenating video clips: {str(e)}")
            return {"error": f"Error concatenating video clips: {str(e)}"}

        # Process audio files
        audio_tracks = []

        # Background audio
        if merge_data.background_audio_url:
            try:
                bg_audio_path = os.path.join(request_temp_dir, "background.mp3")
                logger.info(f"Downloading background audio from {merge_data.background_audio_url}")
                if download_file(merge_data.background_audio_url, bg_audio_path):
                    # Verify audio file
                    if not os.path.exists(bg_audio_path) or os.path.getsize(bg_audio_path) == 0:
                        logger.error(f"Background audio file is missing or empty")
                        return {"error": "Background audio file is missing or empty"}
                    
                    logger.info(f"Loading background audio from {bg_audio_path}")
                    bg_audio = AudioFileClip(bg_audio_path)
                    
                    # Validate audio clip
                    if bg_audio.duration <= 0:
                        logger.error(f"Invalid background audio duration: {bg_audio.duration}")
                        return {"error": "Background audio has invalid duration"}
                    
                    logger.info(f"Background audio duration: {bg_audio.duration}s, video duration: {final_clip.duration}s")
                    
                    # Loop background audio if it's shorter than the final video
                    if bg_audio.duration < final_clip.duration:
                        logger.info(f"Looping background audio to match video duration")
                        bg_audio = bg_audio.loop(duration=final_clip.duration)
                    else:
                        # Trim background audio if it's longer than the final video
                        logger.info(f"Trimming background audio to match video duration")
                        bg_audio = bg_audio.subclip(0, final_clip.duration)

                    # Set volume for background audio
                    logger.info(f"Setting background audio volume to {merge_data.background_volume}")
                    bg_audio = bg_audio.volumex(merge_data.background_volume)
                    audio_tracks.append(bg_audio)
                else:
                    logger.error(f"Failed to download background audio from {merge_data.background_audio_url}")
                    return {"error": "Failed to download background audio"}
            except Exception as e:
                logger.error(f"Error processing background audio: {str(e)}")
                return {"error": f"Error processing background audio: {str(e)}"}

        # Narration audio
        if narration_file:
            narration_path = os.path.join(request_temp_dir, "narration.mp3")
            with open(narration_path, "wb") as buffer:
                shutil.copyfileobj(narration_file.file, buffer)

            narration_audio = AudioFileClip(narration_path)

            # Trim narration if it's longer than the final video
            if narration_audio.duration > final_clip.duration:
                narration_audio = narration_audio.subclip(0, final_clip.duration)

            audio_tracks.append(narration_audio)

        # Combine audio tracks with video
        if audio_tracks:
            final_audio = CompositeAudioClip(audio_tracks)
            final_clip = final_clip.set_audio(final_audio)

        # Export the final clip
        output_filename = f"merged_video_{request_id}.mp4"
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        logger.info(f"Saving output file to: {output_path}")

        # Ensure output directory exists
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Check if output directory is writable
        if not os.access(os.path.dirname(output_path), os.W_OK):
            logger.error(
                f"Output directory is not writable: {os.path.dirname(output_path)}"
            )
            return {"error": "Output directory is not writable"}

        try:
            logger.info(f"Writing final video to {output_path} with codec libx264 and audio codec aac")
            # Add progress logging callback
            def write_log(t):
                logger.info(f"Writing video: {int(t*100)}% complete")
                
            final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", 
                                     logger=None, verbose=False, progress_bar=False, 
                                     callback=write_log)
            logger.info(f"Successfully wrote video file to: {output_path}")

            # Verify file was created
            if os.path.exists(output_path):
                logger.info(
                    f"Verified file exists: {output_path}, size: {os.path.getsize(output_path)} bytes"
                )

                # Create a permanent copy with a fixed name
                permanent_output_path = os.path.join(
                    OUTPUT_DIR, "final_merged_video.mp4"
                )
                logger.info(f"Creating permanent copy at: {permanent_output_path}")
                try:
                    shutil.copy2(output_path, permanent_output_path)
                    logger.info(f"Permanent copy created successfully")

                    # Verify permanent copy was created
                    if os.path.exists(permanent_output_path):
                        logger.info(
                            f"Verified permanent copy exists: {permanent_output_path}, size: {os.path.getsize(permanent_output_path)} bytes"
                        )
                    else:
                        logger.error(
                            f"Failed to create permanent copy at: {permanent_output_path}"
                        )
                except Exception as copy_error:
                    logger.error(f"Error creating permanent copy: {str(copy_error)}")
            else:
                logger.error(f"File was not created: {output_path}")
                return {"error": "Failed to create output file"}
        except Exception as e:
            logger.error(f"Error writing video file: {str(e)}")
            return {"error": f"Error writing video file: {str(e)}"}

        # Close all clips to release resources
        logger.info("Closing all video and audio clips to release resources")
        try:
            final_clip.close()
            for i, clip in enumerate(video_clips):
                try:
                    clip.close()
                except Exception as e:
                    logger.warning(f"Error closing video clip {i}: {str(e)}")
            for i, track in enumerate(audio_tracks):
                try:
                    track.close()
                except Exception as e:
                    logger.warning(f"Error closing audio track {i}: {str(e)}")
        except Exception as e:
            logger.warning(f"Error during resource cleanup: {str(e)}")
            # Continue execution even if cleanup fails

        # Upload to Google Drive if requested
        drive_upload_result = None
        if merge_data.upload_to_drive:
            try:
                logger.info(f"Uploading video to Google Drive: {output_path}")
                drive_upload_result = upload_video(output_path)
                logger.info(
                    f"Successfully uploaded to Google Drive: {drive_upload_result}"
                )

                # We'll clean up files after streaming, not immediately after upload
                logger.info(
                    "Upload successful, but will clean up files after streaming"
                )

                # We'll verify the file still exists before streaming
                if not os.path.exists(output_path):
                    logger.error(
                        f"Output file no longer exists after upload: {output_path}"
                    )
                    # At this point, permanent_output_path might not be defined yet
                    # We'll check for any permanent copy in the output directory
                    permanent_files = [
                        f
                        for f in os.listdir(OUTPUT_DIR)
                        if f.startswith("permanent_copy_")
                        or f == "final_merged_video.mp4"
                    ]
                    if permanent_files:
                        recovery_file = os.path.join(OUTPUT_DIR, permanent_files[0])
                        logger.info(
                            f"Restoring output file from permanent copy: {recovery_file}"
                        )
                        shutil.copy2(recovery_file, output_path)

                # Verify files exist
                temp_files_remaining = (
                    os.listdir(TEMP_DIR) if os.path.exists(TEMP_DIR) else []
                )
                output_files_remaining = (
                    os.listdir(OUTPUT_DIR) if os.path.exists(OUTPUT_DIR) else []
                )

                logger.info(
                    f"Cleanup verification - Temp files remaining: {len(temp_files_remaining)}, Output files remaining: {len(output_files_remaining)}"
                )

                # If files still remain, try one more time with forced cleanup
                if temp_files_remaining or output_files_remaining:
                    logger.warning(
                        "Some files remained after cleanup, attempting forced cleanup"
                    )
                    try:
                        # Force remove all files with more aggressive approach
                        for dir_path in [TEMP_DIR, OUTPUT_DIR]:
                            if os.path.exists(dir_path):
                                for item in os.listdir(dir_path):
                                    item_path = os.path.join(dir_path, item)
                                    try:
                                        if os.path.isdir(item_path):
                                            shutil.rmtree(item_path, ignore_errors=True)
                                        else:
                                            os.remove(item_path)
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to remove {item_path}: {str(e)}"
                                        )
                    except Exception as cleanup_error:
                        logger.error(
                            f"Error during forced cleanup: {str(cleanup_error)}"
                        )

                    # Final verification
                    temp_files_remaining = (
                        os.listdir(TEMP_DIR) if os.path.exists(TEMP_DIR) else []
                    )
                    output_files_remaining = (
                        os.listdir(OUTPUT_DIR) if os.path.exists(OUTPUT_DIR) else []
                    )
                    logger.info(
                        f"Final cleanup verification - Temp files remaining: {len(temp_files_remaining)}, Output files remaining: {len(output_files_remaining)}"
                    )

                # Re-create empty directories to ensure they exist for future requests
                os.makedirs(TEMP_DIR, exist_ok=True)
                os.makedirs(OUTPUT_DIR, exist_ok=True)
            except Exception as e:
                logger.error(f"Error uploading to Google Drive: {str(e)}")
                drive_upload_result = {"error": str(e)}

        # Create a unique filename for the permanent copy
        permanent_filename = f"permanent_copy_{request_id}.mp4"
        permanent_output_path = os.path.join(OUTPUT_DIR, permanent_filename)

        # Create the permanent copy immediately after the main file is created
        logger.info(f"Creating permanent copy at: {permanent_output_path}")
        try:
            shutil.copy2(output_path, permanent_output_path)
            logger.info(
                f"Permanent copy created successfully at: {permanent_output_path}"
            )
        except Exception as copy_error:
            logger.error(f"Error creating permanent copy: {str(copy_error)}")
            permanent_output_path = None  # Mark as not available

        # Return the video as a downloadable file
        def iterfile():
            # Verify file exists before streaming
            if not os.path.exists(output_path):
                logger.error(f"File does not exist before streaming: {output_path}")
                # Try to recover from permanent copy if available
                if permanent_output_path and os.path.exists(permanent_output_path):
                    logger.info(
                        f"Recovering output file from permanent copy: {permanent_output_path}"
                    )
                    try:
                        shutil.copy2(permanent_output_path, output_path)
                        logger.info(f"Successfully recovered file from permanent copy")
                    except Exception as copy_error:
                        logger.error(f"Failed to recover file: {str(copy_error)}")
                        yield b"Error: File was deleted before streaming could begin"
                        return
                else:
                    logger.error("No permanent copy available for recovery")
                    yield b"Error: File was deleted before streaming could begin"
                    return

            logger.info(f"Streaming file: {output_path}")
            try:
                with open(output_path, "rb") as file:
                    yield from file
                logger.info(f"Finished streaming file: {output_path}")
            except Exception as e:
                logger.error(f"Error streaming file: {str(e)}")

            # Always clean up temporary files and the output file AFTER streaming is complete
            # Clean up all files if upload_to_drive was True, otherwise just clean specific files
            if merge_data.upload_to_drive:
                logger.info(
                    "Upload was requested, cleaning up all temp and output files"
                )
                background_tasks.add_task(cleanup_all_temp_output)
            else:
                logger.info(f"Scheduling cleanup of specific files")
                # Add permanent_output_path to cleanup list if it exists
                files_to_clean = [request_temp_dir, output_path]
                if os.path.exists(permanent_output_path):
                    files_to_clean.append(permanent_output_path)
                background_tasks.add_task(cleanup_files, files_to_clean)

        response_headers = {
            "Content-Disposition": f"attachment; filename={output_filename}"
        }

        # Add Drive upload info to headers if available
        if drive_upload_result:
            # Check if it's a successful response (should be a dict with id)
            if isinstance(drive_upload_result, dict) and "id" in drive_upload_result:
                response_headers["X-Drive-Upload-Id"] = drive_upload_result.get(
                    "id", ""
                )
                response_headers["X-Drive-Upload-Link"] = drive_upload_result.get(
                    "webViewLink", ""
                )
                response_headers["X-Drive-Upload-Status"] = "success"
            # If it's an error dict
            elif (
                isinstance(drive_upload_result, dict) and "error" in drive_upload_result
            ):
                response_headers["X-Drive-Upload-Status"] = "failed"
                response_headers["X-Drive-Upload-Error"] = drive_upload_result.get(
                    "error", ""
                )

        return drive_upload_result

    except Exception as e:
        # Clean up all files created during this request in case of error
        logger.error(f"Error in merge_videos: {str(e)}")

        # Create a list of all files to clean up
        files_to_cleanup = [request_temp_dir]

        # Add output path if it exists
        if "output_path" in locals() and os.path.exists(output_path):
            files_to_cleanup.append(output_path)

        # Add permanent output path if it exists
        if "permanent_output_path" in locals() and os.path.exists(
            permanent_output_path
        ):
            files_to_cleanup.append(permanent_output_path)

        # Clean up all files immediately, not as a background task
        cleanup_files(files_to_cleanup)

        return {"error": str(e)}


@app.get("/")
async def root():
    logger.info("Root endpoint accessed")

    # Check if output directory exists and is writable
    output_dir_exists = os.path.exists(OUTPUT_DIR)
    output_dir_writable = os.access(OUTPUT_DIR, os.W_OK) if output_dir_exists else False

    # Check if temp directory exists and is writable
    temp_dir_exists = os.path.exists(TEMP_DIR)
    temp_dir_writable = os.access(TEMP_DIR, os.W_OK) if temp_dir_exists else False

    return {
        "message": "Video Merger API is running. Use /merge endpoint to merge videos.",
        "status": "ok",
        "directories": {
            "output_dir": {
                "path": OUTPUT_DIR,
                "exists": output_dir_exists,
                "writable": output_dir_writable,
            },
            "temp_dir": {
                "path": TEMP_DIR,
                "exists": temp_dir_exists,
                "writable": temp_dir_writable,
            },
        },
    }


@app.get("/check-directories")
async def check_directories():
    """Check and create output and temp directories"""
    logger.info("Check directories endpoint accessed")

    # Ensure directories exist
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Check if directories are writable
    temp_dir_writable = os.access(TEMP_DIR, os.W_OK)
    output_dir_writable = os.access(OUTPUT_DIR, os.W_OK)

    # Create a test file in the output directory to verify write permissions
    test_file_path = os.path.join(OUTPUT_DIR, "test_write.txt")
    test_file_success = False
    try:
        with open(test_file_path, "w") as f:
            f.write("Test write access")
        test_file_success = True
        # Clean up test file
        os.remove(test_file_path)
    except Exception as e:
        logger.error(f"Failed to write test file: {str(e)}")

    return {
        "status": "ok",
        "directories": {
            "temp_dir": {
                "path": TEMP_DIR,
                "exists": os.path.exists(TEMP_DIR),
                "writable": temp_dir_writable,
            },
            "output_dir": {
                "path": OUTPUT_DIR,
                "exists": os.path.exists(OUTPUT_DIR),
                "writable": output_dir_writable,
                "test_write_success": test_file_success,
            },
        },
    }


@app.get("/list-videos")
async def list_videos():
    """Information about video streaming"""
    logger.info("List videos endpoint accessed")

    return {
        "message": "Videos are not being saved to the server. They are streamed directly to the client upon creation.",
        "videos": [],
    }


@app.get("/verify-cleanup")
async def verify_cleanup(run_cleanup: bool = False):
    """Verify that cleanup process is working correctly"""
    logger.info(f"Verify cleanup endpoint accessed with run_cleanup={run_cleanup}")

    # Check temp directory
    temp_files = []
    if os.path.exists(TEMP_DIR) and os.path.isdir(TEMP_DIR):
        temp_files = os.listdir(TEMP_DIR)

    # Check output directory
    output_files = []
    if os.path.exists(OUTPUT_DIR) and os.path.isdir(OUTPUT_DIR):
        output_files = os.listdir(OUTPUT_DIR)

    # Run cleanup if requested via query parameter
    cleanup_requested = run_cleanup
    if cleanup_requested:
        logger.info("Running cleanup as requested")
        cleanup_all_temp_output()

        # Check directories again after cleanup
        temp_files_after = []
        if os.path.exists(TEMP_DIR) and os.path.isdir(TEMP_DIR):
            temp_files_after = os.listdir(TEMP_DIR)

        output_files_after = []
        if os.path.exists(OUTPUT_DIR) and os.path.isdir(OUTPUT_DIR):
            output_files_after = os.listdir(OUTPUT_DIR)

        return {
            "status": "cleanup_performed",
            "before_cleanup": {
                "temp_directory": {
                    "path": TEMP_DIR,
                    "file_count": len(temp_files),
                    "files": temp_files,
                },
                "output_directory": {
                    "path": OUTPUT_DIR,
                    "file_count": len(output_files),
                    "files": output_files,
                },
            },
            "after_cleanup": {
                "temp_directory": {
                    "path": TEMP_DIR,
                    "file_count": len(temp_files_after),
                    "files": temp_files_after,
                },
                "output_directory": {
                    "path": OUTPUT_DIR,
                    "file_count": len(output_files_after),
                    "files": output_files_after,
                },
            },
        }

    # Just return current state without cleanup
    return {
        "status": "current_state",
        "temp_directory": {
            "path": TEMP_DIR,
            "file_count": len(temp_files),
            "files": temp_files,
        },
        "output_directory": {
            "path": OUTPUT_DIR,
            "file_count": len(output_files),
            "files": output_files,
        },
        "message": "To run cleanup, add ?run_cleanup=true to the URL",
        "example": "/verify-cleanup?run_cleanup=true",
    }


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting server on port 8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
