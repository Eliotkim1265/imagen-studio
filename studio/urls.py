# studio/urls.py
from django.urls import path
from . import views

app_name = 'studio'

urlpatterns = [
    path('', views.generate_view, name='generate_home'), # Default to generate page
    path('generate/', views.generate_view, name='generate'),
    path('edit/', views.edit_view, name='edit'),
    path('browse/', views.browse_view, name='browse'),
    path('video/', views.video_generate_view, name='video_generate'),
    path('video/status/<uuid:job_id>/', views.check_video_job_status_api, name='check_video_job_status_api'), 
    path('api/list-media/', views.list_media_api, name='list_media_api'),
    
    # --- NEW URL pattern for serving GCS media files via proxy ---
    path('media_files/<path:gcs_object_name>', views.serve_gcs_media_view, name='serve_gcs_media'),
]