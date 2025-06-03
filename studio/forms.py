# studio/forms.py
from django import forms

class GenerateForm(forms.Form):
    # ... (remains as last update) ...
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'Describe the image you want to create...'}),
        label="Prompt"
    )
    negative_prompt = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control', 'placeholder': 'Describe what you DON\'T want to see...'}),
        label="Negative prompt (optional)",
        required=False
    )
    number_of_images = forms.IntegerField(
        min_value=1, max_value=4, initial=1,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '1'}),
        label="# Images"
    )
    ASPECT_RATIO_CHOICES = [
        ("1:1", "1:1"), ("3:4", "3:4"),
        ("4:3", "4:3"), ("16:9", "16:9"),
    ]
    aspect_ratio = forms.ChoiceField(
        choices=ASPECT_RATIO_CHOICES, initial="1:1",
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Aspect ratio"
    )
    guidance_scale = forms.FloatField(
        min_value=0, max_value=20, initial=7.5,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '0.5'}),
        label="Guidance scale"
    )
    seed = forms.IntegerField(
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Blank = random. May be ignored if watermarking is enabled by policy.'}),
        label="Seed (optional)",
        required=False
    )
    add_watermark = forms.BooleanField( # New field
        required=False,
        initial=True, # Default to adding watermark
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label="Add Watermark"
    )

    def clean(self):
        cleaned_data = super().clean()
        seed = cleaned_data.get('seed')
        add_watermark = cleaned_data.get('add_watermark')

        if seed is not None and add_watermark:
            self.add_error('seed', forms.ValidationError("A specific seed cannot be used when 'Add Watermark' is enabled. Please disable watermark or remove the seed."))
            self.add_error('add_watermark', forms.ValidationError("Disable watermark to use a specific seed, or remove the seed value."))
        return cleaned_data


class EditForm(forms.Form):
    # Option 1: File Upload
    original_image_upload = forms.ImageField(
        label="Upload Original Image",
        widget=forms.ClearableFileInput(attrs={'class': 'form-control'}),
        required=False # Not required if Media Path is provided
    )
    # Option 2: Media Path (formerly GCS Path)
    original_image_media_path = forms.CharField( # RENAMED FIELD
        label="OR Original Media Path (e.g., folder/image.png)",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Path within your Cloud Storage bucket'}),
        required=False
    )

    mask_image_upload = forms.ImageField(
        label="Upload Mask (optional)",
        widget=forms.ClearableFileInput(attrs={'class': 'form-control'}),
        required=False
    )
    mask_image_media_path = forms.CharField( # RENAMED FIELD
        label="OR Mask Media Path (optional)",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Path to mask in your Cloud Storage'}),
        required=False
    )
    
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'Describe the edits...'}),
        label="Edit prompt"
    )
    number_of_images = forms.IntegerField(
        min_value=1, max_value=4, initial=1,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '1'}),
        label="# Images"
    )
    EDIT_MODE_CHOICES = [
        ("mask_free", "Mask-Free Edit"), ("inpaint", "Inpaint/Insert"),
        ("outpaint", "Outpaint (Extend)"), ("background", "Background Edit (Semantic)"),
        ("product_bg_swap", "Product Background Swap"),
    ]
    edit_mode = forms.ChoiceField(
        choices=EDIT_MODE_CHOICES, initial="mask_free",
        widget=forms.RadioSelect,
        label="Edit mode"
    )
    guidance_scale = forms.FloatField(
        min_value=0, max_value=20, initial=7.5,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '0.5'}),
        label="Guidance scale"
    )
    
    style_reference_image_upload = forms.ImageField(
        label="Upload Style Reference (optional)",
        widget=forms.ClearableFileInput(attrs={'class': 'form-control'}),
        required=False
    )
    style_reference_image_media_path = forms.CharField( # RENAMED FIELD
        label="OR Style Reference Media Path (optional)",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Path to style image in Cloud Storage'}),
        required=False
    )

    hex_palette_json = forms.CharField(
        label='Hex palette JSON (e.g. ["#FF0000","#00FF00"])',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional JSON array of hex codes'}),
        required=False
    )

    def clean(self):
        cleaned_data = super().clean()
        original_upload = cleaned_data.get("original_image_upload")
        original_media_path = cleaned_data.get("original_image_media_path")

        if not original_upload and not original_media_path:
            raise forms.ValidationError(
                "Please provide either an uploaded original image or a Media Path to an original image."
            )
        if original_upload and original_media_path:
            raise forms.ValidationError(
                "Please provide either an uploaded original image OR a Media Path, not both."
            )
        
        mask_upload = cleaned_data.get("mask_image_upload")
        mask_media_path = cleaned_data.get("mask_image_media_path")
        if mask_upload and mask_media_path:
            raise forms.ValidationError("Please provide either an uploaded mask OR a Media Path for the mask, not both.")

        style_upload = cleaned_data.get("style_reference_image_upload")
        style_media_path = cleaned_data.get("style_reference_image_media_path")
        if style_upload and style_media_path:
            raise forms.ValidationError("Please provide either an uploaded style reference OR a Media Path for it, not both.")

        return cleaned_data


class VideoGenerateForm(forms.Form):
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': 'Describe the video you want to create...'}),
        label="Video Prompt"
    )
    input_image_upload = forms.ImageField(
        label="Upload Initial Image (Optional)",
        widget=forms.ClearableFileInput(attrs={'class': 'form-control'}),
        required=False
    )
    input_image_media_path = forms.CharField( # RENAMED FIELD
        label="OR Initial Media Path (Optional, e.g., folder/image.png)",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Path within your Cloud Storage bucket'}),
        required=False
    )

    ASPECT_RATIO_CHOICES_VIDEO = [
        ("16:9", "16:9 (Widescreen)"), ("9:16", "9:16 (Vertical)"),
        ("1:1", "1:1 (Square)"), ("4:3", "4:3"), ("3:4", "3:4"),
    ]
    aspect_ratio = forms.ChoiceField(
        choices=ASPECT_RATIO_CHOICES_VIDEO, initial="16:9",
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Aspect Ratio"
    )
    sample_count = forms.IntegerField(
        min_value=1, max_value=4, initial=1,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '1'}),
        label="# Video Samples"
    )
    duration_seconds = forms.FloatField(
        min_value=2, max_value=16, initial=8,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'type': 'range', 'step': '1'}),
        label="Duration (seconds)"
    )
    PERSON_GENERATION_CHOICES = [
        ("allow_adult", "Allow Adult"),
        ("allow_all", "Allow All (including children)"),
    ]
    person_generation = forms.ChoiceField(
        choices=PERSON_GENERATION_CHOICES, initial="allow_adult",
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Person Generation Policy"
    )
    enable_prompt_rewriting = forms.BooleanField(
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label="Enable Prompt Rewriting"
    )
    add_watermark = forms.BooleanField( # From Veo's request.json (optional)
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label="Add Watermark (if applicable by model)"
    )

    def clean(self):
        cleaned_data = super().clean()
        input_image_upload = cleaned_data.get("input_image_upload")
        input_image_media_path = cleaned_data.get("input_image_media_path")

        if input_image_upload and input_image_media_path:
            raise forms.ValidationError(
                "Please provide either an uploaded input image OR a Media Path, not both."
            )
        return cleaned_data


class BrowseFilterForm(forms.Form):
    MEDIA_TYPE_CHOICES = [
        ("", "All Media"),
        ("image", "Images Only"),
        ("video", "Videos Only"),
    ]
    media_type = forms.ChoiceField(
        choices=MEDIA_TYPE_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'})
    )
    name_contains = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Search by name...'}),
        label="Name contains"
    )