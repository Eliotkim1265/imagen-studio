import os
import json
from django.shortcuts import render, redirect
from django.conf import settings
from django.contrib import messages
from django.http import StreamingHttpResponse, Http404, JsonResponse, HttpResponse # Added HttpResponse
from django.urls import reverse
from PIL import Image
from django import forms

from .forms import GenerateForm, EditForm, VideoGenerateForm, BrowseFilterForm
from .models import VideoGenerationJob
from .services import (
    generate_images_from_prompt,
    edit_image_with_prompt,
    load_pil_from_gcs,
    _generate_gcs_object_name, # Keep if used by views directly, or ensure services handle all naming
    # _get_signed_gcs_url, # No longer primary for display, keep if used for direct downloads
    upload_pil_to_gcs, # Returns gcs_object_name
    generate_video_with_veo_sdk,
    check_and_update_veo_job_status,
    list_gcs_bucket_media_files, # Returns dicts with gcs_path
    _get_gcs_client # For the proxy view
)

def process_image_input( # Renamed for general use, added request for session
    request,
    form_cleaned_data, 
    upload_field_key, 
    path_field_key, 
    session_uri_key, 
    temp_prefix_setting_name # Pass the setting name as a string
):
    pil_img, filename_for_naming, gcs_uri_for_session, proxy_url_for_preview = None, None, None, None
    
    pil_img = get_pil_image_from_form_data(form_cleaned_data, upload_field_key, path_field_key)
    uploaded_file = form_cleaned_data.get(upload_field_key)
    media_path = form_cleaned_data.get(path_field_key)
    temp_prefix = getattr(settings, temp_prefix_setting_name) # Get prefix from settings

    if uploaded_file and pil_img:
        filename_for_naming = uploaded_file.name
        temp_obj_name = _generate_gcs_object_name(
            base_prefix=temp_prefix, 
            original_filename=filename_for_naming, 
            extension=os.path.splitext(filename_for_naming)[1].strip('.') if '.' in filename_for_naming else 'png'
        )
        upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, temp_obj_name) # upload_pil_to_gcs now returns object name or dict
        gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_obj_name}"
        try:
            proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': temp_obj_name})
        except Exception as e:
            print(f"Error generating proxy URL for uploaded file {temp_obj_name}: {e}")
            proxy_url_for_preview = f"#error-proxy-url-{temp_obj_name}"

    elif media_path and pil_img: # Image loaded via GCS path from form
        full_obj_name = media_path # Assumes get_pil_image_from_form_data ensures this is full path in bucket
        # Logic from get_pil_image_from_form_data to ensure full_obj_name is correct
        if not any(media_path.startswith(p) for p in [settings.GCS_OBJECT_PATH_PREFIX, settings.GCS_TEMP_INPUTS_PREFIX, settings.GCS_GENERATED_IMAGES_PREFIX, settings.GCS_EDITED_IMAGES_PREFIX, settings.GCS_VIDEO_OUTPUTS_PREFIX]):
            full_obj_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path.lstrip('/')}"
        else:
            full_obj_name = media_path.lstrip('/')

        filename_for_naming = os.path.basename(full_obj_name)
        gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_obj_name}"
        try:
            proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': full_obj_name})
        except Exception as e:
            print(f"Error generating proxy URL for GCS path {full_obj_name}: {e}")
            proxy_url_for_preview = f"#error-proxy-url-{full_obj_name}"

    elif request.session.get(session_uri_key) and pil_img: # Fallback to session if form fields empty but PIL loaded by get_pil_image_from_form_data earlier
        session_uri = request.session[session_uri_key]
        full_obj_name = session_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
        filename_for_naming = os.path.basename(full_obj_name)
        gcs_uri_for_session = session_uri
        try:
            proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': full_obj_name})
        except Exception as e:
            print(f"Error generating proxy URL for session GCS path {full_obj_name}: {e}")
            proxy_url_for_preview = f"#error-proxy-url-{full_obj_name}"


    if gcs_uri_for_session: 
        request.session[session_uri_key] = gcs_uri_for_session
    else: 
        # If no image was processed (neither upload nor path and not in session for this call),
        # ensure the session key is removed if it was only for *this specific input group*.
        # However, if it's a general "last original", you might not want to pop it unless a new one is explicitly cleared.
        # For now, if no image is processed for this field group, we don't touch the session here,
        # it will be handled by specific logic in the view if an input is cleared.
        pass # Or request.session.pop(session_uri_key, None) if appropriate

    return pil_img, filename_for_naming, gcs_uri_for_session, proxy_url_for_preview

# Helper function (ensure it's robust for various path inputs)
def get_pil_image_from_form_data(form_cleaned_data, upload_field_name, media_path_field_name):
    uploaded_file = form_cleaned_data.get(upload_field_name)
    media_path_from_form = form_cleaned_data.get(media_path_field_name)

    if uploaded_file:
        try:
            return Image.open(uploaded_file)
        except Exception as e:
            raise forms.ValidationError(f"Could not open uploaded file '{upload_field_name}': {e}")
    elif media_path_from_form:
        # This path is expected to be the GCS object name (path from bucket root)
        # E.g., "media_studio_app_data/generated_images/image.png"
        # OR "media_studio_app_data/temp_inputs/image.png"
        full_gcs_object_name = media_path_from_form.lstrip('/')
        try:
            return load_pil_from_gcs(settings.GCS_BUCKET_NAME, full_gcs_object_name)
        except FileNotFoundError:
            raise forms.ValidationError(f"Media not found at Cloud Storage path: {full_gcs_object_name}")
        except Exception as e:
            raise forms.ValidationError(f"Error loading media from Cloud Storage path {full_gcs_object_name}: {e}")
    return None


# --- Media Proxy View ---
def serve_gcs_media_view(request, gcs_object_name):
    if not settings.GCS_BUCKET_NAME:
        raise Http404("GCS bucket not configured.")
    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(settings.GCS_BUCKET_NAME)
        blob = bucket.blob(gcs_object_name)

        if not blob.exists():
            raise Http404("Media object not found in Cloud Storage.")

        def file_iterator(blob_to_stream, chunk_size=8192 * 4): # Increased chunk size
            with blob_to_stream.open('rb') as GCSObject:
                while True:
                    chunk = GCSObject.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        
        content_type = blob.content_type or 'application/octet-stream'
        response = StreamingHttpResponse(file_iterator(blob), content_type=content_type)
        
        # Suggest filename for download, but display inline otherwise
        # Modern browsers often ignore Content-Disposition: inline for videos/images and display them anyway.
        disposition_type = 'attachment' if request.GET.get('download') == 'true' else 'inline'
        response['Content-Disposition'] = f'{disposition_type}; filename="{os.path.basename(gcs_object_name)}"'
        
        # For video streaming, browsers might send Range requests.
        # GCS client library's blob.open('rb') can handle Range requests if the underlying transport supports it.
        # StreamingHttpResponse doesn't automatically handle Range requests from Django's side.
        # For full-featured video seeking, more complex range request handling might be needed here
        # or rely on GCS's direct serving capabilities (e.g. for a "download" button using a signed URL).
        # For now, this provides basic streaming.
        if blob.size: # Add Content-Length if available
             response['Content-Length'] = str(blob.size)

        return response
    except Exception as e:
        print(f"Error proxying GCS object {gcs_object_name}: {e}")
        import traceback
        traceback.print_exc()
        return HttpResponse("Error serving media file.", status=500)


def generate_view(request):
    form = GenerateForm(request.POST or None)
    generated_image_results_for_template = []

    if request.method == 'POST' and form.is_valid():
        try:
            seed_val = form.cleaned_data.get('seed')
            seed_int = int(seed_val) if seed_val is not None else None
            add_watermark_val = form.cleaned_data.get('add_watermark', True) # From form

            # Service returns list of dicts: [{'gcs_object_name': ..., 'error':... (optional)}, ...]
            service_results = generate_images_from_prompt(
                prompt=form.cleaned_data['prompt'],
                negative_prompt=form.cleaned_data.get('negative_prompt'),
                n=form.cleaned_data['number_of_images'],
                aspect=form.cleaned_data['aspect_ratio'],
                guidance=form.cleaned_data['guidance_scale'],
                seed=seed_int,
            )
            for item in service_results:
                if item.get('gcs_object_name') and not item.get('error'):
                    try:
                        proxy_url = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': item['gcs_object_name']})
                        generated_image_results_for_template.append({
                            'proxy_url': proxy_url,
                            'gcs_object_name': item['gcs_object_name'],
                            'name': os.path.basename(item['gcs_object_name'])
                        })
                    except Exception as url_e:
                        print(f"Error creating proxy URL for {item['gcs_object_name']}: {url_e}")
                        generated_image_results_for_template.append({
                            'proxy_url': f"#error-url-creation-{os.path.basename(item.get('gcs_object_name','unknown'))}",
                            'gcs_object_name': item.get('gcs_object_name'), 'name': os.path.basename(item.get('gcs_object_name','Error'))
                        })
                else:
                    error_msg = item.get('error', 'Unknown processing error')
                    messages.error(request, f"Failed to process an image ({item.get('gcs_object_name')}): {error_msg}")
                    generated_image_results_for_template.append({
                        'proxy_url': f"#{error_msg.replace(' ','-').lower()}", # Create a slug-like error
                        'gcs_object_name': item.get('gcs_object_name'), 'name': f"Error - {os.path.basename(item.get('gcs_object_name','Error'))}"
                    })


            if any(not r.get('proxy_url','').startswith("#error") for r in generated_image_results_for_template):
                 messages.success(request, f"Image generation complete. Results below.")
            elif generated_image_results_for_template: # If all results are errors from service
                 messages.warning(request, "Image generation process had issues. See details below.")
            else: # Service returned nothing
                 messages.warning(request, "Image generation process completed, but no images were returned.")

        except forms.ValidationError: # Should be handled by is_valid, but catchall
            pass 
        except Exception as e:
            messages.error(request, f"Error generating images: {e}")
            import traceback
            traceback.print_exc()
    elif request.method == 'POST' and not form.is_valid():
         messages.error(request, f"Please correct the form errors below.")

    context = {
        'form': form,
        'generated_image_results': generated_image_results_for_template,
        'active_page': 'generate',
        'settings': settings,
    }
    return render(request, 'studio/generate.html', context)


def edit_view(request):
    initial_form_data = {}
    original_image_proxy_url_for_preview = None
    mask_image_proxy_url_for_preview = None
    style_image_proxy_url_for_preview = None
    style_gcs_object_name_for_form_display = None # <--- ADD THIS LINE
    original_gcs_object_name_for_form_field = None
    mask_gcs_object_name_for_form_field = None
    style_gcs_object_name_for_form_field = None

    query_param_original_gcs_path = request.GET.get('original_gcs_path')

    if request.method == 'GET':
        path_source_is_query_param = False
        if query_param_original_gcs_path:
            original_gcs_object_name_for_form_field = query_param_original_gcs_path
            request.session['last_original_for_edit_gcs_uri'] = f"gs://{settings.GCS_BUCKET_NAME}/{query_param_original_gcs_path}"
            request.session.pop('last_mask_for_edit_gcs_uri', None) # Clear mask if new original from link
            request.session.pop('last_style_for_edit_gcs_uri', None) # Clear style if new original
            path_source_is_query_param = True
        else:
            session_original_gcs_uri = request.session.get('last_original_for_edit_gcs_uri')
            if session_original_gcs_uri and session_original_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                original_gcs_object_name_for_form_field = session_original_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            
            session_mask_gcs_uri = request.session.get('last_mask_for_edit_gcs_uri')
            if session_mask_gcs_uri and session_mask_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                mask_gcs_object_name_for_form_field = session_mask_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]

            session_style_gcs_uri = request.session.get('last_style_for_edit_gcs_uri')
            if session_style_gcs_uri and session_style_gcs_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                style_gcs_object_name_for_form_field = session_style_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]

        # Generate preview URLs for display (these are Django proxy URLs)
        if original_gcs_object_name_for_form_field:
            initial_form_data['original_image_media_path'] = original_gcs_object_name_for_form_field # Form field gets full path from bucket root
            try: original_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': original_gcs_object_name_for_form_field})
            except Exception as e: messages.warning(request, f"Could not generate preview URL for original image: {e}")
        
        if mask_gcs_object_name_for_form_field:
            initial_form_data['mask_image_media_path'] = mask_gcs_object_name_for_form_display
            try: mask_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': mask_gcs_object_name_for_form_display})
            except Exception as e: messages.warning(request, f"Could not generate preview URL for mask image: {e}")

        if style_gcs_object_name_for_form_display:
            initial_form_data['style_reference_image_media_path'] = style_gcs_object_name_for_form_display
            try: style_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': style_gcs_object_name_for_form_display})
            except Exception as e: messages.warning(request, f"Could not generate preview URL for style image: {e}")


    form = EditForm(request.POST or None, initial=initial_form_data or None, files=request.FILES or None)
    edited_image_results_for_template = []

    if request.method == 'POST' and form.is_valid():
        original_pil, mask_pil, style_pil = None, None, None
        original_filename = None
        processed_original_gcs_uri, processed_mask_gcs_uri, processed_style_gcs_uri = None, None, None
        try:
            def process_image_input_for_post(upload_field_key, path_field_key, session_uri_key, temp_prefix_setting):
                pil_img, filename_for_naming, gcs_uri_for_session, proxy_url_for_preview = None, None, None, None
                
                pil_img = get_pil_image_from_form_data(form.cleaned_data, upload_field_key, path_field_key)
                uploaded_file = form.cleaned_data.get(upload_field_key)
                media_path = form.cleaned_data.get(path_field_key)

                if uploaded_file and pil_img:
                    filename_for_naming = uploaded_file.name
                    temp_obj_name = _generate_gcs_object_name(
                        getattr(settings, temp_prefix_setting), filename_for_naming, 
                        extension=os.path.splitext(filename_for_naming)[1].strip('.') if '.' in filename_for_naming else 'png')
                    upload_pil_to_gcs(pil_img, settings.GCS_BUCKET_NAME, temp_obj_name) # Returns GCS object name
                    gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{temp_obj_name}"
                    proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': temp_obj_name})
                elif media_path and pil_img:
                    full_obj_name = media_path # Assuming get_pil_image_from_form_data ensures this is full path in bucket
                    if not any(media_path.startswith(p) for p in [settings.GCS_OBJECT_PATH_PREFIX, settings.GCS_TEMP_INPUTS_PREFIX, settings.GCS_GENERATED_IMAGES_PREFIX, settings.GCS_EDITED_IMAGES_PREFIX]):
                        full_obj_name = f"{settings.GCS_OBJECT_PATH_PREFIX.rstrip('/')}/{media_path.lstrip('/')}"
                    else:
                        full_obj_name = media_path.lstrip('/')

                    filename_for_naming = os.path.basename(full_obj_name)
                    gcs_uri_for_session = f"gs://{settings.GCS_BUCKET_NAME}/{full_obj_name}"
                    proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': full_obj_name})
                elif request.session.get(session_uri_key) and pil_img: # Fallback to session if form fields empty but PIL loaded by helper
                    session_uri = request.session[session_uri_key]
                    full_obj_name = session_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    filename_for_naming = os.path.basename(full_obj_name)
                    gcs_uri_for_session = session_uri
                    proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': full_obj_name})

                if gcs_uri_for_session: request.session[session_uri_key] = gcs_uri_for_session
                else: request.session.pop(session_uri_key, None)
                return pil_img, filename_for_naming, gcs_uri_for_session, proxy_url_for_preview

            original_pil, original_filename, processed_original_gcs_uri, original_image_proxy_url_for_preview = process_image_input_for_post(
                'original_image_upload', 'original_image_media_path', 'last_original_for_edit_gcs_uri', 'GCS_TEMP_INPUTS_PREFIX')
            mask_pil, _, processed_mask_gcs_uri, mask_image_proxy_url_for_preview = process_image_input_for_post(
                'mask_image_upload', 'mask_image_media_path', 'last_mask_for_edit_gcs_uri', 'GCS_TEMP_INPUTS_PREFIX')
            style_pil, _, processed_style_gcs_uri, style_image_proxy_url_for_preview = process_image_input_for_post(
                'style_reference_image_upload', 'style_reference_image_media_path', 'last_style_for_edit_gcs_uri', 'GCS_TEMP_INPUTS_PREFIX')


            if not original_pil:
                messages.error(request, "Original image is required for editing.")
            else:
                service_results = edit_image_with_prompt(
                    original_pil_image=original_pil, original_filename_for_naming=original_filename,
                    n=form.cleaned_data['number_of_images'], mask_pil_image=mask_pil,
                    prompt=form.cleaned_data['prompt'], mode=form.cleaned_data['edit_mode'],
                    guidance=form.cleaned_data.get('guidance_scale'), style_pil_image=style_pil,
                    hex_codes=form.cleaned_data['hex_palette_json']
                )
                for item in service_results:
                    if item.get('gcs_object_name') and not item.get('error'):
                        edited_image_results_for_template.append({
                            'proxy_url': reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': item['gcs_object_name']}),
                            'gcs_object_name': item['gcs_object_name'],
                            'name': os.path.basename(item['gcs_object_name'])
                        })
                    else: edited_image_results_for_template.append(item)
                
                messages.success(request, f"{len(edited_image_results_for_template)} image(s) edited and saved to Cloud Storage!")
                
                current_initial_data = {}
                if processed_original_gcs_uri:
                    path_in_b = processed_original_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data['original_image_media_path'] = path_in_b # Store full path for form
                if processed_mask_gcs_uri:
                    path_in_b_mask = processed_mask_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data['mask_image_media_path'] = path_in_b_mask
                if processed_style_gcs_uri:
                    path_in_b_style = processed_style_gcs_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    current_initial_data['style_reference_image_media_path'] = path_in_b_style
                form = EditForm(initial=current_initial_data)

        except forms.ValidationError as ve: messages.error(request, str(ve))
        except FileNotFoundError as fnfe: messages.error(request, f"Error: An input file was not found. {fnfe}")
        except Exception as e:
            messages.error(request, f"An error occurred during image editing: {e}")
            import traceback; traceback.print_exc()

    elif request.method == 'POST' and not form.is_valid():
         messages.error(request, f"Please correct the form errors below.")
         # Re-populate preview URLs from session if form is invalid on POST
         if request.session.get('last_original_for_edit_gcs_uri') and not original_image_proxy_url_for_preview:
            uri = request.session['last_original_for_edit_gcs_uri']
            if uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                p = uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                try: original_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': p})
                except: pass
         if request.session.get('last_mask_for_edit_gcs_uri') and not mask_image_proxy_url_for_preview:
            uri = request.session['last_mask_for_edit_gcs_uri']
            if uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                p = uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                try: mask_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': p})
                except: pass
         if request.session.get('last_style_for_edit_gcs_uri') and not style_image_proxy_url_for_preview:
            uri = request.session['last_style_for_edit_gcs_uri']
            if uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                p = uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                try: style_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': p})
                except: pass

    context = {
        'form': form,
        'edited_image_results': edited_image_results_for_template,
        'active_page': 'edit',
        'gcs_bucket_name_for_display': settings.GCS_BUCKET_NAME if settings.GCS_BUCKET_NAME != "your-gcs-bucket-name-here" else "[Not Set]",
        'gcs_path_prefix_for_display': settings.GCS_OBJECT_PATH_PREFIX,
        'original_image_proxy_url_for_preview': original_image_proxy_url_for_preview,
        'mask_image_proxy_url_for_preview': mask_image_proxy_url_for_preview,
        'style_image_proxy_url_for_preview': style_image_proxy_url_for_preview,
        'settings': settings,
    }
    return render(request, 'studio/edit.html', context)


def video_generate_view(request):
    initial_form_data = {}
    input_image_proxy_url_for_preview = None
    # Stores the full GCS object name (path from bucket root) for form pre-filling and preview generation
    gcs_object_name_for_initial_image_preview_logic = None 

    job_id_to_check_on_get = request.GET.get('job_id')
    current_job_details = None
    processed_video_proxy_urls = [] # For displaying completed videos

    # Handle "Use this image" link (e.g., from Browse page) or session stickiness for GET requests
    query_param_initial_image_gcs_path = request.GET.get('initial_image_gcs_path') 

    if request.method == 'GET':
        if query_param_initial_image_gcs_path:
            # Query parameter takes precedence for setting the initial image
            gcs_object_name_for_initial_image_preview_logic = query_param_initial_image_gcs_path
            request.session['last_initial_image_for_video_gcs_uri'] = f"gs://{settings.GCS_BUCKET_NAME}/{query_param_initial_image_gcs_path}"
        else:
            # Try to load from session for stickiness if no query parameter
            session_uri = request.session.get('last_initial_image_for_video_gcs_uri')
            if session_uri and session_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                gcs_object_name_for_initial_image_preview_logic = session_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
        
        # If we have a GCS object name (from query param or session), prepare for form and preview
        if gcs_object_name_for_initial_image_preview_logic:
            # The form field 'input_image_media_path' should display the full GCS object name
            initial_form_data['input_image_media_path'] = gcs_object_name_for_initial_image_preview_logic
            try:
                input_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': gcs_object_name_for_initial_image_preview_logic})
            except Exception as e:
                messages.warning(request, f"Could not generate preview URL for initial image '{gcs_object_name_for_initial_image_preview_logic}': {e}")
        
        # --- Job Status Checking part for GET requests ---
        if job_id_to_check_on_get:
            try:
                current_job_details = VideoGenerationJob.objects.get(id=job_id_to_check_on_get)
                if current_job_details.status == 'COMPLETED' and current_job_details.output_video_gcs_uris_json:
                    try:
                        # These are raw GCS object names (paths within bucket) or full gs:// URIs
                        gcs_uris_or_paths_list = json.loads(current_job_details.output_video_gcs_uris_json)
                        for item_path in gcs_uris_or_paths_list:
                            object_name_for_proxy = item_path
                            if item_path.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"):
                                object_name_for_proxy = item_path[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                            elif item_path.startswith("gs://"): # Video is in a different bucket
                                messages.warning(request, f"Video {item_path} is in a different bucket and cannot be proxied by this app. Showing direct link.")
                                processed_video_proxy_urls.append(item_path.replace("gs://", "https://storage.googleapis.com/"))
                                continue
                            # Else, assume item_path is already just the object path relative to bucket root
                            
                            try:
                                processed_video_proxy_urls.append(reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': object_name_for_proxy}))
                            except Exception as url_e:
                                print(f"Error creating proxy URL for video object {object_name_for_proxy}: {url_e}")
                                processed_video_proxy_urls.append(f"#error-proxy-url-{os.path.basename(object_name_for_proxy)}")
                    except json.JSONDecodeError:
                        messages.warning(request, f"Job {current_job_details.id}: Could not parse stored video GCS URIs/paths.")
                elif current_job_details.status == 'FAILED':
                     messages.error(request, f"Job {current_job_details.id} previously failed: {current_job_details.error_message or 'Unknown error'}")
            except VideoGenerationJob.DoesNotExist:
                messages.error(request, "The requested video job was not found.")
                job_id_to_check_on_get = None # Clear if job doesn't exist


    form = VideoGenerateForm(request.POST or None, initial=initial_form_data or None, files=request.FILES or None)
    
    if request.method == 'POST' and form.is_valid():
        try:
            # Use the global process_image_input helper to handle uploaded file or media path
            # It returns: (pil_img, filename_for_naming, gs_uri_for_session, proxy_url_for_preview)
            input_pil_image, input_image_filename_for_service, \
            processed_initial_gcs_uri_for_session, \
            input_image_proxy_url_for_preview_on_post = process_image_input(
                 request, # Pass the request object for session handling
                 form.cleaned_data, 
                 'input_image_upload', 
                 'input_image_media_path', 
                 'last_initial_image_for_video_gcs_uri', # Session key for this specific input
                 'GCS_TEMP_INPUTS_PREFIX' # Name of the settings attribute for temp upload prefix
            )
            
            # Update the main preview URL if an image was processed in POST and a proxy URL was generated
            if processed_initial_gcs_uri_for_session: # This implies an image was processed
                 input_image_proxy_url_for_preview = input_image_proxy_url_for_preview_on_post

            # Call the service function to start the Veo generation
            job = generate_video_with_veo_sdk(
                prompt=form.cleaned_data['prompt'],
                input_pil_image=input_pil_image, # Will be None if no image was provided/loaded
                input_image_filename=input_image_filename_for_service, # For naming temp GCS upload
                aspect_ratio=form.cleaned_data['aspect_ratio'],
                sample_count=form.cleaned_data['sample_count'],
                duration_seconds=float(form.cleaned_data['duration_seconds']),
                person_generation=form.cleaned_data['person_generation'],
                enable_prompt_rewriting=form.cleaned_data.get('enable_prompt_rewriting', True)
                # add_watermark is now handled by VideoGenerateForm and passed here if still present
            )
            messages.info(request, f"Video generation job {job.id} started. Status: {job.get_status_display()}. The page will auto-update.")
            return redirect(f"{request.path}?job_id={job.id}") # Redirect to GET with job_id

        except forms.ValidationError as ve: # Catch validation errors from get_pil_image_from_form_data or form.clean()
            messages.error(request, str(ve))
            # Form will re-render with errors
        except Exception as e:
            messages.error(request, f"Error starting video generation: {e}")
            import traceback
            traceback.print_exc() # For server logs
            # Form will re-render
            
    elif request.method == 'POST' and not form.is_valid():
         messages.error(request, f"Please correct the form errors below.")
         # If form is invalid on POST, try to re-populate preview URL from session for stickiness
         session_initial_uri = request.session.get('last_initial_image_for_video_gcs_uri')
         if session_initial_uri and session_initial_uri.startswith(f"gs://{settings.GCS_BUCKET_NAME}/") and not input_image_proxy_url_for_preview:
            path_in_bucket = session_initial_uri[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
            try: 
                input_image_proxy_url_for_preview = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': path_in_bucket})
            except Exception as e:
                print(f"Error generating proxy URL for session image on POST error: {e}")
                input_image_proxy_url_for_preview = f"#error-preview-{path_in_bucket}"


    context = {
        'form': form, 
        'current_job_details': current_job_details,
        'job_id_for_polling': current_job_details.id if current_job_details and current_job_details.status in ['PENDING', 'PROCESSING'] else None,
        'processed_video_urls': processed_video_proxy_urls, # List of proxy URLs for completed videos
        'active_page': 'video_generate',
        'gcs_bucket_name_for_display': settings.GCS_BUCKET_NAME if settings.GCS_BUCKET_NAME != "your-gcs-bucket-name-here" else "[Not Set]",
        'gcs_path_prefix_for_display': settings.GCS_OBJECT_PATH_PREFIX, # For help text in template
        'input_image_proxy_url_for_preview': input_image_proxy_url_for_preview,
        'settings': settings, # For accessing settings in template if needed
    }
    return render(request, 'studio/video_generate.html', context)


def check_video_job_status_api(request, job_id):
    if not request.user.is_authenticated and not settings.DEBUG:
        return JsonResponse({'status': 'FAILED', 'error': 'Authentication required.'}, status=403)
    
    try:
        job = check_and_update_veo_job_status(job_id=job_id)
        proxy_video_urls_for_api = []
        if job.status == 'COMPLETED' and job.output_video_gcs_uris_json:
            try:
                gcs_object_names_list = json.loads(job.output_video_gcs_uris_json) # These are now object names
                for obj_name in gcs_object_names_list:
                    if obj_name.startswith(f"gs://{settings.GCS_BUCKET_NAME}/"): # If full gs:// URI was stored
                        obj_name_for_url = obj_name[len(f"gs://{settings.GCS_BUCKET_NAME}/"):]
                    elif obj_name.startswith("gs://"): # From a different bucket
                        proxy_video_urls_for_api.append(obj_name.replace("gs://", "https://storage.googleapis.com/"))
                        continue
                    else: # Assumed to be object name already
                        obj_name_for_url = obj_name
                    proxy_video_urls_for_api.append(reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': obj_name_for_url}))
            except json.JSONDecodeError: pass
            except Exception as e_url: print(f"Error creating proxy URL in API for job {job.id}: {e_url}")


        return JsonResponse({
            'job_id': str(job.id), 'status': job.status,
            'video_urls': proxy_video_urls_for_api, # Send proxy URLs
            'error_message': job.error_message, 'veo_operation_name': job.veo_operation_name,
            'expected_output_gcs_prefix': f"gs://{settings.GCS_BUCKET_NAME}/{settings.GCS_VIDEO_OUTPUTS_PREFIX}{job.id}/" if job.status != 'FAILED' else None
        })
    except VideoGenerationJob.DoesNotExist:
        return JsonResponse({'status': 'NOT_FOUND', 'error': 'Job not found.'}, status=404)
    except Exception as e:
        import traceback; traceback.print_exc();
        return JsonResponse({'status': 'ERROR', 'error': str(e)}, status=500)


def list_media_api(request):
    if not settings.GCS_BUCKET_NAME or settings.GCS_BUCKET_NAME == "your-gcs-bucket-name-here":
        return JsonResponse({'error': 'Cloud Storage bucket not configured.'}, status=500)

    media_type_filter = request.GET.get('media_type', None)
    name_filter = request.GET.get('name_contains', None)
    
    try:
        media_items_from_service = list_gcs_bucket_media_files(
            gcs_bucket_name=settings.GCS_BUCKET_NAME,
            base_prefix=settings.GCS_OBJECT_PATH_PREFIX,
            media_type_filter=media_type_filter,
            name_filter=name_filter
        )
        
        processed_media_files_for_api = []
        for item in media_items_from_service:
            try:
                proxy_url = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': item['gcs_path']})
                processed_media_files_for_api.append({
                    'url_for_display': proxy_url, # This is the proxy URL for modal thumbnails
                    'gcs_path': item['gcs_path'],   # Path within bucket for selection
                    'type': item['type'],
                    'name': item['name'],
                    'updated': item.get('updated').isoformat() if item.get('updated') else None
                })
            except Exception as e:
                print(f"Error reversing URL for GCS object in list_media_api {item.get('gcs_path', 'unknown')}: {e}")
                processed_media_files_for_api.append({
                    'url_for_display': f"#error-proxy-url-{item.get('name', 'unknown')}",
                    'gcs_path': item.get('gcs_path'), 'type': item.get('type'),
                    'name': item.get('name', 'Error Item'),
                    'updated': item.get('updated').isoformat() if item.get('updated') else None
                })
        return JsonResponse({'media_files': processed_media_files_for_api})
    except Exception as e:
        print(f"Error in list_media_api: {e}")
        import traceback; traceback.print_exc();
        return JsonResponse({'error': f'Error listing media: {str(e)}'}, status=500)

def browse_view(request):
    filter_form = BrowseFilterForm(request.GET or None)
    processed_media_files_for_template = []

    if not settings.GCS_BUCKET_NAME or settings.GCS_BUCKET_NAME == "your-gcs-bucket-name-here":
        messages.warning(request, "Cloud Storage bucket is not configured. Cannot browse media.")
    else:
        media_type_filter_val = None
        name_filter_val = None
        if filter_form.is_valid():
            media_type_filter_val = filter_form.cleaned_data.get('media_type')
            name_filter_val = filter_form.cleaned_data.get('name_contains')
        
        try:
            media_items_from_service = list_gcs_bucket_media_files(
                gcs_bucket_name=settings.GCS_BUCKET_NAME,
                base_prefix=settings.GCS_OBJECT_PATH_PREFIX,
                media_type_filter=media_type_filter_val,
                name_filter=name_filter_val
            )
            for item in media_items_from_service:
                try:
                    proxy_url = reverse('studio:serve_gcs_media', kwargs={'gcs_object_name': item['gcs_path']})
                    processed_media_files_for_template.append({
                        'proxy_url': proxy_url, # Renamed from 'url' to 'proxy_url' for clarity
                        'type': item['type'],
                        'name': item['name'],
                        'gcs_path': item['gcs_path'], # For "Edit Again" links for images
                        'updated': item['updated']
                    })
                except Exception as e_url:
                    print(f"Error creating proxy URL for browse item {item['gcs_path']}: {e_url}")
                    # Optionally skip or add an error placeholder
            
            if not processed_media_files_for_template:
                if media_type_filter_val or name_filter_val:
                     messages.info(request, "No media found matching your current filters.")
                else:
                    messages.info(request, "No media found in your Cloud Storage or the location is empty.")
        except Exception as e:
            messages.error(request, f"Error Browse Cloud Storage: {e}")
            import traceback; traceback.print_exc();
            
    return render(request, 'studio/browse.html', {
        'filter_form': filter_form,
        'media_files': processed_media_files_for_template, # Pass processed list
        'active_page': 'browse',
        'gcs_bucket_name': settings.GCS_BUCKET_NAME, # For display in template if needed
        'gcs_path_prefix': settings.GCS_OBJECT_PATH_PREFIX, # For display in template
        'settings': settings,
    })