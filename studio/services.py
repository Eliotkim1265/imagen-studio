# studio/services.py
import os
import uuid
import io
import time
import json
from datetime import datetime 
from typing import Optional, List, Dict

from PIL import Image
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
from .models import VideoGenerationJob

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
VEO2_VIDEO_MODEL = "veo-2.0-generate-001"

# --- GCS Helper Functions ---
def _get_gcs_client():
    """Initializes and returns a GCS client, relying on ADC."""
    try:
        if not settings.GOOGLE_PROJECT_ID:
            raise ValueError("GOOGLE_PROJECT_ID setting is not configured for GCS client.")
        return storage.Client(project=settings.GOOGLE_PROJECT_ID)
    except Exception as e:
        print(f"CRITICAL Error initializing GCS client: {e}")
        import traceback
        traceback.print_exc()
        raise


def _generate_gcs_object_name(
    base_prefix: str,
    original_filename: Optional[str] = None,
    prompt_text: Optional[str] = None,
    extension: str = "png"
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = str(uuid.uuid4())[:8]
    name_part = "media"
    if original_filename:
        name_part = slugify(os.path.splitext(original_filename)[0])[:50]
    elif prompt_text:
        name_part = slugify(prompt_text)[:50]
    if not name_part or len(name_part) == 0: name_part = "unnamed_media"
    if base_prefix and not base_prefix.endswith('/'): base_prefix += '/'
    return f"{base_prefix}{name_part}_{timestamp}_{unique_id}.{extension}"

def _get_signed_gcs_url_for_direct_download(bucket_name: str, blob_name: str, expiration_minutes: int = 15) -> str:
    """Generates a V4 signed URL for a direct, temporary download."""
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if not blob.exists(): return f"#error-blob-not-found-{os.path.basename(blob_name)}"
        url = blob.generate_signed_url(version="v4", expiration=timedelta(minutes=expiration_minutes), method="GET")
        return url
    except Exception as e:
        print(f"Error generating direct download signed URL for {blob_name}: {e}")
        return f"#error-signing-download-url-{os.path.basename(blob_name)}"


def upload_pil_to_gcs(
    pil_image: Image.Image,
    gcs_bucket_name: str,
    gcs_object_name: str,
    content_type: str = 'image/png'
) -> str: # Returns GCS object name (path within bucket)
    """Uploads a PIL Image to GCS and returns its GCS object name."""
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
        print(f"Uploaded to gs://{gcs_bucket_name}/{gcs_object_name}")
        return gcs_object_name # Return the object name
    except Exception as e:
        print(f"Error uploading to GCS (gs://{gcs_bucket_name}/{gcs_object_name}): {e}")
        raise # Re-raise the exception to be handled by the caller

def load_pil_from_gcs(gcs_bucket_name: str, gcs_object_name: str) -> Image.Image:
    if not gcs_bucket_name: raise ValueError("GCS_BUCKET_NAME not configured.")
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(gcs_bucket_name)
        blob = bucket.blob(gcs_object_name)
        if not blob.exists(): raise FileNotFoundError(f"GCS object gs://{gcs_bucket_name}/{gcs_object_name} not found.")
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
) -> List[Dict]:
    """Lists image and video files, returns data to construct proxy URLs."""
    if not gcs_bucket_name or gcs_bucket_name == "your-gcs-bucket-name-here":
        print("Warning: GCS_BUCKET_NAME not configured for listing files.")
        return []
    
    media_files_data = []
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(gcs_bucket_name)
        prefix_to_list = base_prefix
        if prefix_to_list and not prefix_to_list.endswith('/'): prefix_to_list += '/'
        blobs = bucket.list_blobs(prefix=prefix_to_list)
        
        for blob in blobs:
            name_lower = blob.name.lower()
            file_type = None
            if name_lower.endswith(('.png', '.jpg', '.jpeg', '.webp')): file_type = 'image'
            elif name_lower.endswith(('.mp4', '.mov', '.avi', '.webm')): file_type = 'video'
            if not file_type: continue
            if media_type_filter and file_type != media_type_filter: continue
            blob_filename = os.path.basename(blob.name)
            if name_filter and name_filter.lower() not in blob_filename.lower(): continue
            
            media_files_data.append({
                # 'url' will be constructed in the view using reverse() and gcs_path
                'gcs_path': blob.name, # Full GCS object path (e.g., media_studio_uploads/generated_images/file.png)
                'type': file_type,
                'name': blob_filename,
                'updated': blob.updated
            })
        media_files_data.sort(key=lambda x: x['updated'], reverse=True)
        return media_files_data
    except Exception as e:
        print(f"Error listing GCS bucket media (gs://{gcs_bucket_name}/{prefix_to_list or ''}): {e}")
        return []


def convert_bytes_to_pil_image(image_bytes: bytes) -> Image.Image:
    """Converts image bytes to a PIL Image object."""
    return Image.open(io.BytesIO(image_bytes))


# --- Imagen 3 Image Generation ---
def generate_images_from_prompt(
    prompt: str, negative_prompt: Optional[str], n: int, aspect: str, guidance: float, seed: Optional[int]
) -> List[Dict[str, str]]: # Returns list of dicts: [{'gcs_object_name': ...}, ...]
    if not client: raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: raise Exception("GCS_BUCKET_NAME not configured.")

    apply_watermark_policy = settings.IMAGEN_ADD_WATERMARK
    cfg_params = {
        "number_of_images": n, "aspect_ratio": aspect, "guidance_scale": guidance,
        "safety_filter_level": "BLOCK_MEDIUM_AND_ABOVE", "person_generation": "ALLOW_ADULT",
    }
    if apply_watermark_policy:
        if seed is not None: print(f"INFO: Seed {seed} provided, but IMAGEN_ADD_WATERMARK policy is True. Seed may be ignored.")
        cfg_params["addWatermark"] = True 
    else:
        if seed is not None: cfg_params["seed"] = seed
        cfg_params["addWatermark"] = False
    if "seed" in cfg_params: cfg_params["addWatermark"] = False
    
    cfg = types.GenerateImagesConfig(**cfg_params, negative_prompt = negative_prompt or None)
    resp = client.models.generate_images(model=IMAGEN3_GEN_MODEL, prompt=prompt, config=cfg)
    if not resp.generated_images: raise Exception("No images returned by generation model. (Safety filters?)")
    
    results = []
    for img_data in resp.generated_images:
        pil_img = convert_bytes_to_pil_image(img_data.image.image_bytes)
        obj_name = _generate_gcs_object_name(settings.GCS_GENERATED_IMAGES_PREFIX, prompt_text=prompt, extension="png")
        try:
            # upload_pil_to_gcs returns the GCS object name
            gcs_object_name_created = upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, obj_name)
            results.append({"gcs_object_name": gcs_object_name_created})
        except Exception as e: 
            print(f"Error uploading generated image to GCS: {e}")
            results.append({"gcs_object_name": obj_name, "error": str(e)}) # Include error
    return results

# --- Imagen 3 Image Editing ---
def edit_image_with_prompt(
    original_pil_image: Image.Image, original_filename_for_naming: Optional[str],
    n: int, mask_pil_image: Optional[Image.Image],
    prompt: str, mode: str, guidance: Optional[float],
    style_pil_image: Optional[Image.Image], hex_codes: str
) -> List[Dict[str, str]]: # Returns list of dicts
    if not client: raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: raise Exception("GCS_BUCKET_NAME not configured.")
    buf = io.BytesIO(); original_pil_image.save(buf, format="PNG"); orig_bytes = buf.getvalue()
    orig_ref = types.RawReferenceImage(reference_id=0, reference_image=types.Image(image_bytes=orig_bytes, mime_type="image/png"))
    refs = [orig_ref]
    if isinstance(mask_pil_image, Image.Image): 
        mb = io.BytesIO(); mask_pil_image.save(mb, format="PNG")
        refs.append(types.MaskReferenceImage(reference_id=1, reference_image=types.Image(image_bytes=mb.getvalue(), mime_type="png"),
                                            config=types.MaskReferenceConfig(mask_mode="MASK_MODE_USER_PROVIDED", mask_dilation=0.0)))
    elif mask_pil_image is None and mode in ["inpaint", "background"]:
        refs.append(types.MaskReferenceImage(reference_id=1, reference_image=None, config=types.MaskReferenceConfig(mask_mode="MASK_MODE_FOREGROUND", mask_dilation=0.0)))

    mode_map = {"mask_free": "EDIT_MODE_DEFAULT", "inpaint": "EDIT_MODE_INPAINT_INSERTION", "outpaint": "EDIT_MODE_OUTPAINT", "background": "EDIT_MODE_BGSWAP"}
    edit_mode_sdk = mode_map.get(mode)
    cfg_params = {"edit_mode": edit_mode_sdk, "number_of_images": n,
                  "safety_filter_level": "BLOCK_MEDIUM_AND_ABOVE", "person_generation": "ALLOW_ADULT"}
    if guidance is not None: cfg_params["guidance_scale"] = guidance
    
    edit_cfg = types.EditImageConfig(**cfg_params)
    resp = client.models.edit_image(model=IMAGEN3_EDIT_MODEL, prompt=prompt, reference_images=refs, config=edit_cfg)
    if not resp.generated_images: raise Exception("No images returned by editing model. (Safety filters?)")

    results = []
    for img_data in resp.generated_images:
        pil_img = convert_bytes_to_pil_image(img_data.image.image_bytes)
        obj_name = _generate_gcs_object_name(settings.GCS_EDITED_IMAGES_PREFIX, original_filename=original_filename_for_naming, prompt_text=prompt, extension="png")
        try:
            gcs_object_name_created = upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, obj_name)
            results.append({"gcs_object_name": gcs_object_name_created})
        except Exception as e:
            print(f"Error uploading edited image to GCS: {e}")
            results.append({"gcs_object_name": obj_name, "error": str(e)})
    return results


def edit_image_with_prompt(
    original_pil_image: Image.Image, 
    original_filename_for_naming: Optional[str],
    n: int, # Number of images to generate
    mask_pil_image: Optional[Image.Image],
    prompt: str, 
    mode: str, # UI mode: "mask_free", "inpaint", "outpaint", "background"
    guidance: Optional[float],
    style_pil_image: Optional[Image.Image], # For style transfer
    hex_codes: Optional[str] # JSON string of hex codes for palette
) -> List[Dict[str, str]]: # Returns list of dicts with 'gcs_object_name' or 'error'
    if not client: 
        raise Exception("Google Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: 
        raise Exception("GCS_BUCKET_NAME not configured in settings.")

    # 1. Prepare Base Image Reference
    buf = io.BytesIO()
    original_pil_image.save(buf, format="PNG") # Ensure PNG for transparency if applicable
    orig_bytes = buf.getvalue()
    # The SDK might expect types.Image directly or a RawReferenceImage.
    # Based on imagen3_editing.py, it often uses types.Image directly for some inputs.
    # Let's assume the SDK's edit_image call takes a list of reference images,
    # where the first is the base image and subsequent ones are masks or style references if structured that way.
    # The notebook used a list of RawReferenceImage and MaskReferenceImage for the `reference_images` param.
    
    base_image_sdk_ref = types.Image(image_bytes=orig_bytes, mime_type="image/png")
    # Or, if RawReferenceImage is strictly needed for the base image:
    # base_image_sdk_ref = types.RawReferenceImage(
    #     reference_id=0, # Or a unique ID like "base"
    #     reference_image=types.Image(image_bytes=orig_bytes, mime_type="image/png")
    # )
    # For now, let's assume the client.models.edit_image can take types.Image for the main image
    # and types.Mask for the mask, as shown in some SDK patterns, or adjust if it strictly needs RawReferenceImage.
    # The user's provided snippet for this function used RawReferenceImage, so we'll stick to that.
    orig_ref = types.RawReferenceImage(
        reference_id=0, 
        reference_image=types.Image(image_bytes=orig_bytes, mime_type="image/png")
    )
    reference_images_list = [orig_ref] # Start with the base image

    # 2. Prepare Mask Reference (if applicable)
    mask_config_sdk = None
    if isinstance(mask_pil_image, Image.Image): # User-provided mask
        mb = io.BytesIO()
        mask_pil_image.save(mb, format="PNG") # Ensure mask is PNG
        mask_image_for_sdk = types.Image(image_bytes=mb.getvalue(), mime_type="image/png")
        # The SDK might take types.Mask directly or a MaskReferenceImage.
        # The notebook used MaskReferenceImage.
        mask_ref_img_obj = types.MaskReferenceImage(
            reference_id=1, # Or "mask"
            reference_image=mask_image_for_sdk,
            config=types.MaskReferenceConfig(
                mask_mode="MASK_MODE_USER_PROVIDED",
                mask_dilation=0.0 # Adjust if needed
            )
        )
        reference_images_list.append(mask_ref_img_obj)
    elif mask_pil_image is None: # No user mask, check if mode requires semantic masking
        if mode == "inpaint":
            mask_config_sdk = types.MaskReferenceConfig(mask_mode="MASK_MODE_FOREGROUND")
        elif mode == "background": # For BGSWAP with auto-mask
            mask_config_sdk = types.MaskReferenceConfig(mask_mode="MASK_MODE_BACKGROUND")
        
        if mask_config_sdk: # If an auto-mask config was created
            auto_mask_ref = types.MaskReferenceImage(
                reference_id=1, # Or "auto_mask"
                reference_image=None, # API will generate mask based on config
                config=mask_config_sdk
            )
            reference_images_list.append(auto_mask_ref)
    mode_map = {
        "mask_free": "EDIT_MODE_DEFAULT",          # General editing
        "inpaint": "EDIT_MODE_INPAINT_INSERTION",  # Inpainting or replacing within mask
        "outpaint": "EDIT_MODE_OUTPAINT",          # Outpainting
        "background": "EDIT_MODE_BGSWAP",          # Background swap/edit
        "product_image_edit": "EDIT_MODE_PRODUCT_IMAGE" 
    }
    edit_mode_sdk = mode_map.get(mode)
    if not edit_mode_sdk:
        print(f"Warning: Unknown UI edit mode '{mode}'. Defaulting to Mask-Free Edit.")
        edit_mode_sdk = "EDIT_MODE_DEFAULT"

    # 4. Prepare top-level parameters for client.models.edit_image
    api_call_params = {
        "model": IMAGEN3_EDIT_MODEL,
        "prompt": prompt,
        "reference_images": reference_images_list, # List of RawReferenceImage and MaskReferenceImage
    }

    # Add style image if provided (as per imagen3_editing.py notebook, this is a top-level param)
    if isinstance(style_pil_image, Image.Image):
        style_buf = io.BytesIO()
        style_pil_image.save(style_buf, format="PNG") # Or JPEG, whatever API prefers for style
        api_call_params["style_reference_image"] = types.Image(
            image_bytes=style_buf.getvalue(),
            mime_type="image/png" # Or "image/jpeg"
        )
    
    # Add palette colors if provided (as per imagen3_editing.py notebook, top-level param)
    parsed_hex_codes = []
    if hex_codes:
        try:
            parsed_hex_codes = json.loads(hex_codes)
            if not (isinstance(parsed_hex_codes, list) and all(isinstance(code, str) for code in parsed_hex_codes)):
                print(f"Warning: Invalid hex_codes format: {hex_codes}. Expected list of strings. Ignoring palette.")
                parsed_hex_codes = []
        except json.JSONDecodeError:
            print(f"Warning: Could not parse hex_codes JSON: {hex_codes}. Ignoring palette.")
            parsed_hex_codes = []
    
    if parsed_hex_codes:
        api_call_params["palette_colors"] = types.Palette(colors=parsed_hex_codes)


    # 5. Assemble EditImageConfig
    config_params_for_sdk = {
        "edit_mode": edit_mode_sdk,
        "number_of_images": n,
        "safety_filter_level": "BLOCK_MEDIUM_AND_ABOVE", # Or make configurable via settings
        "person_generation": "ALLOW_ADULT",           # Or make configurable
        # "output_format": "PNG", # Default is usually PNG
    }
    if guidance is not None:
        config_params_for_sdk["guidance_scale"] = guidance
    
    # Specific config for outpainting if not using a user-provided outpainting mask
    # This part is complex as true outpainting usually requires padding the image first
    # and providing a mask of the new areas. The EDIT_MODE_OUTPAINT might work semantically
    # or based on the extent of a user-provided mask.
    if edit_mode_sdk == "EDIT_MODE_OUTPAINT" and not isinstance(mask_pil_image, Image.Image):
        print("Warning: Outpainting without a user-provided mask might yield unpredictable results or use default extents.")
        # You might need to set specific outpainting parameters here if the SDK supports them
        # e.g., outpaint_config = types.OutpaintConfig(desired_width=..., desired_height=...)
        # config_params_for_sdk["outpaint_config"] = outpaint_config # Hypothetical

    api_call_params["config"] = types.EditImageConfig(**config_params_for_sdk)
    
    print(f"Calling client.models.edit_image with params: { {k: v for k, v in api_call_params.items() if k != 'reference_images'} }")
    # (Not printing reference_images byte data to keep log clean)

    # 6. Call the API
    try:
        resp = client.models.edit_image(**api_call_params)
    except Exception as api_e:
        print(f"Error calling Imagen 3 edit_image API: {api_e}")
        import traceback
        traceback.print_exc()
        raise Exception(f"Imagen 3 API call failed: {api_e}")


    if not resp.generated_images:
        # Check for specific safety filter feedback if available
        # rai_info = getattr(resp, 'rai_info', None) or getattr(resp, 'safety_feedback', None)
        # if rai_info:
        #     raise Exception(f"No images returned by editing model. Safety filters likely triggered. Details: {rai_info}")
        raise Exception("No images were returned by the editing model. (Safety filters or invalid parameters?)")

    # 7. Process Response & Upload to GCS
    results = []
    for i, img_data in enumerate(resp.generated_images):
        pil_img = convert_bytes_to_pil_image(img_data.image.image_bytes)
        # Construct a filename based on original name, prompt, and current mode for better organization
        filename_hint = f"{mode}"
        if original_filename_for_naming:
            filename_hint += f"_{os.path.splitext(original_filename_for_naming)[0]}"
        
        obj_name = _generate_gcs_object_name(
            base_prefix=settings.GCS_EDITED_IMAGES_PREFIX,
            original_filename=filename_hint, # Use the constructed hint
            prompt_text=prompt, 
            extension="png"
        )
        try:
            # upload_pil_to_gcs should return the GCS object name string
            gcs_object_name_created = upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, obj_name)
            results.append({"gcs_object_name": gcs_object_name_created})
        except Exception as e:
            print(f"Error uploading edited image {i} to GCS: {e}")
            results.append({"gcs_object_name": obj_name, "error": f"GCS Upload Failed: {str(e)}"})
    return results


# --- Veo 2 Video Generation Service (using google-genai SDK) ---
def _upload_input_image_for_veo_sdk(pil_image: Image.Image, original_filename: Optional[str]) -> str:
    if not settings.GCS_BUCKET_NAME: raise ValueError("GCS_BUCKET_NAME for Veo inputs missing.")
    if not hasattr(settings, 'GCS_TEMP_INPUTS_PREFIX'): raise AttributeError("GCS_TEMP_INPUTS_PREFIX missing.")
    gcs_object_name = _generate_gcs_object_name(settings.GCS_TEMP_INPUTS_PREFIX, original_filename, extension="png")
    try:
        storage_client = _get_gcs_client(); bucket = storage_client.bucket(settings.GCS_BUCKET_NAME); blob = bucket.blob(gcs_object_name)
        img_byte_arr = io.BytesIO(); pil_image.save(img_byte_arr, format='PNG'); img_byte_arr.seek(0)
        blob.upload_from_file(img_byte_arr, content_type='image/png')
        return f"gs://{settings.GCS_BUCKET_NAME}/{gcs_object_name}"
    except Exception as e: raise Exception(f"Failed to upload temp image for Veo: {e}") from e

def generate_video_with_veo_sdk(
    prompt: str, input_pil_image: Optional[Image.Image], input_image_filename: Optional[str],
    aspect_ratio: str, sample_count: int, duration_seconds: float,
    person_generation: str, enable_prompt_rewriting: bool
) -> VideoGenerationJob:
    # The check_and_update_veo_job_status will populate job.output_video_gcs_uris_json (raw gs:// URIs)
    if not client: raise Exception("Gen AI Client not initialized.")
    if not settings.GCS_BUCKET_NAME: raise Exception("GCS_BUCKET_NAME for video output missing.")

    input_image_gcs_uri = None
    if input_pil_image:
        try: input_image_gcs_uri = _upload_input_image_for_veo_sdk(input_pil_image, input_image_filename)
        except Exception as e: 
            job_err = VideoGenerationJob.objects.create(prompt=prompt, status='FAILED', error_message=f"Input image prep failed: {e}")
            raise Exception(f"Input image prep failed: {e}") from e

    job = VideoGenerationJob.objects.create(prompt=prompt, input_image_gcs_uri=input_image_gcs_uri, status='PENDING')
    video_out_prefix = f"gs://{settings.GCS_BUCKET_NAME}/{settings.GCS_VIDEO_OUTPUTS_PREFIX}{job.id}/" # Veo writes here
    
    api_params = {"model": VEO2_VIDEO_MODEL, "prompt": prompt}
    cfg_params = {"aspect_ratio": aspect_ratio, "output_gcs_uri": video_out_prefix, 
                  "number_of_videos": sample_count, "duration_seconds": duration_seconds, 
                  "person_generation": person_generation}
    if enable_prompt_rewriting is not None: cfg_params["enhance_prompt"] = enable_prompt_rewriting
    if input_image_gcs_uri: api_params["image"] = types.Image(gcs_uri=input_image_gcs_uri, mime_type="image/png")
    api_params["config"] = types.GenerateVideosConfig(**cfg_params)
    
    try:
        op_sdk = client.models.generate_videos(**api_params)
        op_name = op_sdk.operation.name if hasattr(op_sdk, 'operation') and op_sdk.operation else (op_sdk.name if hasattr(op_sdk, 'name') else None)
        if op_name:
            job.veo_operation_name = op_name; job.status = 'PROCESSING'; job.save()
        else: raise Exception("Could not extract LRO name from Veo response.")
        return job
    except Exception as e:
        job.status = 'FAILED'; job.error_message = f"Error initiating Veo: {str(e)}"; job.save()
        raise

# check_and_update_veo_job_status
def check_and_update_veo_job_status(job_id: uuid.UUID) -> VideoGenerationJob:
    job = VideoGenerationJob.objects.get(id=job_id)
    if job.status not in ['PENDING', 'PROCESSING'] or not job.veo_operation_name: return job
    if not client: raise Exception("Gen AI Client not initialized.")

    try:
        current_op = client.operations.get(name=job.veo_operation_name)
        if current_op.done():
            if hasattr(current_op, 'error') and current_op.error:
                job.status = 'FAILED'; job.error_message = current_op.error.message or "Unknown LRO error"
            else:
                job.status = 'COMPLETED'; job.error_message = None
                raw_video_gcs_object_names = [] # Store just object names relative to bucket
                
                final_result = current_op.result()
                if final_result and hasattr(final_result, 'generated_videos'):
                    for video_info in final_result.generated_videos:
                        if hasattr(video_info, 'video') and hasattr(video_info.video, 'uri'):
                            gcs_uri = video_info.video.uri 
                            if gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                                object_name = gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                                raw_video_gcs_object_names.append(object_name)
                            else: 
                                print(f"Warning: Video URI {gcs_uri} is not in the configured GCS_BUCKET_NAME.")
                                raw_video_gcs_object_names.append(gcs_uri) # Store full URI if different bucket
                        else: print(f"Job {job.id}: Unexpected video_info structure: {video_info}")
                else: print(f"Job {job.id}: Result/generated_videos not found.")
                
                # Store raw GCS object names (or full gs:// URIs if different bucket)
                job.output_video_gcs_uris_json = json.dumps(raw_video_gcs_object_names) 
                # job.public_video_urls_json will be populated by the view using these object names to create proxy URLs
                job.public_video_urls_json = None # Clear any old signed URLs
            job.save()
    except Exception as e:
        print(f"Error checking Veo job {job.id} (LRO: {job.veo_operation_name}): {e}")
        raise Exception(f"Status check error: {e}") from e
    return job