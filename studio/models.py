from django.db import models
from django.contrib.auth.models import User # Assuming you might want to link jobs to users later
import uuid

class VideoGenerationJob(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True) # Optional: link to user
    prompt = models.TextField()
    input_image_gcs_uri = models.CharField(max_length=1024, null=True, blank=True)
    
    veo_operation_name = models.CharField(max_length=255, null=True, blank=True) # From Veo LRO
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    
    # Store output video URLs as a JSON string (list of URLs)
    output_video_gcs_uris_json = models.TextField(blank=True, null=True) # Store list of gs:// URIs
    public_video_urls_json = models.TextField(blank=True, null=True) # Store list of public https:// URLs

    error_message = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Video Job {self.id} - {self.status}"

    class Meta:
        ordering = ['-created_at']