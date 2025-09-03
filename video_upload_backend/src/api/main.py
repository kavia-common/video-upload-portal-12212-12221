import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Constants
MAX_VIDEO_SIZE_BYTES = 500 * 1024 * 1024  # 500MB
UPLOAD_DIR = Path("/upload")  # As per requirements


def ensure_upload_dir_exists() -> None:
    """
    Ensure the upload directory exists, create with safe permissions if it doesn't.
    """
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # Set directory permissions to rwx for user only (best-effort, may be limited by OS)
        try:
            os.chmod(UPLOAD_DIR, 0o700)
        except PermissionError:
            # If we don't have permission to chmod, ignore silently.
            pass
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to prepare upload directory: {exc}",
        )


# Pydantic models for API documentation
class HealthResponse(BaseModel):
    message: str = Field(..., description="Health check message")


class UploadSuccessResponse(BaseModel):
    filename: str = Field(..., description="Saved filename")
    size_bytes: int = Field(..., description="Size of the uploaded file in bytes")
    message: str = Field(..., description="Success message")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Error description")


app = FastAPI(
    title="Video Upload Backend",
    description=(
        "A FastAPI service to upload video files up to 500 MB. "
        "Files are saved under the /upload directory."
    ),
    version="1.0.0",
    contact={"name": "Video Upload Service"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {"name": "Health", "description": "Service health and readiness checks"},
        {"name": "Upload", "description": "Endpoints for uploading video files"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# PUBLIC_INTERFACE
@app.get(
    "/",
    response_model=HealthResponse,
    summary="Health Check",
    tags=["Health"],
)
def health_check() -> HealthResponse:
    """
    Health check endpoint.

    Returns:
        HealthResponse: A simple JSON indicating service health.
    """
    return HealthResponse(message="Healthy")


def _validate_content_length(request: Request) -> Optional[int]:
    """
    Validate 'Content-Length' header if provided to pre-check size.

    Returns:
        Optional[int]: Content length if present and valid.
    Raises:
        HTTPException: If Content-Length indicates a payload over the limit or invalid.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return None  # Cannot pre-validate; will check during stream write
    try:
        size = int(content_length)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length header.",
        )

    if size > MAX_VIDEO_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum allowed size is {MAX_VIDEO_SIZE_BYTES} bytes (500MB).",
        )
    return size


# PUBLIC_INTERFACE
@app.post(
    "/upload",
    response_model=UploadSuccessResponse,
    responses={
        200: {"model": UploadSuccessResponse, "description": "Upload successful"},
        400: {"model": ErrorResponse, "description": "Bad Request"},
        413: {"model": ErrorResponse, "description": "Payload Too Large"},
        415: {"model": ErrorResponse, "description": "Unsupported Media Type"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
    summary="Upload a video file",
    description=(
        "Accepts a video file upload up to 500MB and saves it under the /upload directory. "
        "If the directory does not exist, it is created automatically. "
        "The endpoint enforces the size limit using Content-Length (if provided) and by "
        "streaming the file to disk while checking the cumulative size."
    ),
    tags=["Upload"],
)
async def upload_video(
    request: Request,
    _: Optional[int] = Depends(_validate_content_length),
    file: UploadFile = File(
        ...,
        description="The video file to upload. Maximum size: 500MB.",
    ),
) -> UploadSuccessResponse:
    """
    Upload a single video file and save to /upload.

    Parameters:
        request (Request): The incoming request, used to inspect headers for size checks.
        file (UploadFile): The uploaded file.

    Returns:
        UploadSuccessResponse: Information about the saved file.

    Raises:
        HTTPException: If file is missing, too large, or cannot be saved.
    """
    ensure_upload_dir_exists()

    # Basic filename sanitization: keep base name only to prevent path traversal
    safe_filename = os.path.basename(file.filename or "").strip()

    if not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required.",
        )

    # Optional: rudimentary content type check - allow common video mime types but not enforce strictly
    # Clients may set generic types; we won't block unless it's clearly not a file
    if file.content_type is None:
        # Some clients may not set this; proceed cautiously
        pass

    destination_path = UPLOAD_DIR / safe_filename

    # Stream the file to disk in chunks and enforce size limit.
    total_written = 0
    chunk_size = 1024 * 1024  # 1MB chunks

    try:
        with destination_path.open("wb") as out_file:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > MAX_VIDEO_SIZE_BYTES:
                    # Close and remove partial file
                    out_file.close()
                    try:
                        destination_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File too large. Maximum allowed size is {MAX_VIDEO_SIZE_BYTES} bytes (500MB).",
                    )
                out_file.write(chunk)
    except HTTPException:
        # Re-raise explicit HTTP errors
        raise
    except Exception as exc:
        # Clean up partial file on unexpected errors
        try:
            destination_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        )
    finally:
        await file.close()

    return UploadSuccessResponse(
        filename=safe_filename,
        size_bytes=total_written,
        message="Upload successful",
    )


# Global exception handler to ensure consistent JSON error responses
@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException):
    """
    Return JSON response for HTTPException with consistent schema.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail if isinstance(exc.detail, str) else str(exc.detail)},
    )
