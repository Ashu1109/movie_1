# pip install google-api-python-client
from googleapiclient.discovery import build
from google.oauth2 import service_account
import os

SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = "service_account.json"
PARENT_FOLDER_ID = "16tw9ifrTCLGC8RxioSh02_0TNngC4A_p"


def authenticate():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return creds


def upload_video(file_path):
    """Upload a video file to Google Drive and return the file information"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        # Validate file exists
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        # Validate file size
        file_size = os.path.getsize(file_path)
        logger.info(f"Uploading file: {file_path}, size: {file_size} bytes")
        
        # Check if file is empty
        if file_size == 0:
            error_msg = f"File is empty: {file_path}"
            logger.error(error_msg)
            return {"error": error_msg}
            
        # Authenticate with Google Drive
        try:
            creds = authenticate()
            service = build("drive", "v3", credentials=creds)
        except Exception as auth_error:
            error_msg = f"Authentication error: {str(auth_error)}"
            logger.error(error_msg)
            return {"error": error_msg}

        # Prepare file metadata
        file_metadata = {
            "name": os.path.basename(file_path), 
            "parents": [PARENT_FOLDER_ID]
        }

        # Use MediaFileUpload for proper file upload
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        
        # Upload file to Google Drive
        logger.info(f"Starting upload to Google Drive: {os.path.basename(file_path)}")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name,webViewLink'
        ).execute()

        logger.info(f"File uploaded successfully: {file}")
        return file
    except Exception as e:
        error_msg = f"Error uploading file to Google Drive: {str(e)}"
        logger.error(error_msg)
        return {"error": error_msg}


# Example usage - uncomment to test
# if __name__ == "__main__":
#     upload_video("merged_video_c9dbdb43-510a-49e7-85ff-ee736f05938d.mp4")
