import os
import uuid
import io
import time
import json
from datetime import datetime, timedelta # Import timedelta for signed URL expiration
from typing import Optional, List, Dict

from PIL import Image # Using direct import PIL.Image
from django.conf import settings
from django.utils.text import slugify
from google.cloud import storage
from google import genai
from google.genai import types
from google.genai.types import (
    RawReferenceImage,
    MaskReferenceImage,
    MaskReferenceConfig,
    EditImageConfig,
)
from .models import VideoGenerationJob # Import the VideoGenerationJob model

# --- Google Gen AI Client & Model IDs ---
try:
    client = genai.Client(
        vertexai=True,
        project=settings.GOOGLE_PROJECT_ID,
        location=settings.GOOGLE_LOCATION,
    )
except Exception as e:
    print(f"Error initializing Google Gen AI Client: {e}")
    client = None

IMAGEN3_GEN_MODEL = "imagen-3.0-generate-002"
IMAGEN3_EDIT_MODEL = "imagen-3.0-capability-001"
VEO2_VIDEO_MODEL = "veo-2.0-generate-001" # Ensure this is the correct SDK model ID

# --- GCS Helper Functions ---

def _get_gcs_client():
    """Initializes and returns a GCS client."""
    try:
        # Assumes Application Default Credentials (ADC) are set up
        return storage.Client(project=settings.GOOGLE_PROJECT_ID)
    except Exception as e:
        print(f"Error initializing GCS client: {e}")
        raise

def _generate_gcs_object_name(
    base_prefix: str, # e.g., settings.GCS_GENERATED_IMAGES_PREFIX
    original_filename: Optional[str] = None,
    prompt_text: Optional[str] = None,
    extension: str = "png"
) -> str:
    """Generates a more descriptive GCS object name with timestamp and unique ID."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = str(uuid.uuid4())[:8] # Short UUID for uniqueness

    name_part = "media"
    if original_filename:
        name_part = slugify(os.path.splitext(original_filename)[0])[:50]
    elif prompt_text:
        name_part = slugify(prompt_text)[:50]

    if not name_part:
        name_part = "unnamed_media"

    # Ensure base_prefix ends with a slash if it's not empty
    if base_prefix and not base_prefix.endswith('/'):
        base_prefix += '/'
        
    return f"{base_prefix}{name_part}_{timestamp}_{unique_id}.{extension}"


def _get_signed_gcs_url(bucket_name: str, blob_name: str, expiration_minutes: int = 60) -> str:
    """Generates a V4 signed URL for a GCS object."""
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        # Check if blob exists before trying to sign, this prevents errors for non-existent objects
        if not blob.exists():
            print(f"Warning: Blob gs://{bucket_name}/{blob_name} does not exist for signing URL.")
            return f"#error-blob-not-found-{os.path.basename(blob_name)}"

        # The service account running this code needs 'Service Account Token Creator' role
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes), # Using timedelta
            method="GET" # For viewing/downloading
        )
        return url
    except Exception as e:
        print(f"Error generating signed URL for gs://{bucket_name}/{blob_name}: {e}")
        # Log the full traceback for debugging server-side issues
        import traceback
        traceback.print_exc()
        return f"#error-generating-signed-url-{os.path.basename(blob_name)}"

from google.auth import default

def _get_signed_gcs_url(bucket_name: str, blob_name: str, expiration_minutes: int = 60) -> str:
    storage_client = _get_gcs_client(project=settings.GOOGLE_PROJECT_ID)  # uses default Compute Engine creds
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    if not blob.exists():
        return f"#error-blob-not-found-{os.path.basename(blob_name)}"

    # Explicitly request “IAM‐based signing” if no private key is available:
    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
        service_account_email=settings.GCE_SERVICE_ACCOUNT_EMAIL,  # e.g. “my-vm-sa@my-project.iam.gserviceaccount.com”
    )
    return url


# Modified upload_pil_to_gcs to return a dictionary
def upload_pil_to_gcs(
    pil_image: Image.Image,
    gcs_bucket_name: str,
    gcs_object_name: str, # This is the full desired object name (e.g., folder/file.png)
    content_type: str = 'image/png'
) -> Dict[str, str]: # Returns dict with signed_url and gcs_object_name
    """Uploads a PIL Image to GCS and returns its signed URL and GCS object name."""
    if not gcs_bucket_name:
        raise ValueError("GCS_BUCKET_NAME is not configured.")
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(gcs_bucket_name)
        blob = bucket.blob(gcs_object_name)

        img_byte_arr = io.BytesIO()
        image_format = 'PNG' if content_type == 'image/png' else 'JPEG'
        pil_image.save(img_byte_arr, format=image_format)
        img_byte_arr.seek(0)

        blob.upload_from_file(img_byte_arr, content_type=content_type)
        print(f"Uploaded gs://{gcs_bucket_name}/{gcs_object_name}")
        
        signed_url = _get_signed_gcs_url(gcs_bucket_name, gcs_object_name)
        return {
            "signed_url": signed_url,
            "gcs_object_name": gcs_object_name # Path within the bucket
        }
    except Exception as e:
        print(f"Error uploading to GCS (gs://{gcs_bucket_name}/{gcs_object_name}): {e}")
        # Return an error structure
        return {
            "signed_url": f"#error-uploading-gcs-{os.path.basename(gcs_object_name)}",
            "gcs_object_name": gcs_object_name
        }

def load_pil_from_gcs(gcs_bucket_name: str, gcs_object_name: str) -> Image.Image:
    """Loads an image from GCS as a PIL Image object (for backend processing)."""
    if not gcs_bucket_name:
        raise ValueError("GCS_BUCKET_NAME is not configured.")
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(gcs_bucket_name)
        blob = bucket.blob(gcs_object_name)
        if not blob.exists():
            raise FileNotFoundError(f"GCS object gs://{gcs_bucket_name}/{gcs_object_name} not found.")
        img_bytes = blob.download_as_bytes()
        return Image.open(io.BytesIO(img_bytes))
    except Exception as e:
        print(f"Error loading from GCS (gs://{gcs_bucket_name}/{gcs_object_name}): {e}")
        raise


def list_gcs_bucket_media_files(
    gcs_bucket_name: str,
    base_prefix: Optional[str] = None,
    media_type_filter: Optional[str] = None,
    name_filter: Optional[str] = None
) -> List[Dict]: # Returns list of dicts with SIGNED URLs
    """Lists image and video files in a GCS bucket, returns list of dicts with url (signed) and type."""
    if not gcs_bucket_name or gcs_bucket_name == "your-gcs-bucket-name-here":
        print("Warning: GCS_BUCKET_NAME not configured for listing files.")
        return []
    
    media_files = []
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(gcs_bucket_name)
        
        prefix_to_list = base_prefix
        if prefix_to_list and not prefix_to_list.endswith('/'):
            prefix_to_list += '/'
            
        blobs = bucket.list_blobs(prefix=prefix_to_list)
        
        for blob in blobs:
            name_lower = blob.name.lower()
            file_type = None

            # Determine file type based on extension
            if name_lower.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                file_type = 'image'
            elif name_lower.endswith(('.mp4', '.mov', '.avi', '.webm')): # Added more common video extensions
                file_type = 'video'
            
            if not file_type: # Skip non-media files or files not matching expected types
                continue

            # Apply media type filter
            if media_type_filter and file_type != media_type_filter:
                continue

            # Apply name filter (case-insensitive search within filename)
            blob_filename = os.path.basename(blob.name)
            if name_filter and name_filter.lower() not in blob_filename.lower():
                continue
            
            # Generate signed URL for the blob
            signed_url = _get_signed_gcs_url(gcs_bucket_name, blob.name)

            media_files.append({
                'url': signed_url, # Now returns signed URL
                'type': file_type,
                'name': blob_filename,
                'gcs_path': blob.name, # Full GCS object path (gs://bucket/object)
                'updated': blob.updated # Timestamp
            })
        
        # Sort by date (newest first)
        media_files.sort(key=lambda x: x['updated'], reverse=True)
        return media_files
    except Exception as e:
        print(f"Error listing GCS bucket media (gs://{gcs_bucket_name}/{prefix_to_list or ''}): {e}")
        return []


def convert_bytes_to_pil_image(image_bytes: bytes) -> Image.Image:
    """Converts image bytes to a PIL Image object."""
    return Image.open(io.BytesIO(image_bytes))


# --- Imagen 3 Image Generation ---
def generate_images_from_prompt(
    prompt: str, negative_prompt: str, n: int, aspect: str, guidance: float, seed: Optional[int]
) -> List[Dict[str, str]]: # Returns list of dicts
    # ... (client, GCS_BUCKET_NAME checks, cfg setup) ...
    if not client: raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: raise Exception("GCS_BUCKET_NAME not configured.")
    # Get watermark policy from settings
    apply_watermark_policy = settings.IMAGEN_ADD_WATERMARK
    cfg = types.GenerateImagesConfig(
        number_of_images=n, aspect_ratio=aspect, guidance_scale=guidance,
        seed=seed or None, negative_prompt=negative_prompt or None,
        safety_filter_level="BLOCK_MEDIUM_AND_ABOVE", person_generation="ALLOW_ADULT",
        addWatermark = apply_watermark_policy,
    )
    resp = client.models.generate_images(model=IMAGEN3_GEN_MODEL, prompt=prompt, config=cfg) # Ensure model name is correct
    if not resp.generated_images: raise Exception("No images were returned by the generation model. (Safety filters?)")
    
    results = []
    for img_data in resp.generated_images:
        pil_img = convert_bytes_to_pil_image(img_data.image.image_bytes)
        obj_name = _generate_gcs_object_name(
            base_prefix=settings.GCS_GENERATED_IMAGES_PREFIX,
            prompt_text=prompt,
            extension="png"
        )
        try:
            upload_result = upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, obj_name)
            results.append(upload_result)
        except Exception as e: 
            print(f"Error uploading generated image to GCS and getting signed URL: {e}")
            results.append({
                "signed_url": f"#error-processing-gcs-{os.path.basename(obj_name)}",
                "gcs_object_name": obj_name
            })
    return results


# --- Imagen 3 Image Editing ---
def edit_image_with_prompt(
    original_pil_image: Image.Image, 
    original_filename_for_naming: Optional[str],
    n: int, mask_pil_image: Optional[Image.Image],
    prompt: str, mode: str, guidance: float, 
    style_pil_image: Optional[Image.Image], hex_codes: str
) -> List[Dict[str, str]]: # Returns list of dicts
    # ... (client, GCS_BUCKET_NAME checks, reference image setup as before) ...
    if not client: raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: raise Exception("GCS_BUCKET_NAME not configured.")
    buf = io.BytesIO(); original_pil_image.save(buf, format="PNG"); orig_bytes = buf.getvalue()
    orig_ref = types.RawReferenceImage(reference_id=0, reference_image=types.Image(image_bytes=orig_bytes, mime_type="image/png"))
    refs = [orig_ref]
    if isinstance(mask_pil_image, Image.Image): # Check against Image.Image
        mb = io.BytesIO(); mask_pil_image.save(mb, format="PNG")
        refs.append(types.MaskReferenceImage(reference_id=1, reference_image=types.Image(image_bytes=mb.getvalue(), mime_type="png"),
                                            config=types.MaskReferenceConfig(mask_mode="MASK_MODE_USER_PROVIDED", mask_dilation=0.0)))
    elif mask_pil_image is None and mode in ["inpaint", "background"]:
        refs.append(types.MaskReferenceImage(reference_id=1, reference_image=None, config=types.MaskReferenceConfig(mask_mode="MASK_MODE_FOREGROUND", mask_dilation=0.0)))
    
    mode_map = {"mask_free": "EDIT_MODE_DEFAULT", "inpaint": "EDIT_MODE_INPAINT_INSERTION", "outpaint": "EDIT_MODE_OUTPAINT", "background": "EDIT_MODE_INPAINT_INSERTION", "product_bg_swap": "EDIT_MODE_BGSWAP"}
    edit_mode_sdk = mode_map.get(mode, "EDIT_MODE_INPAINT_INSERTION")
    # Get watermark policy from settings
    cfg_params = {"edit_mode": edit_mode_sdk, "number_of_images": n, "safety_filter_level": "BLOCK_MEDIUM_AND_ABOVE", "person_generation": "ALLOW_ADULT"}
    if guidance is not None: cfg_params["guidance_scale"] = guidance

    resp = client.models.edit_image(model=IMAGEN3_EDIT_MODEL, prompt=prompt, reference_images=refs, config=types.EditImageConfig(**cfg_params)) # Ensure model name is correct
    if not resp.generated_images: raise Exception("No images were returned by the editing model. (Safety filters?)")

    results = []
    for img_data in resp.generated_images:
        pil_img = convert_bytes_to_pil_image(img_data.image.image_bytes)
        obj_name = _generate_gcs_object_name(
            base_prefix=settings.GCS_EDITED_IMAGES_PREFIX,
            original_filename=original_filename_for_naming,
            prompt_text=prompt,
            extension="png"
        )
        try:
            upload_result = upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, obj_name)
            results.append(upload_result)
        except Exception as e: 
            print(f"Error uploading edited image to GCS and getting signed URL: {e}")
            results.append({
                "signed_url": f"#error-processing-gcs-{os.path.basename(obj_name)}",
                "gcs_object_name": obj_name
            })
    return results


# --- Veo 2 Video Generation Service (using google-genai SDK, NO POLLING) ---
def _upload_input_image_for_veo_sdk(pil_image: Image.Image, original_filename: Optional[str]) -> str:
    """Uploads a PIL Image to a temporary GCS location and returns its GCS URI (gs://...)."""
    if not settings.GCS_BUCKET_NAME:
        raise ValueError("GCS_BUCKET_NAME for Veo inputs is not configured.")
    if not hasattr(settings, 'GCS_TEMP_INPUTS_PREFIX'):
        raise AttributeError("GCS_TEMP_INPUTS_PREFIX is not defined in Django settings.")
    
    gcs_object_name = _generate_gcs_object_name(
        base_prefix=settings.GCS_TEMP_INPUTS_PREFIX,
        original_filename=original_filename,
        extension="png" # Input image to Veo is typically PNG/JPG
    )
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(settings.GCS_BUCKET_NAME)
        blob = bucket.blob(gcs_object_name)

        img_byte_arr = io.BytesIO()
        pil_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        blob.upload_from_file(img_byte_arr, content_type='image/png')
        
        gcs_uri = f"gs://{settings.GCS_BUCKET_NAME}/{gcs_object_name}"
        print(f"Uploaded input image for Veo to: {gcs_uri}")
        return gcs_uri # Returns gs:// URI, not signed URL, as it's for API input
    except Exception as e:
        print(f"Error uploading temporary image for Veo to GCS: {e}")
        raise Exception(f"Failed to upload temp image for Veo: {e}") from e


def generate_video_with_veo_sdk(
    prompt: str,
    input_pil_image: Optional[Image.Image],
    input_image_filename: Optional[str], # For naming the temp GCS upload
    aspect_ratio: str,
    sample_count: int,
    duration_seconds: float,
    person_generation: str,
    enable_prompt_rewriting: bool,
) -> VideoGenerationJob:
    if not client:
        raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME:
        raise Exception("GCS_BUCKET_NAME for video output is not configured.")

    input_image_gcs_uri_for_veo = None
    if input_pil_image:
        try:
            # Upload input image to GCS (returns gs:// URI)
            input_image_gcs_uri_for_veo = _upload_input_image_for_veo_sdk(input_pil_image, input_image_filename)
        except Exception as e:
            # Create a job record to show failure early if input image upload fails
            job_error_early = VideoGenerationJob.objects.create(
                prompt=prompt, status='FAILED', error_message=f"Failed to prepare input image for Veo: {e}")
            raise Exception(f"Failed to prepare input image for Veo: {e}") from e

    job = VideoGenerationJob.objects.create( # Create job record
        prompt=prompt,
        input_image_gcs_uri=input_image_gcs_uri_for_veo, # Store gs:// URI of temp input
        status='PENDING',
    )

    # Veo will save videos directly into this folder in your bucket
    video_output_gcs_prefix_for_veo = f"gs://{settings.GCS_BUCKET_NAME}/{settings.GCS_VIDEO_OUTPUTS_PREFIX}{job.id}/"
    
    generate_videos_params_for_api_call = {
        "model": VEO2_VIDEO_MODEL,
        "prompt": prompt,
    }

    video_config_params = {
        "aspect_ratio": aspect_ratio,
        "output_gcs_uri": video_output_gcs_prefix_for_veo, # Veo saves outputs here (gs:// URI)
        "number_of_videos": sample_count,
        "duration_seconds": duration_seconds,
        "person_generation": person_generation,
    }
    # Add enhance_prompt if supported by SDK. (Your example from last turn had it)
    if enable_prompt_rewriting is not None:
        video_config_params["enhance_prompt"] = enable_prompt_rewriting

    # Add 'image' parameter if input_image_gcs_uri_for_veo is available
    if input_image_gcs_uri_for_veo:
        generate_videos_params_for_api_call["image"] = types.Image(
            gcs_uri=input_image_gcs_uri_for_veo,
            mime_type="image/png" # Assuming input image is PNG
        )
        print(f"Using initial image for video generation: {input_image_gcs_uri_for_veo}")

    config_obj = types.GenerateVideosConfig(**video_config_params)
    generate_videos_params_for_api_call["config"] = config_obj
    
    print(f"Veo SDK request params for Job ID {job.id}: {generate_videos_params_for_api_call}")

    try:
        operation_sdk_obj = client.models.generate_videos(**generate_videos_params_for_api_call)
        
        # Extract LRO name for polling. Usually operation.operation.name or operation.name
        operation_name_for_polling = None
        if hasattr(operation_sdk_obj, 'operation') and hasattr(operation_sdk_obj.operation, 'name'):
            operation_name_for_polling = operation_sdk_obj.operation.name
        elif hasattr(operation_sdk_obj, 'name'):
             operation_name_for_polling = operation_sdk_obj.name
        elif hasattr(operation_sdk_obj, 'metadata') and operation_sdk_obj.metadata is not None and hasattr(operation_sdk_obj.metadata, 'name'):
            operation_name_for_polling = operation_sdk_obj.metadata.name
        
        if operation_name_for_polling:
            job.veo_operation_name = operation_name_for_polling
            job.status = 'PROCESSING'
            job.save()
            print(f"Veo video generation operation initiated for Job ID {job.id}. LRO Name: {job.veo_operation_name}")
        else:
            job.status = 'FAILED'
            job.error_message = "Could not extract LRO name from Veo response object."
            job.save()
            print(f"Job ID {job.id}: {job.error_message}. Raw op obj: {operation_sdk_obj}")
            raise Exception(job.error_message)
        
        return job # Return the job object immediately

    except Exception as e:
        # Catch and update job status if initiation fails
        job.status = 'FAILED'
        job.error_message = f"Error initiating Veo SDK video generation: {str(e)}"
        job.save()
        print(f"Job ID {job.id}: {job.error_message}")
        # Log the full traceback for debugging
        # import traceback
        # traceback.print_exc()
        raise # Re-raise the exception to be caught by the view


def check_and_update_veo_job_status(job_id: uuid.UUID) -> VideoGenerationJob:
    """Checks the status of a Veo LRO and updates the VideoGenerationJob record."""
    try:
        job = VideoGenerationJob.objects.get(id=job_id)
    except VideoGenerationJob.DoesNotExist:
        raise Exception(f"VideoGenerationJob with ID {job_id} not found for status check.")

    if job.status not in ['PENDING', 'PROCESSING'] or not job.veo_operation_name:
        print(f"Job {job.id} is not in a pollable state (Status: {job.status}) or has no LRO name.")
        return job

    if not client:
        raise Exception("Google Gen AI Client not initialized for status check.")

    try:
        # Use the LRO name string (e.g., "projects/.../locations/.../operations/...") to get the operation status
        print(f"Polling LRO with name: {job.veo_operation_name}")
        current_operation_state = client.operations.get(name=job.veo_operation_name) # Pass name string directly
        
        print(f"Polling Job ID {job.id}, LRO {job.veo_operation_name}, Done: {current_operation_state.done()}")

        if current_operation_state.done():
            if hasattr(current_operation_state, 'error') and current_operation_state.error:
                job.status = 'FAILED'
                job.error_message = current_operation_state.error.message if hasattr(current_operation_state.error, 'message') else "Unknown Veo LRO error"
                print(f"Job ID {job.id} LRO failed: {job.error_message}")
            else:
                job.status = 'COMPLETED'
                job.error_message = None
                print(f"Job ID {job.id} LRO completed successfully.")
                
                video_gcs_uris_from_veo_output = []
                signed_public_urls = []
                final_operation_result = current_operation_state.result()

                if final_operation_result and hasattr(final_operation_result, 'generated_videos'):
                    for video_info in final_operation_result.generated_videos:
                        if hasattr(video_info, 'video') and hasattr(video_info.video, 'uri'):
                            gcs_uri = video_info.video.uri # This is gs://bucket/path/to/video.mp4
                            video_gcs_uris_from_veo_output.append(gcs_uri)
                            
                            # Extract bucket and blob name from gs:// URI to generate signed URL
                            if gcs_uri.startswith("gs://"):
                                # Assuming gs://BUCKET_NAME/OBJECT_NAME format
                                parts = gcs_uri[5:].split("/", 1) # Split after 'gs://' to get bucket and rest
                                if len(parts) == 2:
                                    output_bucket_name, output_blob_name = parts
                                    try:
                                        signed_url = _get_signed_gcs_url(output_bucket_name, output_blob_name)
                                        signed_public_urls.append(signed_url)
                                    except Exception as sign_e:
                                        print(f"Job ID {job.id}: Error signing URL for {gcs_uri}: {sign_e}")
                                        signed_public_urls.append(f"#error-signing-uri-{os.path.basename(gcs_uri)}")
                                else:
                                    print(f"Job ID {job.id}: Could not parse GCS URI for signing: {gcs_uri}")
                                    signed_public_urls.append(f"#error-parsing-gcs-uri-{os.path.basename(gcs_uri)}")
                            else:
                                print(f"Job ID {job.id}: Expected gs:// URI from Veo, got: {gcs_uri}")
                                signed_public_urls.append(f"#error-invalid-gcs-uri-{os.path.basename(gcs_uri)}")
                        else:
                            print(f"Job ID {job.id}: Unexpected video_info structure in result: {video_info}")
                else:
                    print(f"Job ID {job.id}: Operation result or generated_videos not found in completed LRO.")
                
                job.output_video_gcs_uris_json = json.dumps(video_gcs_uris_from_veo_output) # Store raw GCS URIs
                job.public_video_urls_json = json.dumps(signed_public_urls) # Store signed URLs for display
            job.save()
            
    except Exception as e:
        print(f"Error checking/updating Veo job status for Job ID {job.id}, LRO Name {job.veo_operation_name}: {e}")
        # Mark as failed if polling itself fails repeatedly or critically
        # job.status = 'FAILED' # Uncomment if polling error should immediately fail job
        # job.error_message = f"Error during status check: {str(e)}"
        # job.save()
        import traceback
        traceback.print_exc()
        # Re-raise to signal to API view
        raise Exception(f"Internal server error during status check: {e}")
    return job