# studio/views.py
import os
from django.shortcuts import render, redirect
from django.conf import settings
from django.contrib import messages
from PIL import Image
from django import forms
from django.http import JsonResponse # For API endpoint
from .models import VideoGenerationJob # Import the model
from .forms import GenerateForm, EditForm, VideoGenerateForm, BrowseFilterForm
from .services import (
    generate_images_from_prompt,
    edit_image_with_prompt,
    load_pil_from_gcs,
    _generate_gcs_object_name,
    _get_signed_gcs_url,
    #list_gcs_bucket_files,
    upload_pil_to_gcs,
    generate_video_with_veo_sdk, # Updated service function name
    # client # If you want to check if client is None
    check_and_update_veo_job_status, # New service function
    list_gcs_bucket_media_files # Updated listing function
)

import json

# Helper function to get PIL image from either upload or GCS path
def get_pil_image_from_form_data(form_cleaned_data, upload_field_name, media_path_field_name):
    """Helper to get PIL image from either uploaded file or GCS path (which is now 'media_path')."""
    uploaded_file = form_cleaned_data.get(upload_field_name)
    media_path_from_form = form_cleaned_data.get(media_path_field_name) # This is the user's input

    if uploaded_file:
        try:
            return Image.open(uploaded_file)
        except Exception as e:
            raise forms.ValidationError(f"Could not open uploaded file '{upload_field_name}': {e}")
    elif media_path_from_form:
        # Construct full GCS object name. It might be relative to GCS_OBJECT_PATH_PREFIX or a full path if passed from 'Edit Again'.
        if media_path_from_form.startswith(settings.GCS_OBJECT_PATH_PREFIX) or \
           media_path_from_form.startswith(settings.GCS_TEMP_INPUTS_PREFIX) or \
           media_path_from_form.startswith(settings.GCS_GENERATED_IMAGES_PREFIX) or \
           media_path_from_form.startswith(settings.GCS_EDITED_IMAGES_PREFIX) or \
           media_path_from_form.startswith(settings.GCS_VIDEO_OUTPUTS_PREFIX):
            full_gcs_object_name = media_path_from_form.lstrip('/') # It's already a full path if it starts with one of these
        else: # Assume it's relative to the app's main prefix if no special prefix is detected
            full_gcs_object_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path_from_form.lstrip('/')}"
        
        try:
            return load_pil_from_gcs(settings.GCS_BUCKET_NAME, full_gcs_object_name)
        except FileNotFoundError:
            raise forms.ValidationError(f"Media not found at Cloud Storage path: {full_gcs_object_name}")
        except Exception as e:
            raise forms.ValidationError(f"Error loading media from Cloud Storage path {full_gcs_object_name}: {e}")
    return None



def generate_view(request):
    form = GenerateForm(request.POST or None)
    generated_image_results = [] # These will now be GCS URLs

    if request.method == 'POST' and form.is_valid():
        try:
            seed_val = form.cleaned_data.get('seed')
            seed_int = int(seed_val) if seed_val is not None else None

            results_list = generate_images_from_prompt(
                prompt=form.cleaned_data['prompt'],
                negative_prompt=form.cleaned_data['negative_prompt'],
                n=form.cleaned_data['number_of_images'],
                aspect=form.cleaned_data['aspect_ratio'],
                guidance=form.cleaned_data['guidance_scale'],
                seed=seed_int
            )
            generated_image_results = results_list 
            messages.success(request, f"{len(generated_image_results)} image(s) generated and saved to GCS!")
        except Exception as e:
            messages.error(request, f"Error generating images: {e}")
            import traceback
            traceback.print_exc()
    elif request.method == 'POST' and not form.is_valid():
        messages.error(request, f"Please correct the form errors: {form.errors.as_json()}")


    return render(request, 'studio/generate.html', {
        'form': form,
        'generated_image_results': generated_image_results,
        'active_page': 'generate'
    })

def edit_view(request):
    initial_form_data = {}
    original_image_signed_url_for_preview = None
    mask_image_signed_url_for_preview = None
    style_image_signed_url_for_preview = None # Initialize

    # Initialize variables to hold GCS object names for form pre-filling
    original_gcs_object_name_for_form_display = None # CHANGED from original_gcs_object_name_for_form
    mask_gcs_object_name_for_form_display = None     # CORRECT INITIALIZATION
    style_gcs_object_name_for_form_display = None    # CORRECT INITIALIZATION

    query_param_original_media_path = request.GET.get('original_gcs_path') # Keep 'original_gcs_path' for "Edit Again" links

    if request.method == 'GET':
        if query_param_original_media_path:
            original_gcs_object_name_for_form_display = query_param_original_media_path
            request.session['last_original_for_edit_gcs_uri'] = f"gs://{settings.GCS_BUCKET_NAME}/{query_param_original_media_path}"
            try:
                original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, query_param_original_media_path)
            except Exception as e:
                messages.error(request, f"Could not load preview for GCS image {query_param_original_media_path}: {e}")
            request.session.pop('last_mask_for_edit_gcs_uri', None)
            request.session.pop('last_style_for_edit_gcs_uri', None)
        else:
            session_original_gcs_uri = request.session.get('last_original_for_edit_gcs_uri')
            if session_original_gcs_uri and session_original_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                path_in_bucket = session_original_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                original_gcs_object_name_for_form_display = path_in_bucket
                try:
                    original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, path_in_bucket)
                except Exception as e:
                    messages.warning(request, f"Could not load session preview for {path_in_bucket}: {e}")

            session_mask_gcs_uri = request.session.get('last_mask_for_edit_gcs_uri')
            if session_mask_gcs_uri and session_mask_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                mask_path_in_bucket = session_mask_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                mask_gcs_object_name_for_form_display = mask_path_in_bucket
                try:
                    mask_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, mask_path_in_bucket)
                except Exception as e:
                    messages.warning(request, f"Could not load session mask preview for {mask_path_in_bucket}: {e}")

            session_style_gcs_uri = request.session.get('last_style_for_edit_gcs_uri')
            if session_style_gcs_uri and session_style_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                style_path_in_bucket = session_style_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                style_gcs_object_name_for_form_display = style_path_in_bucket
                try:
                    style_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, style_path_in_bucket)
                except Exception as e:
                    messages.warning(request, f"Could not load session style preview for {style_path_in_bucket}: {e}")

        # Prepare initial data for the form.
        # The form fields 'original_image_media_path', etc., expect paths relative to GCS_OBJECT_PATH_PREFIX
        # or full paths from other known app prefixes (like GCS_TEMP_INPUTS_PREFIX).
        if original_gcs_object_name_for_form_display:
            if original_gcs_object_name_for_form_display.startswith(settings.GCS_OBJECT_PATH_PREFIX):
                initial_form_data['original_image_media_path'] = original_gcs_object_name_for_form_display[len(settings.GCS_OBJECT_PATH_PREFIX):]
            else: # If not under main prefix (e.g., from temp, or full path from "Edit Again"), use as is for field
                 initial_form_data['original_image_media_path'] = original_gcs_object_name_for_form_display

        if mask_gcs_object_name_for_form_display: # THIS IS WHERE THE ERROR WAS
            if mask_gcs_object_name_for_form_display.startswith(settings.GCS_OBJECT_PATH_PREFIX):
                initial_form_data['mask_image_media_path'] = mask_gcs_object_name_for_form_display[len(settings.GCS_OBJECT_PATH_PREFIX):]
            else:
                initial_form_data['mask_image_media_path'] = mask_gcs_object_name_for_form_display
        
        if style_gcs_object_name_for_form_display:
            if style_gcs_object_name_for_form_display.startswith(settings.GCS_OBJECT_PATH_PREFIX):
                initial_form_data['style_reference_image_media_path'] = style_gcs_object_name_for_form_display[len(settings.GCS_OBJECT_PATH_PREFIX):]
            else:
                initial_form_data['style_reference_image_media_path'] = style_gcs_object_name_for_form_display


    form = EditForm(request.POST or None, initial=initial_form_data or None, files=request.FILES or None)
    edited_image_results = []

    if request.method == 'POST' and form.is_valid():
        original_pil = None
        original_filename_for_naming = None
        processed_original_gcs_uri_for_session = None
        processed_mask_gcs_uri_for_session = None
        processed_style_gcs_uri_for_session = None

        try:
            # Determine original image source
            original_pil = get_pil_image_from_form_data(
                form.cleaned_data, 'original_image_upload', 'original_image_media_path'
            )
            uploaded_original_file = form.cleaned_data.get('original_image_upload')
            media_path_original_from_form = form.cleaned_data.get('original_image_media_path')

            if uploaded_original_file:
                original_filename_for_naming = uploaded_original_file.name
                # If uploaded, original_pil is already set by get_pil_image_from_form_data
                if original_pil: # Ensure PIL object was created
                    temp_gcs_object_name = _generate_gcs_object_name(
                        base_prefix=settings.GCS_TEMP_INPUTS_PREFIX,
                        original_filename=original_filename_for_naming,
                        extension=os.path.splitext(original_filename_for_naming)[1].strip('.') if '.' in original_filename_for_naming else 'png'
                    )
                    upload_pil_to_gcs(original_pil, settings.GCS_BUCKET_NAME, temp_gcs_object_name)
                    processed_original_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_gcs_object_name}"
                    original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, temp_gcs_object_name)
            elif media_path_original_from_form and original_pil: # original_pil loaded by get_pil_image_from_form_data
                # Construct full GCS object name based on what get_pil_image_from_form_data resolved
                full_gcs_object_name = media_path_original_from_form
                if not (media_path_original_from_form.startswith(settings.GCS_OBJECT_PATH_PREFIX) or \
                        media_path_original_from_form.startswith(settings.GCS_TEMP_INPUTS_PREFIX) or \
                        media_path_original_from_form.startswith(settings.GCS_GENERATED_IMAGES_PREFIX) or \
                        media_path_original_from_form.startswith(settings.GCS_EDITED_IMAGES_PREFIX)):
                    full_gcs_object_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path_original_from_form.lstrip('/')}"
                else:
                    full_gcs_object_name = media_path_original_from_form.lstrip('/')

                original_filename_for_naming = os.path.basename(full_gcs_object_name)
                processed_original_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_gcs_object_name}"
                original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_object_name)
            elif request.session.get('last_original_for_edit_gcs_uri') and original_pil: # From session
                session_uri = request.session['last_original_for_edit_gcs_uri']
                full_gcs_object_name = session_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                original_filename_for_naming = os.path.basename(full_gcs_object_name)
                processed_original_gcs_uri_for_session = session_uri
                original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_object_name)


            if not original_pil:
                messages.error(request, "Original image is required for editing. Please upload or specify a valid Media Path.")
            else:
                if processed_original_gcs_uri_for_session:
                    request.session['last_original_for_edit_gcs_uri'] = processed_original_gcs_uri_for_session
                
                # --- Handle Mask Image ---
                mask_pil = get_pil_image_from_form_data(
                    form.cleaned_data, 'mask_image_upload', 'mask_image_media_path'
                )
                uploaded_mask_file = form.cleaned_data.get('mask_image_upload')
                media_path_mask_from_form = form.cleaned_data.get('mask_image_media_path')

                if uploaded_mask_file and mask_pil:
                    mask_filename = uploaded_mask_file.name
                    temp_mask_gcs_obj_name = _generate_gcs_object_name(
                        settings.GCS_TEMP_INPUTS_PREFIX, mask_filename, 
                        extension=os.path.splitext(mask_filename)[1].strip('.') if '.' in mask_filename else 'png')
                    upload_pil_to_gcs(mask_pil, settings.GCS_BUCKET_NAME, temp_mask_gcs_obj_name)
                    processed_mask_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_mask_gcs_obj_name}"
                    mask_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, temp_mask_gcs_obj_name)
                elif media_path_mask_from_form and mask_pil:
                    full_gcs_mask_name = media_path_mask_from_form
                    if not (media_path_mask_from_form.startswith(settings.GCS_OBJECT_PATH_PREFIX) or \
                            media_path_mask_from_form.startswith(settings.GCS_TEMP_INPUTS_PREFIX)): # etc.
                        full_gcs_mask_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path_mask_from_form.lstrip('/')}"
                    else:
                        full_gcs_mask_name = media_path_mask_from_form.lstrip('/')
                    processed_mask_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_gcs_mask_name}"
                    mask_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_mask_name)
                elif request.session.get('last_mask_for_edit_gcs_uri') and mask_pil: # Loaded from session
                    session_mask_uri = request.session['last_mask_for_edit_gcs_uri']
                    full_gcs_mask_name = session_mask_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    # mask_pil is already loaded if this path is taken by get_pil_image_from_form_data logic for session fallback
                    processed_mask_gcs_uri_for_session = session_mask_uri
                    mask_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_mask_name)
                
                if processed_mask_gcs_uri_for_session:
                    request.session['last_mask_for_edit_gcs_uri'] = processed_mask_gcs_uri_for_session
                elif not (uploaded_mask_file or media_path_mask_from_form): # No new mask provided by user, clear from session
                    request.session.pop('last_mask_for_edit_gcs_uri', None)
                    mask_image_signed_url_for_preview = None # Clear preview too


                # --- Handle Style Reference Image (similar logic to mask) ---
                style_pil = get_pil_image_from_form_data(
                    form.cleaned_data, 'style_reference_image_upload', 'style_reference_image_media_path'
                )
                uploaded_style_file = form.cleaned_data.get('style_reference_image_upload')
                media_path_style_from_form = form.cleaned_data.get('style_reference_image_media_path')

                if uploaded_style_file and style_pil:
                    style_filename = uploaded_style_file.name
                    temp_style_gcs_obj_name = _generate_gcs_object_name(
                        settings.GCS_TEMP_INPUTS_PREFIX, style_filename,
                        extension=os.path.splitext(style_filename)[1].strip('.') if '.' in style_filename else 'png')
                    upload_pil_to_gcs(style_pil, settings.GCS_BUCKET_NAME, temp_style_gcs_obj_name)
                    processed_style_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_style_gcs_obj_name}"
                    style_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, temp_style_gcs_obj_name)

                elif media_path_style_from_form and style_pil:
                    full_gcs_style_name = media_path_style_from_form
                    if not (media_path_style_from_form.startswith(settings.GCS_OBJECT_PATH_PREFIX) or \
                            media_path_style_from_form.startswith(settings.GCS_TEMP_INPUTS_PREFIX)): # etc.
                        full_gcs_style_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path_style_from_form.lstrip('/')}"
                    else:
                        full_gcs_style_name = media_path_style_from_form.lstrip('/')
                    processed_style_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_gcs_style_name}"
                    style_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_style_name)
                elif request.session.get('last_style_for_edit_gcs_uri') and style_pil:
                    session_style_uri = request.session['last_style_for_edit_gcs_uri']
                    full_gcs_style_name = session_style_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    processed_style_gcs_uri_for_session = session_style_uri
                    style_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_style_name)

                if processed_style_gcs_uri_for_session:
                    request.session['last_style_for_edit_gcs_uri'] = processed_style_gcs_uri_for_session
                elif not (uploaded_style_file or media_path_style_from_form):
                    request.session.pop('last_style_for_edit_gcs_uri', None)
                    style_image_signed_url_for_preview = None


                # --- Call the Editing Service ---
                edited_image_results = edit_image_with_prompt(
                    original_pil_image=original_pil,
                    original_filename_for_naming=original_filename_for_naming,
                    n=form.cleaned_data['number_of_images'],
                    mask_pil_image=mask_pil,
                    prompt=form.cleaned_data['prompt'],
                    mode=form.cleaned_data['edit_mode'],
                    guidance=form.cleaned_data['guidance_scale'],
                    style_pil_image=style_pil,
                    hex_codes=form.cleaned_data['hex_palette_json']
                )
                messages.success(request, f"{len(edited_image_results)} image(s) edited and saved to Cloud Storage!")
                
                # Re-initialize form with current data for stickiness if re-rendering
                current_initial_data_for_form = {}
                if processed_original_gcs_uri_for_session:
                    path_in_b = processed_original_gcs_uri_for_session[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data_for_form['original_image_media_path'] = path_in_b[len(settings.GCS_OBJECT_PATH_PREFIX):] if path_in_b.startswith(settings.GCS_OBJECT_PATH_PREFIX) else path_in_b
                
                if processed_mask_gcs_uri_for_session:
                    path_in_b_mask = processed_mask_gcs_uri_for_session[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data_for_form['mask_image_media_path'] = path_in_b_mask[len(settings.GCS_OBJECT_PATH_PREFIX):] if path_in_b_mask.startswith(settings.GCS_OBJECT_PATH_PREFIX) else path_in_b_mask
                
                if processed_style_gcs_uri_for_session:
                    path_in_b_style = processed_style_gcs_uri_for_session[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data_for_form['style_reference_image_media_path'] = path_in_b_style[len(settings.GCS_OBJECT_PATH_PREFIX):] if path_in_b_style.startswith(settings.GCS_OBJECT_PATH_PREFIX) else path_in_b_style

                form = EditForm(initial=current_initial_data_for_form) # Use this updated data for the form instance

        except forms.ValidationError as ve:
            messages.error(request, str(ve))
        except FileNotFoundError as fnfe:
            messages.error(request, f"Error: An input file was not found. {fnfe}")
        except Exception as e:
            messages.error(request, f"An error occurred during image editing: {e}")
            import traceback
            traceback.print_exc()

    elif request.method == 'POST' and not form.is_valid():
         messages.error(request, f"Please correct the form errors below.")
         # When form is invalid on POST, re-populate preview URLs from session if they existed
         session_orig_uri = request.session.get('last_original_for_edit_gcs_uri')
         if session_orig_uri and session_orig_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/") and not original_image_signed_url_for_preview:
            path_in_bucket = session_orig_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            try: original_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, path_in_bucket)
            except: pass 

         session_mask_uri = request.session.get('last_mask_for_edit_gcs_uri')
         if session_mask_uri and session_mask_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/") and not mask_image_signed_url_for_preview:
            mask_path_in_bucket = session_mask_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            try: mask_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, mask_path_in_bucket)
            except: pass
            
         session_style_uri = request.session.get('last_style_for_edit_gcs_uri')
         if session_style_uri and session_style_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/") and not style_image_signed_url_for_preview:
            style_path_in_bucket = session_style_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            try: style_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, style_path_in_bucket)
            except: pass


    context = {
        'form': form,
        'edited_image_results': edited_image_results,
        'active_page': 'edit',
        'gcs_bucket_name_for_display': settings.GCS_BUCKET_NAME if settings.GCS_BUCKET_NAME != "your-gcs-bucket-name-here" else "[Not Set]",
        'gcs_path_prefix_for_display': settings.GCS_OBJECT_PATH_PREFIX,
        'original_image_signed_url_for_preview': original_image_signed_url_for_preview,
        'mask_image_signed_url_for_preview': mask_image_signed_url_for_preview,
        'style_image_signed_url_for_preview': style_image_signed_url_for_preview, # Pass to template
    }
    return render(request, 'studio/edit.html', context)

'''
def browse_view(request):
    if not settings.GCS_BUCKET_NAME or settings.GCS_BUCKET_NAME == "your-gcs-bucket-name-here":
        messages.warning(request, "GCS Bucket Name is not configured. Cannot browse images.")
        image_urls_from_bucket = []
    else:
        try:
            # Pass the GCS_OBJECT_PATH_PREFIX to list only images from this app
            image_urls_from_bucket = list_gcs_bucket_files(
                settings.GCS_BUCKET_NAME,
                prefix=settings.GCS_OBJECT_PATH_PREFIX,
            )
            if not image_urls_from_bucket:
                messages.info(request, "No images found in the configured GCS path, or the bucket is empty.")
        except Exception as e:
            messages.error(request, f"Error Browse GCS bucket: {e}")
            image_urls_from_bucket = []
            
    return render(request, 'studio/browse.html', {
        'image_urls': image_urls_from_bucket,
        'active_page': 'browse',
        'gcs_bucket_name': settings.GCS_BUCKET_NAME,
        'gcs_path_prefix': settings.GCS_OBJECT_PATH_PREFIX
    })
'''
def video_generate_view(request):
    initial_form_data = {}
    input_image_signed_url_for_preview = None # For GCS-sourced initial image preview
    input_gcs_object_name_for_form_display = None # Full path in bucket for form init

    job_id_to_check_on_get = request.GET.get('job_id')
    current_job_details = None
    processed_video_urls = []
    
    query_param_initial_image_path = request.GET.get('initial_image_gcs_path') # For "Use this image" link

    if request.method == 'GET':
        # Handle "Use this image" from another page or direct link
        if query_param_initial_image_path:
            input_gcs_object_name_for_form_display = query_param_initial_image_path
            request.session['last_initial_image_for_video_gcs_uri'] = f"gs://{settings.GCS_BUCKET_NAME}/{query_param_initial_image_path}"
            try:
                input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, query_param_initial_image_path)
            except Exception as e:
                messages.error(request, f"Could not load preview for initial image {query_param_initial_image_path}: {e}")
        else:
            # Try to load from session for stickiness
            session_initial_image_gcs_uri = request.session.get('last_initial_image_for_video_gcs_uri')
            if session_initial_image_gcs_uri and session_initial_image_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                path_in_bucket = session_initial_image_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                input_gcs_object_name_for_form_display = path_in_bucket
                try:
                    input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, path_in_bucket)
                except Exception as e:
                    messages.warning(request, f"Could not load session preview for initial image {path_in_bucket}: {e}")

        # Prepare initial data for the form field 'input_image_media_path'
        if input_gcs_object_name_for_form_display:
            # The form field expects path relative to GCS_OBJECT_PATH_PREFIX or full if from other known prefixes
            if input_gcs_object_name_for_form_display.startswith(settings.GCS_OBJECT_PATH_PREFIX):
                initial_form_data['input_image_media_path'] = input_gcs_object_name_for_form_display[len(settings.GCS_OBJECT_PATH_PREFIX):]
            elif input_gcs_object_name_for_form_display.startswith(settings.GCS_TEMP_INPUTS_PREFIX) or \
                 input_gcs_object_name_for_form_display.startswith(settings.GCS_GENERATED_IMAGES_PREFIX) or \
                 input_gcs_object_name_for_form_display.startswith(settings.GCS_EDITED_IMAGES_PREFIX):
                 initial_form_data['input_image_media_path'] = input_gcs_object_name_for_form_display # Form field takes full path here
            else:
                 initial_form_data['input_image_media_path'] = input_gcs_object_name_for_form_display

        # --- Job Status Checking part (as before) ---
        if job_id_to_check_on_get:
            try:
                current_job_details = VideoGenerationJob.objects.get(id=job_id_to_check_on_get)
                if current_job_details.status == 'COMPLETED' and current_job_details.public_video_urls_json:
                    try: processed_video_urls = json.loads(current_job_details.public_video_urls_json)
                    except json.JSONDecodeError: messages.warning(request, f"Job {current_job_details.id}: Could not parse stored video URLs.")
                elif current_job_details.status == 'FAILED':
                     messages.error(request, f"Job {current_job_details.id} previously failed: {current_job_details.error_message or 'Unknown error'}")
            except VideoGenerationJob.DoesNotExist:
                messages.error(request, "The requested video job was not found.")
                job_id_to_check_on_get = None


    form = VideoGenerateForm(request.POST or None, initial=initial_form_data or None, files=request.FILES or None)
    
    if request.method == 'POST' and form.is_valid():
        try:
            input_pil_image = None
            input_image_filename_for_service = None
            processed_initial_image_gcs_uri_for_session = None # gs:// URI

            uploaded_file = form.cleaned_data.get('input_image_upload')
            media_path_from_form = form.cleaned_data.get('input_image_media_path')

            if uploaded_file:
                input_pil_image = Image.open(uploaded_file)
                input_image_filename_for_service = uploaded_file.name
                # Upload this to GCS_TEMP_INPUTS_PREFIX for persistence if used
                temp_gcs_object_name = _generate_gcs_object_name(
                    base_prefix=settings.GCS_TEMP_INPUTS_PREFIX,
                    original_filename=input_image_filename_for_service,
                    extension=os.path.splitext(input_image_filename_for_service)[1].strip('.') if '.' in input_image_filename_for_service else 'png'
                )
                upload_pil_to_gcs(input_pil_image, settings.GCS_BUCKET_NAME, temp_gcs_object_name)
                processed_initial_image_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_gcs_object_name}"
                input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, temp_gcs_object_name)

            elif media_path_from_form:
                full_gcs_object_name = media_path_from_form
                if not (media_path_from_form.startswith(settings.GCS_OBJECT_PATH_PREFIX) or \
                        media_path_from_form.startswith(settings.GCS_TEMP_INPUTS_PREFIX) or \
                        media_path_from_form.startswith(settings.GCS_GENERATED_IMAGES_PREFIX) or \
                        media_path_from_form.startswith(settings.GCS_EDITED_IMAGES_PREFIX)):
                    full_gcs_object_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path_from_form.lstrip('/')}"
                else: # It's already a full path relative to bucket root
                    full_gcs_object_name = media_path_from_form.lstrip('/')
                
                input_pil_image = load_pil_from_gcs(settings.GCS_BUCKET_NAME, full_gcs_object_name)
                input_image_filename_for_service = os.path.basename(full_gcs_object_name)
                processed_initial_image_gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_gcs_object_name}"
                input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_object_name)
            
            elif request.session.get('last_initial_image_for_video_gcs_uri'): # Fallback to session
                session_uri = request.session['last_initial_image_for_video_gcs_uri']
                if session_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                    full_gcs_object_name = session_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    input_pil_image = load_pil_from_gcs(settings.GCS_BUCKET_NAME, full_gcs_object_name)
                    input_image_filename_for_service = os.path.basename(full_gcs_object_name)
                    processed_initial_image_gcs_uri_for_session = session_uri
                    input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, full_gcs_object_name)

            # Store the GCS URI of the initial image actually used for this session
            if processed_initial_image_gcs_uri_for_session:
                request.session['last_initial_image_for_video_gcs_uri'] = processed_initial_image_gcs_uri_for_session
            else: # No image used, clear from session
                request.session.pop('last_initial_image_for_video_gcs_uri', None)
                input_image_signed_url_for_preview = None


            job = generate_video_with_veo_sdk(
                prompt=form.cleaned_data['prompt'],
                input_pil_image=input_pil_image, # This will be None if no image was provided/loaded
                input_image_filename=input_image_filename_for_service,
                aspect_ratio=form.cleaned_data['aspect_ratio'],
                sample_count=form.cleaned_data['sample_count'],
                duration_seconds=float(form.cleaned_data['duration_seconds']),
                person_generation=form.cleaned_data['person_generation'],
                enable_prompt_rewriting=form.cleaned_data.get('enable_prompt_rewriting', True),
            )
            messages.info(request, f"Video generation job {job.id} started. Status: {job.get_status_display()}. Page will auto-update.")
            return redirect(f"{request.path}?job_id={job.id}")

        except forms.ValidationError as ve:
            messages.error(request, str(ve))
        except Exception as e:
            messages.error(request, f"Error starting video generation: {e}")
            import traceback
            traceback.print_exc()
    elif request.method == 'POST' and not form.is_valid():
         messages.error(request, f"Please correct the form errors: {form.errors.as_json(escape_html=True)}")
         # Re-populate preview URL from session if form is invalid on POST
         session_initial_uri = request.session.get('last_initial_image_for_video_gcs_uri')
         if session_initial_uri and session_initial_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/") and not input_image_signed_url_for_preview:
            path_in_bucket = session_initial_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            try: input_image_signed_url_for_preview = _get_signed_gcs_url(settings.GCS_BUCKET_NAME, path_in_bucket)
            except: pass

    context = {
        'form': form,
        'current_job_details': current_job_details,
        'job_id_for_polling': current_job_details.id if current_job_details and current_job_details.status in ['PENDING', 'PROCESSING'] else None,
        'processed_video_urls': processed_video_urls,
        'active_page': 'video_generate',
        'gcs_bucket_name_for_display': settings.GCS_BUCKET_NAME if settings.GCS_BUCKET_NAME != "your-gcs-bucket-name-here" else "[Not Set]",
        'gcs_path_prefix_for_display': settings.GCS_OBJECT_PATH_PREFIX,
        'input_image_signed_url_for_preview': input_image_signed_url_for_preview, # Pass for GCS preview
    }
    return render(request, 'studio/video_generate.html', context)


def check_video_job_status_api(request, job_id):
    """API endpoint for JavaScript to poll for video job status."""
    if not request.user.is_authenticated and not settings.DEBUG: # Add proper auth for API
        return JsonResponse({'status': 'FAILED', 'error': 'Authentication required.'}, status=403)
    
    try:
        # This function now also triggers the check against the Google LRO
        job = check_and_update_veo_job_status(job_id=job_id)
        
        video_urls = []
        if job.status == 'COMPLETED' and job.public_video_urls_json:
            try:
                video_urls = json.loads(job.public_video_urls_json)
            except json.JSONDecodeError:
                pass # Will be empty

        return JsonResponse({
            'job_id': str(job.id),
            'status': job.status,
            'video_urls': video_urls,
            'error_message': job.error_message,
            'veo_operation_name': job.veo_operation_name,
            'expected_output_gcs_prefix': f"gs://{settings.GCS_BUCKET_NAME}/{settings.GCS_OBJECT_PATH_PREFIX}video_outputs/{job.id}/" if job.status != 'FAILED' else None

        })
    except VideoGenerationJob.DoesNotExist:
        return JsonResponse({'status': 'NOT_FOUND', 'error': 'Job not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'ERROR', 'error': str(e)}, status=500)


def browse_view(request):
    filter_form = BrowseFilterForm(request.GET or None)
    media_files_from_bucket = []

    if not settings.GCS_BUCKET_NAME or settings.GCS_BUCKET_NAME == "your-gcs-bucket-name-here":
        messages.warning(request, "GCS Bucket Name is not configured. Cannot browse media.")
    else:
        media_type_filter = None
        name_filter = None
        if filter_form.is_valid():
            media_type_filter = filter_form.cleaned_data.get('media_type')
            name_filter = filter_form.cleaned_data.get('name_contains')
        
        try:
            # List from the main app prefix, then filter
            media_files_from_bucket = list_gcs_bucket_media_files(
                settings.GCS_BUCKET_NAME,
                base_prefix=settings.GCS_OBJECT_PATH_PREFIX,
                media_type_filter=media_type_filter,
                name_filter=name_filter
            )
            if not media_files_from_bucket and (media_type_filter or name_filter):
                 messages.info(request, "No media found matching your current filters.")
            elif not media_files_from_bucket:
                messages.info(request, "No media found in the configured GCS path, or the bucket is empty.")
        except Exception as e:
            messages.error(request, f"Error Browse GCS bucket: {e}")
            
    return render(request, 'studio/browse.html', {
        'filter_form': filter_form,
        'media_files': media_files_from_bucket,
        'active_page': 'browse',
        'gcs_bucket_name': settings.GCS_BUCKET_NAME,
        'gcs_path_prefix': settings.GCS_OBJECT_PATH_PREFIX
    })

# studio/views.py
# ... (existing imports: JsonResponse, settings, messages, list_gcs_bucket_media_files) ...

def list_media_api(request):
    """
    API endpoint to list media files from GCS for the media selector modal.
    Accepts an optional 'media_type' query parameter ('image' or 'video').
    """
    if not settings.GCS_BUCKET_NAME or settings.GCS_BUCKET_NAME == "your-gcs-bucket-name-here":
        return JsonResponse({'error': 'GCS Bucket Name not configured.'}, status=500)

    media_type_filter = request.GET.get('media_type', None) # e.g., 'image' or 'video'
    
    # You might want to limit the number of items or implement pagination for performance
    # if there are many files in the bucket. For now, it lists all matching.
    try:
        media_files = list_gcs_bucket_media_files(
            gcs_bucket_name=settings.GCS_BUCKET_NAME,
            base_prefix=settings.GCS_OBJECT_PATH_PREFIX,
            media_type_filter=media_type_filter
        )
        # 'media_files' is already a list of dicts with 'url' (signed), 'type', 'name', 'gcs_path'
        return JsonResponse({'media_files': media_files})
    except Exception as e:
        print(f"Error in list_media_api: {e}")
        return JsonResponse({'error': f'Error listing media: {str(e)}'}, status=500)